"""各组接口的共同底子。

``transport`` 提供 ``config``、``settings`` 和带 Cookie 的请求（``_http_get`` 这些）。
它就是插件对象本身。
"""
from __future__ import annotations


class BiliApi:
    def __init__(self, transport):
        self._t = transport

    @property
    def config(self):
        return self._t.config

    @property
    def settings(self):
        return self._t.settings

    def _has_cookie(self):
        return self._t.bili_http._has_cookie()

    def _headers(self):
        return self._t.bili_http._headers()

    async def _http_get(self, *args, **kwargs):
        return await self._t.bili_http._http_get(*args, **kwargs)

    async def _http_post(self, *args, **kwargs):
        return await self._t.bili_http._http_post(*args, **kwargs)

    async def _http_get_text(self, *args, **kwargs):
        return await self._t.bili_http._http_get_text(*args, **kwargs)

    async def _wbi_get(self, url, params):
        return await self._t.bili_wbi._wbi_get(url, params)
