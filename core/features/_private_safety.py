"""私信安全：危险外链和色情引流判断（只解析文字，不访问目标地址），以及谁受保护、隔离怎么记账。"""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from ..infra.host_view import HostView


_URL_RE = re.compile(
    r"""(?ix)
    (?:
        (?:https?|hxxps?)://[^\s<>"'，。！？、]+
        |
        www\.[^\s<>"'，。！？、]+
        |
        (?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+
        (?:com|net|org|cn|tv|cc|me|xyz|top|vip|site|link|app|io|info|live)
        (?:/[^\s<>"'，。！？、]*)?
    )
    """
)
_STRONG_ADULT_RE = re.compile(
    r"(?i)(裸聊|约炮|援交|卖片|色情网站|黄色网站|成人网站|成人视频|"
    r"无码视频|无码视频|看片地址|看片链接|未成年.{0,8}(?:裸照|私密视频))"
)
_LINKED_ADULT_RE = re.compile(
    r"(?i)(色情|黄色|成人|福利姬|福利群|资源群|私密视频|色图|涩图|裸照|成人视频|看片)"
)
_ADULT_DOMAIN_MARKERS = (
    "porn", "sex", "xxx", "hentai", "jav", "xvideo", "onlyfans", "91porn", "麻豆",
)


@dataclass(frozen=True)
class PrivateSafetyDecision:
    should_block: bool
    reason: str = ""
    urls: tuple = ()


def _normalize_private_text(text):
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", value)
    value = re.sub(
        r"(?i)\bhxxps?://",
        lambda match: match.group(0).replace("xx", "tt"),
        value,
    )
    value = re.sub(r"[\[\(\{]\s*\.\s*[\]\)\}]", ".", value)
    value = value.replace("。", ".").replace("．", ".").replace("｡", ".")
    value = re.sub(r"(?<=[A-Za-z0-9])点(?=[A-Za-z]{2,12}\b)", ".", value)
    return value


def extract_private_urls(text):
    normalized = _normalize_private_text(text)
    urls = []
    seen = set()
    for match in _URL_RE.finditer(normalized):
        candidate = match.group(0).rstrip(".,;:!?)]}")
        if candidate.lower().startswith("www."):
            candidate = "https://" + candidate
        elif "://" not in candidate:
            candidate = "https://" + candidate
        if candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
    return urls


def _private_hostname(url):
    try:
        return (urlsplit(url).hostname or "").strip(".").lower()
    except ValueError:
        return ""


def _is_trusted_private_host(host, trusted_domains):
    for item in trusted_domains:
        trusted = str(item or "").strip().strip(".").lower()
        if trusted and (host == trusted or host.endswith("." + trusted)):
            return True
    return False


def assess_private_message(text, trusted_domains=None):
    """危险外链或色情引流直接判为应隔离；不会访问消息里的网址。"""
    normalized = _normalize_private_text(text)
    urls = extract_private_urls(normalized)
    trusted = trusted_domains or ["bilibili.com", "b23.tv"]

    if _STRONG_ADULT_RE.search(normalized):
        return PrivateSafetyDecision(True, "疑似色情或成人引流内容", tuple(urls))

    for url in urls:
        host = _private_hostname(url)
        if not host:
            return PrivateSafetyDecision(True, "无法识别目标域名的链接", tuple(urls))
        try:
            ipaddress.ip_address(host)
            return PrivateSafetyDecision(True, f"不可信 IP 链接：{host}", tuple(urls))
        except ValueError:
            pass
        if any(marker in host for marker in _ADULT_DOMAIN_MARKERS):
            return PrivateSafetyDecision(True, f"疑似色情域名：{host}", tuple(urls))
        if not _is_trusted_private_host(host, trusted):
            return PrivateSafetyDecision(True, f"未信任的外部链接：{host}", tuple(urls))

    if urls and _LINKED_ADULT_RE.search(normalized):
        return PrivateSafetyDecision(True, "链接伴随疑似色情引流内容", tuple(urls))
    return PrivateSafetyDecision(False, urls=tuple(urls))


class PrivateSafety(HostView):
    """命中安全规则后：谁受保护只记日志，其他人记进黑名单。"""

    HOST_API = frozenset({
        "affection", "blocklist", "settings",
    })

    def _private_sender_protected(self, mid):
        uid = str(mid or "").strip()
        protected = {
            str(self.settings.get("OWNER_MID") or "").strip(),
            str(self.settings.get("DEDE_USER_ID") or "").strip(),
        }
        for key in ("PRIVATE_MESSAGE_BLOCK_WHITELIST_UIDS", "BLOCK_WHITELIST_UIDS"):
            protected.update(
                str(item or "").strip()
                for item in (self.settings.get(key) or [])
            )
        protected.discard("")
        return uid in protected

    def _record_private_block(self, message, reason, blocked):
        mid = str(message.get("sender_uid") or "")
        self.blocklist.add(mid, {
            "username": message.get("username", ""),
            "reason": reason,
            "last_comment": message.get("content", ""),
            "last_message": message.get("content", ""),
            "source": "private_message",
            "score": self.affection.score(mid),
            # None 表示远端结果未知，不能落盘成确定失败。
            "api_blocked": None if blocked is None else bool(blocked),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
