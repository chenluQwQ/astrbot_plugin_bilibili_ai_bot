"""从 QQ 消息里认出 B站视频：摊平小程序和 CQ JSON、找 BV/av 号、展开短链（只请求 B站自己的域名）。"""
from __future__ import annotations

import html
import json
import re
from urllib.parse import parse_qs, unquote, urlparse, urljoin

import aiohttp
from astrbot.api import logger
from ...network import plugin_http_session
from ..host_view import HostView


class ShareLinks(HostView):
    """全部只读，不带登录 Cookie。"""

    HOST_API = frozenset({
        "config",
    })

    BVID_RE = re.compile(r"\b(BV[0-9A-Za-z]{10,})\b")

    AV_RE = re.compile(r"(?:\bav|aid=)(\d+)\b", re.IGNORECASE)

    URL_RE = re.compile(r"https?://[^\s\]\[\)\(\"'<>]+", re.IGNORECASE)

    CQ_JSON_RE = re.compile(r"\[CQ:json,data=(.*?)\]", re.IGNORECASE | re.DOTALL)

    MINIAPP_KEYS = (
        "url", "jumpUrl", "jump_url", "qqdocurl", "preview", "sourceUrl",
        "pagepath", "pagePath", "webUrl", "web_url", "shareUrl", "share_url",
        "title", "desc", "content", "text", "summary",
    )

    def _collect_share_text(self, event, include_reply=False):
        parts = [event.message_str or ""]
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if raw:
                parts.append(str(raw))
        except Exception:
            pass
        try:
            for comp in getattr(event.message_obj, "message", []) or []:
                is_reply = (
                    comp.__class__.__name__.lower() == "reply"
                    or (
                        isinstance(comp, dict)
                        and str(comp.get("type", "")).lower() == "reply"
                    )
                )
                if is_reply and not include_reply:
                    continue
                parts.append(str(comp))
                if isinstance(comp, dict):
                    parts.extend(self._flatten_share_payload(comp))
                    continue
                attrs = (*self.MINIAPP_KEYS, "data", "meta")
                if include_reply:
                    attrs = (*attrs, "message_str")
                for attr in attrs:
                    val = getattr(comp, attr, None)
                    if val:
                        parts.extend(self._flatten_share_payload(val))
        except Exception:
            pass
        return "\n".join(p for p in parts if p)

    def _flatten_share_payload(self, payload):
        """把 QQ 小程序/CQ JSON 里的嵌套字段尽量摊平成可搜索文本。"""
        out = []
        if payload is None:
            return out
        if isinstance(payload, (list, tuple, set)):
            for item in payload:
                out.extend(self._flatten_share_payload(item))
            return out
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in self.MINIAPP_KEYS or isinstance(value, (dict, list, tuple)):
                    out.extend(self._flatten_share_payload(value))
                elif isinstance(value, str):
                    out.append(value)
            return out

        text = str(payload)
        out.append(text)
        for candidate in self._share_text_variants(text):
            stripped = candidate.strip()
            if not stripped:
                continue
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    out.extend(self._flatten_share_payload(json.loads(stripped)))
                except Exception:
                    pass
        return out

    def _share_text_variants(self, text):
        text = text or ""
        variants = []

        def add(value):
            if value and value not in variants:
                variants.append(value)

        add(text)
        add(html.unescape(text))
        add(text.replace("\\/", "/"))
        add(html.unescape(text).replace("\\/", "/"))
        try:
            add(unquote(text))
            add(unquote(html.unescape(text).replace("\\/", "/")))
        except Exception:
            pass
        for m in self.CQ_JSON_RE.finditer(text):
            data = m.group(1)
            add(data)
            add(html.unescape(data).replace("\\/", "/"))
            try:
                add(unquote(html.unescape(data).replace("\\/", "/")))
            except Exception:
                pass
        return variants

    def _normalized_share_blob(self, text):
        queue = list(self._share_text_variants(text))
        seen = set()
        parts = []
        while queue:
            item = queue.pop(0)
            if not item or item in seen:
                continue
            seen.add(item)
            parts.append(item)
            for extra in self._flatten_share_payload(item):
                if extra not in seen:
                    queue.extend(self._share_text_variants(extra))
        return "\n".join(parts)

    def _clean_share_url(self, url):
        text = unquote(html.unescape((url or "").replace("\\/", "/")))
        return text.rstrip("。，、,;；:：)）]】}>\\\"'")

    def _target_from_url(self, url):
        text = self._clean_share_url(url)
        m = self.BVID_RE.search(text)
        if m:
            return {"bvid": m.group(1), "source": "url", "url": text}
        m = self.AV_RE.search(text)
        if m:
            return {"aid": int(m.group(1)), "source": "url", "url": text}
        try:
            parsed = urlparse(text)
            qs = parse_qs(parsed.query)
            for key in ("bvid", "bv", "video_id"):
                value = (qs.get(key) or [""])[0]
                m = self.BVID_RE.search(value)
                if m:
                    return {"bvid": m.group(1), "source": "url", "url": text}
            for key in ("aid", "av"):
                value = (qs.get(key) or [""])[0]
                if str(value).isdigit():
                    return {"aid": int(value), "source": "url", "url": text}
        except Exception:
            pass
        return None

    @staticmethod
    def _is_allowed_share_host(url):
        """严格主机校验：仅允许 B站官方域名，杜绝子串匹配绕过。

        旧实现用 ``host in url.lower()`` 判断，``http://127.0.0.1/b23.tv/x``
        或 ``b23.tv.evil.com`` 都会通过。这里改为对 ``urlparse`` 出的
        hostname 做精确等值/后缀比较。
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() not in {"http", "https"} or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
                return False
            hostname = (parsed.hostname or "").lower().rstrip(".")
        except Exception:
            return False
        if hostname in ("b23.tv", "bili2233.cn", "bilibili.com"):
            return True
        return hostname.endswith(".bilibili.com")

    async def _resolve_share_url(self, url):
        # Check every hop BEFORE making the next request; never send login cookies.
        original = url
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BiliBot",
            "Referer": "https://www.bilibili.com",
            "Accept": "text/html,application/xhtml+xml",
        }
        try:
            async with plugin_http_session(self.config) as session:
                for _ in range(5):
                    if not self._is_allowed_share_host(url):
                        return original
                    async with session.get(url, headers=headers, allow_redirects=False,
                                           timeout=aiohttp.ClientTimeout(total=10)) as response:
                        current = str(response.url)
                        if not self._is_allowed_share_host(current):
                            return original
                        if getattr(response, "status", 200) not in {301, 302, 303, 307, 308}:
                            return current
                        location = response.headers.get("Location", "")
                        target = urljoin(current, location)
                        if not location or not self._is_allowed_share_host(target):
                            return original
                        url = target
        except Exception as exc:
            logger.debug(f"[BiliBot] B站短链展开失败: {type(exc).__name__}")
        return original

    async def _extract_bili_share_target(self, text):
        blob = self._normalized_share_blob(text)
        m = self.BVID_RE.search(blob)
        if m:
            return {"bvid": m.group(1), "source": "bvid"}
        m = self.AV_RE.search(blob)
        if m:
            return {"aid": int(m.group(1)), "source": "aid"}

        urls = []
        for candidate in self._share_text_variants(blob):
            for url in self.URL_RE.findall(candidate):
                clean = self._clean_share_url(url)
                if clean not in urls:
                    urls.append(clean)
        for url in urls:
            # 严格主机校验：伪造路径（如 127.0.0.1/b23.tv/x）或仿冒域名
            # （b23.tv.evil.com）一律不请求，避免 Cookie 外带与 SSRF。
            if not self._is_allowed_share_host(url):
                continue
            direct = self._target_from_url(url)
            if direct:
                return direct
            if any(host in url.lower() for host in ("b23.tv", "bili2233.cn")):
                resolved = await self._resolve_share_url(url)
                # 重定向后的最终地址也要过主机校验，防止短链跳到站外。
                if not self._is_allowed_share_host(resolved):
                    continue
                target = self._target_from_url(resolved)
                if target:
                    target["url"] = resolved
                    return target
        return None
