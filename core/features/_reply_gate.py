"""回不回：低价值和广告过滤、每日上限、兴趣筛选、B站能用哪些工具。评论和私信共用。"""
from __future__ import annotations

import re
import json

from astrbot.api import logger
from ..infra.host_view import HostView


class ReplyGate(HostView):
    HOST_API = frozenset({"bili_http", "bili_videos", "models", "reply_log", "schedule_rules", "settings"})

    # 评论区是不是 Bot 自己的，查过的记在这里，同一个评论区不重复请求
    _OWN_CONTENT_CACHE_SIZE = 500

    _AD_PATTERNS = (
        r"(?:加|\+)(?:微|v|vx|q|qq)",
        r"(?:微信|vx|qq)[:：]?\s*[a-z0-9_-]{4,}",
        r"(?:代刷|代充|返利|兼职|引流|推广|低价|免费领取|进群|私聊我|联系我)",
        r"(?:https?://|www\.|t\.me/)",
        r"[群裙]\s*[:：]?\s*\d{5,}",
    )
    _INVALID_AD_PATTERNS_LOGGED = set()

    @staticmethod
    def _normalized_interaction_text(content):
        return re.sub(r"[\W_]+", "", str(content or "").lower(), flags=re.UNICODE)

    @classmethod
    def _matches_ad_pattern(cls, text):
        """匹配广告规则；单条规则损坏时跳过，避免拖垮整个轮询。"""
        for pattern in cls._AD_PATTERNS:
            try:
                if re.search(pattern, text, re.I):
                    return True
            except re.error as exc:
                if pattern not in cls._INVALID_AD_PATTERNS_LOGGED:
                    cls._INVALID_AD_PATTERNS_LOGGED.add(pattern)
                    logger.error(f"[BiliBot] 已跳过无效广告过滤规则 {pattern!r}: {exc}")
        return False

    async def _is_own_content(self, oid, comment_type) -> bool:
        """这个评论区是不是在 Bot 自己的视频或动态下面。查不到当作不是，下次再查。"""
        bot_mid = str(self.settings.get("DEDE_USER_ID") or "").strip()
        if not bot_mid or not oid:
            return False
        try:
            kind = int(comment_type)
        except (TypeError, ValueError):
            return False
        key = (str(oid), kind)
        cache = getattr(self, "_own_content_cache", None)
        if cache is None:
            cache = self._own_content_cache = {}
        if key in cache:
            return cache[key]
        try:
            author = await self._content_author_mid(oid, kind)
        except Exception as exc:
            logger.debug(f"[BiliBot] 查评论区是谁的失败（oid={oid} type={kind}）：{exc}")
            author = ""
        if not author:
            return False
        if len(cache) >= self._OWN_CONTENT_CACHE_SIZE:
            cache.clear()
        cache[key] = author == bot_mid
        return cache[key]

    async def _content_author_mid(self, oid, kind) -> str:
        """评论区所在内容的作者 UID：1 是视频，11 是图文相簿，17 是动态；别的类型不查。"""
        if kind == 1:
            info = await self.bili_videos._get_video_info(oid)
            return str((info or {}).get("owner_mid") or "")
        if kind == 17:
            d, _ = await self.bili_http._http_get(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail",
                params={"id": oid, "timezone_offset": -480},
            )
            if isinstance(d, dict) and d.get("code") == 0:
                item = (d.get("data") or {}).get("item") or {}
                return str(((item.get("modules") or {}).get("module_author") or {}).get("mid") or "")
            return ""
        if kind == 11:
            for url in (
                f"https://api.bilibili.com/x/dynamic/feed/draw/doc_detail?doc_id={oid}",
                f"https://api.vc.bilibili.com/link_draw/v1/doc/detail?doc_id={oid}",
            ):
                d, _ = await self.bili_http._http_get(url)
                if isinstance(d, dict) and d.get("code") == 0:
                    user = (d.get("data") or {}).get("user") or {}
                    mid = str(user.get("uid", user.get("mid", "")) or "")
                    if mid:
                        return mid
        return ""

    def _today_reply_count(self, channel="comment"):
        return self.reply_log.today_count(channel)

    def _daily_reply_limit_reached(self, channel="comment"):
        limit_kind = "private" if channel == "private" else "reply"
        limit = self.schedule_rules._autonomous_limit_max(limit_kind)
        return bool(limit and self._today_reply_count(channel) >= limit)

    def _interaction_filter_reason(self, content, channel="comment"):
        text = str(content or "").strip()
        compact = self._normalized_interaction_text(text)
        if not compact:
            return "empty_or_symbol_only"
        if self.settings.get("FILTER_LOW_VALUE_MESSAGES"):
            low_value = {"顶", "路过", "来了", "打卡", "哈哈", "哈哈哈", "呵呵", "哦", "嗯", "6", "666", "1", "支持", "关注了"}
            if compact in low_value or len(compact) <= 1:
                return "low_value_message"
            if re.fullmatch(r"(.)\1{3,}", compact):
                return "repeated_character_spam"
        if self.settings.get("FILTER_AD_MESSAGES"):
            if self._matches_ad_pattern(text):
                return "advertisement_or_contact_spam"
        if self.settings.get("FILTER_DUPLICATE_MESSAGES"):
            for earlier in self.reply_log.recent_contents(channel, 120):
                if self._normalized_interaction_text(earlier) == compact:
                    return "exact_duplicate_message"
        return None

    def _allowed_bili_tool_names(self):
        supported = {
            "bili_up_info", "get_up_info", "bili_video_search", "search_bilibili",
            "bili_search_and_watch", "watch_video",
            "check_following_updates", "check_following_live", "get_bangumi_info",
            "get_bangumi_trending", "get_bangumi_timeline", "get_bangumi_updates",
            "bili_video_info", "bili_bangumi_search", "bili_memory",
            "web_search",
        }
        if not self.settings.get("BILI_ALLOW_SEARCH_TOOLS"):
            return set()
        configured = self.settings.get("BILI_TOOL_ALLOWLIST")
        allowed = {str(name).strip().lower() for name in configured if str(name).strip()} if isinstance(configured, list) else set()
        # Backend-supported read-only tools are the absolute ceiling. AstrBot/QQ
        # commands, filesystem and shell tools are never reachable from B站 input.
        return supported & allowed if self.settings.get("BILI_TOOL_ISOLATION_ENABLED") else supported

    async def _should_reply_by_interest(self, content, username, channel="comment"):
        if not self.settings.get("ENABLE_INTEREST_BASED_REPLY"):
            return True, "interest_filter_disabled"
        if channel == "private" and not self.settings.get("INTEREST_APPLY_TO_PRIVATE"):
            return True, "private_interest_filter_disabled"
        prompt = f"""判断一个B站角色是否值得回复下面这条{('私信' if channel == 'private' else '评论')}。
只输出严格 JSON：{{"reply":true,"reason":"不超过30字"}}。
选择规则：{str(self.settings.get("INTEREST_SELECTION_PROMPT"))[:900]}
用户：{str(username)[:40]}
内容：{str(content)[:800]}
不要因为礼貌就默认回复；低价值、广告、复读、无交流空间的内容应为 false。"""
        try:
            raw = str(await self.models._llm_call(prompt, max_tokens=120) or "").strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S)
            match = re.search(r"\{.*?\}", raw, re.S)
            data = json.loads(match.group(0) if match else raw)
            if isinstance(data, dict) and isinstance(data.get("reply"), bool):
                return data["reply"], str(data.get("reason") or "模型兴趣判断")[:80]
        except Exception as exc:
            logger.debug(f"[BiliBot] 兴趣筛选解析失败，使用确定性回退：{exc}")
        compact = self._normalized_interaction_text(content)
        fallback = len(compact) >= 8 or bool(re.search(r"[?？]|怎么|为什么|觉得|喜欢|推荐|请问", str(content)))
        return fallback, "deterministic_interest_fallback"
