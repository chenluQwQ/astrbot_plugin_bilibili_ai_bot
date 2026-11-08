"""WBI 签名：key 缓存在 transport 上，HTTP 层收到风控码时会清掉。"""
from __future__ import annotations

import time
import hashlib
from functools import reduce

from astrbot.api import logger

from ...config import (
    BILI_NAV_URL, MIXIN_KEY_ENC_TAB,
)
from .base import BiliApi


class BiliWbi(BiliApi):
    async def _get_wbi_keys(self):
        # wbi key 官方约一天一换，缓存 6 小时避免每次签名都请求 nav 接口
        cached = getattr(self._t, "_wbi_keys_cache", None)
        if cached and time.time() - cached[0] < 6 * 3600:
            return cached[1], cached[2]
        d, _ = await self._http_get(BILI_NAV_URL)
        d = d["data"]["wbi_img"]
        ik = d["img_url"].rsplit("/", 1)[1].split(".")[0]
        sk = d["sub_url"].rsplit("/", 1)[1].split(".")[0]
        self._t._wbi_keys_cache = (time.time(), ik, sk)
        return ik, sk

    def _get_mixin_key(self, orig):
        return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]

    async def sign_wbi_params(self, params):
        try:
            ik, sk = await self._get_wbi_keys()
            mk = self._get_mixin_key(ik + sk)
            params["wts"] = int(time.time())
            params = dict(sorted(params.items()))
            params["w_rid"] = hashlib.md5(("&".join(f"{k}={v}" for k, v in params.items()) + mk).encode()).hexdigest()
            return params
        except Exception as e:
            logger.warning(f"[BiliBot] WBI 签名失败，将以未签名参数请求（接口大概率返回 -352）: {e}")
            return params

    async def _wbi_get(self, url, params):
        """签名并请求 wbi 接口；遇 -352 时清缓存、用原始参数重新签名并立即重试一次。"""
        raw = dict(params)
        d, r = await self._http_get(url, params=await self.sign_wbi_params(dict(raw)))
        if isinstance(d, dict) and d.get("code") == -352:
            self._t._wbi_keys_cache = None
            logger.warning("[BiliBot] wbi 接口返回 -352，重新签名重试一次")
            d, r = await self._http_get(url, params=await self.sign_wbi_params(dict(raw)))
        return d, r
