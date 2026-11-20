"""和 B站说话的底层：带 Cookie 的请求头，GET 和 POST。"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

import aiohttp

from ...config import USER_AGENT
from ...network import plugin_http_session
from ...runtime import WriteResultUnknownError
from ...settings import Settings
from .request_log import note_request, recent_request_counts
from ..host_view import HostView

# B站的风控码：通常是 wbi key 轮换了或签名失效。
RISK_CONTROL_CODE = -352
# B站的账号级请求频控码：命中后私信、评论通知等后台轮询同步暂停冷却。
RATE_LIMIT_CODE = -509
RATE_LIMIT_BASE_SECONDS = 300.0
RATE_LIMIT_MAX_SECONDS = 3600.0
RATE_LIMIT_BURST_WINDOW = 10.0
RATE_LIMIT_RESET_WINDOW = 1800.0


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


async def http_get(
    config, url, headers, params=None, timeout=10, retries=2,
    on_risk_control=None, on_rate_limit=None,
):
    """GET 可以重试；收到风控码或频控码时调对应回调，结果照常返回。"""
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
                    if isinstance(d, dict):
                        code = d.get("code")
                        if on_risk_control and code == RISK_CONTROL_CODE:
                            on_risk_control()
                        if on_rate_limit and code == RATE_LIMIT_CODE:
                            on_rate_limit()
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


async def http_post(config, url, headers, data=None, timeout=10, retries=0, on_rate_limit=None):
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
                    if on_rate_limit and payload.get("code") == RATE_LIMIT_CODE:
                        on_rate_limit()
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


def _source_label(source):
    text = str(source or "").strip()
    if not text:
        return "bilibili"
    if "://" in text:
        return urlsplit(text).path or text
    return text


class BiliHttp(HostView):
    """插件上用的那一份：配置每次现读；收到 -352 清 wbi 缓存，收到 -509 维护账号级共享冷却。"""

    HOST_API = frozenset({"config"})

    def __init__(self, host):
        super().__init__(host)
        self._rate_limit_until = 0.0
        self._rate_limit_seconds = 0.0
        self._rate_limit_at = 0.0
        self._rate_limit_seq = 0
        self._rate_limit_source = ""

    @property
    def rate_limit_until(self) -> float:
        return self._rate_limit_until

    @property
    def rate_limit_seq(self) -> int:
        return self._rate_limit_seq

    def rate_limit_remaining(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else float(now)
        return max(0.0, self._rate_limit_until - current)

    def note_rate_limit(self, source: str = "", *, min_seconds: float = 0.0) -> float:
        """记录一次账号级 -509 频控，供私信、评论通知等轮询模块共享退避。"""
        now = time.monotonic()
        floor = max(0.0, float(min_seconds or 0.0))
        label = _source_label(source)
        # 同一波紧邻请求一起撞到 -509 时不重复翻倍也不刷屏，仅按需要延长截止时间。
        if self._rate_limit_at > 0 and now - self._rate_limit_at < RATE_LIMIT_BURST_WINDOW:
            target = min(RATE_LIMIT_MAX_SECONDS, max(self._rate_limit_seconds, RATE_LIMIT_BASE_SECONDS, floor))
            self._rate_limit_seconds = target
            self._rate_limit_until = max(self._rate_limit_until, now + target)
            return self.rate_limit_remaining(now)

        if self._rate_limit_at > 0 and now - self._rate_limit_at >= RATE_LIMIT_RESET_WINDOW:
            self._rate_limit_seconds = 0.0

        base = RATE_LIMIT_BASE_SECONDS if self._rate_limit_seconds <= 0 else self._rate_limit_seconds * 2
        backoff = min(RATE_LIMIT_MAX_SECONDS, max(base, floor))
        self._rate_limit_seconds = backoff
        self._rate_limit_at = now
        self._rate_limit_until = max(self._rate_limit_until, now + backoff)
        self._rate_limit_seq += 1
        self._rate_limit_source = label

        from astrbot.api import logger  # 本文件不在导入时依赖 AstrBot

        logger.warning(
            f"[BiliBot] 收到 B站账号级 -509 频控（{label}），"
            f"共享退避 {int(backoff)} 秒（评论/私信轮询将同步暂停）"
        )
        recent = recent_request_counts(300)
        if recent:
            top = "，".join(f"{key} ×{count}" for key, count in recent.most_common(8))
            logger.info(
                f"[BiliBot] 触发 -509 前 5 分钟共 {sum(recent.values())} 次 B站请求：{top}"
            )
        return self.rate_limit_remaining(now)

    def _has_cookie(self):
        return has_cookie(self.config)

    def _headers(self):
        return bili_headers(self.config)

    async def _http_get(self, url, headers=None, params=None, timeout=10, retries=2):
        return await http_get(
            self.config, url, headers or self._headers(), params=params, timeout=timeout, retries=retries,
            on_risk_control=lambda: _forget_wbi_keys(self._host),
            on_rate_limit=lambda: self.note_rate_limit(url),
        )

    async def _http_post(self, url, headers=None, data=None, timeout=10, retries=0):
        """POST once by default; an ambiguous transport result must not be retried."""
        return await http_post(
            self.config, url, headers or self._headers(), data=data, timeout=timeout, retries=retries,
            on_rate_limit=lambda: self.note_rate_limit(url),
        )

    async def _http_get_text(self, url, headers=None, params=None, timeout=10, retries=2):
        return await http_get_text(self.config, url, headers or self._headers(), params=params, timeout=timeout, retries=retries)
