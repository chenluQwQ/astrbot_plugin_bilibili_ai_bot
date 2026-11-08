"""和 B站说话的底层：带 Cookie 的请求头，GET 和 POST。"""
from __future__ import annotations

import asyncio

import aiohttp

from ...config import USER_AGENT
from ...network import plugin_http_session
from ...runtime import WriteResultUnknownError
from ...settings import Settings
from .request_log import note_request
from ..host_view import HostView

# B站的风控码：通常是 wbi key 轮换了或签名失效。
RISK_CONTROL_CODE = -352


def has_cookie(config):
    return bool(Settings(config).get("SESSDATA"))


def bili_headers(config):
    settings = Settings(config)
    cookie_parts = (
        f"SESSDATA={settings.get('SESSDATA')}; "
        f"bili_jct={settings.get('BILI_JCT')}; "
        f"DedeUserID={settings.get('DEDE_USER_ID')}"
    )
    buvid3 = settings.get("BUVID3")
    if buvid3:
        cookie_parts += f"; buvid3={buvid3}"
    buvid4 = settings.get("BUVID4")
    if buvid4:
        cookie_parts += f"; buvid4={buvid4}"
    return {
        "Cookie": cookie_parts,
        "User-Agent": USER_AGENT,
        "Referer": "https://www.bilibili.com",
        "Accept-Encoding": "gzip, deflate",
    }


async def http_get(config, url, headers, params=None, timeout=10, retries=2, on_risk_control=None):
    """GET 可以重试；收到风控码时调一次 ``on_risk_control``，结果照常返回。"""
    last_err = None
    for i in range(retries + 1):
        try:
            note_request("GET", url)
            async with plugin_http_session(config) as s:
                async with s.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as r:
                    d = await r.json(content_type=None)
                    if on_risk_control and isinstance(d, dict) and d.get("code") == RISK_CONTROL_CODE:
                        on_risk_control()
                    return d, r
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as e:
            last_err = e
            if i < retries:
                await asyncio.sleep(0.5 * (i + 1))
                continue
            raise
    raise last_err


async def http_get_text(config, url, headers, params=None, timeout=10, retries=2):
    last_err = None
    for i in range(retries + 1):
        try:
            note_request("GET", url)
            async with plugin_http_session(config) as s:
                async with s.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as r:
                    return await r.text(), r
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as e:
            last_err = e
            if i < retries:
                await asyncio.sleep(0.5 * (i + 1))
                continue
            raise
    raise last_err


async def http_post(config, url, headers, data=None, timeout=10, retries=0):
    """默认只发一次：写操作结果不明时不能重试，否则可能发两遍。"""
    last_err = None
    for i in range(retries + 1):
        try:
            note_request("POST", url)
            async with plugin_http_session(config) as s:
                async with s.post(
                    url,
                    headers=headers,
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as r:
                    payload = await r.json(content_type=None)
                    if not isinstance(payload, dict):
                        raise WriteResultUnknownError(
                            f"POST response was not an object ({type(payload).__name__})"
                        )
                    if type(payload.get("code")) is not int:
                        raise WriteResultUnknownError("POST response has no valid result code")
                    return payload, r
        except WriteResultUnknownError:
            raise
        except (ValueError, UnicodeError) as e:
            raise WriteResultUnknownError(
                f"POST response could not be decoded ({type(e).__name__})"
            ) from e
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            last_err = e
            if i < retries:
                await asyncio.sleep(0.5 * (i + 1))
                continue
            raise WriteResultUnknownError(
                f"POST result unknown after {type(e).__name__}"
            ) from e
    raise WriteResultUnknownError(
        f"POST result unknown after {type(last_err).__name__}"
    ) from last_err


def _forget_wbi_keys(bot):
    """收到风控码：wbi key 多半换了，清缓存让下次重新拉。"""
    if getattr(bot, "_wbi_keys_cache", None):
        from astrbot.api import logger  # 本文件不在导入时依赖 AstrBot

        bot._wbi_keys_cache = None
        logger.warning("[BiliBot] 收到 -352 风控码，已清除 WBI key 缓存待下次重签")


class BiliHttp(HostView):
    """插件上用的那一份：配置每次现读；``_http_get`` 收到风控码时清插件上的 wbi 密钥缓存。"""

    HOST_API = frozenset({"config"})

    def _has_cookie(self):
        return has_cookie(self.config)

    def _headers(self):
        return bili_headers(self.config)

    async def _http_get(self, url, headers=None, params=None, timeout=10, retries=2):
        return await http_get(
            self.config, url, headers or self._headers(), params=params, timeout=timeout, retries=retries,
            on_risk_control=lambda: _forget_wbi_keys(self._host),
        )

    async def _http_post(self, url, headers=None, data=None, timeout=10, retries=0):
        """POST once by default; an ambiguous transport result must not be retried."""
        return await http_post(self.config, url, headers or self._headers(), data=data, timeout=timeout, retries=retries)

    async def _http_get_text(self, url, headers=None, params=None, timeout=10, retries=2):
        return await http_get_text(self.config, url, headers or self._headers(), params=params, timeout=timeout, retries=retries)
