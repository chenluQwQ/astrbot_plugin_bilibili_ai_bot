"""B站私信轮询、回复与安全隔离。

仅处理个人会话中的纯文本和 B站视频分享卡片。首次开启时建立游标，
不会补回历史私信；危险链接判断只解析文字，不访问目标地址。
"""
import asyncio
import ipaddress
import json
import os
import random
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from astrbot.api import logger

from .config import (
    AFFECTION_FILE,
    BILI_PRIVATE_MESSAGES_URL,
    BILI_PRIVATE_SESSIONS_URL,
    DATA_DIR,
    LEVEL_NAMES,
    PRIVATE_MESSAGE_STATE_FILE,
    REPLY_LOG_FILE,
)
from .runtime import ActionRequest, EventState, InboundEvent


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


class PrivateMessageMixin:
    """轮询新私信，并复用当前人格、记忆、画像和好感度生成回复。"""

    def _private_runtime_event(self, message):
        """把持久化私信转换为统一事件，供排序、领取和重试共用。"""

        mid = str(message.get("sender_uid") or "")
        owner_check = getattr(self, "_is_owner", None)
        is_admin = (
            bool(owner_check(mid))
            if callable(owner_check)
            else mid == str(self.config.get("OWNER_MID", "") or "").strip()
        )
        return InboundEvent(
            source="private",
            event_id=str(message.get("msg_key") or message.get("msg_seqno") or ""),
            actor_id=mid,
            actor_name=message.get("username", ""),
            content=message.get("content", ""),
            conversation_id=self._private_conversation_thread_id(mid),
            account_id=str(self.config.get("DEDE_USER_ID", "") or ""),
            occurred_at=float(message.get("timestamp") or 0),
            metadata={
                "content_type": message.get("content_type", "text"),
                "is_admin": is_admin,
                "conversation_active": True,
                "retry": int(message.get("retry_count") or 0) > 0,
            },
        )

    def _private_message_headers(self):
        return {
            **self._headers(),
            "Referer": "https://message.bilibili.com/",
            "Origin": "https://message.bilibili.com",
        }

    @staticmethod
    def _default_private_message_state(account_uid=""):
        return {
            "initialized": False,
            "initialized_at": int(time.time()),
            "account_uid": str(account_uid or ""),
            "device_id": str(uuid.uuid4()).upper(),
            "sessions": {},
            "processed_keys": [],
            "pending_messages": [],
            "failed_messages": [],
            "conversation_ids": {},
        }

    def _private_conversation_thread_id(self, mid):
        """Return the current lightweight conversation id for one Bilibili UID."""
        uid = str(mid or "").strip()
        state = self._load_json(
            PRIVATE_MESSAGE_STATE_FILE,
            self._default_private_message_state(
                self.config.get("DEDE_USER_ID", "")
            ),
        )
        if not isinstance(state, dict):
            state = self._default_private_message_state(
                self.config.get("DEDE_USER_ID", "")
            )
        conversation_ids = state.setdefault("conversation_ids", {})
        conversation_id = str(conversation_ids.get(uid) or "").strip()
        return f"private:{uid}:{conversation_id}" if conversation_id else f"private:{uid}"

    def _reset_private_conversation(self, mid):
        """Start a new private-message context without deleting profile or long-term memory."""
        uid = str(mid or "").strip()
        state = self._load_json(
            PRIVATE_MESSAGE_STATE_FILE,
            self._default_private_message_state(
                self.config.get("DEDE_USER_ID", "")
            ),
        )
        if not isinstance(state, dict):
            state = self._default_private_message_state(
                self.config.get("DEDE_USER_ID", "")
            )
        conversation_id = uuid.uuid4().hex[:12]
        state.setdefault("conversation_ids", {})[uid] = conversation_id
        self._save_json(PRIVATE_MESSAGE_STATE_FILE, state)
        return f"private:{uid}:{conversation_id}"

    @staticmethod
    def _private_json_text(raw):
        if isinstance(raw, dict):
            return str(raw.get("content") or raw.get("text") or "").strip()
        if not isinstance(raw, str):
            return ""
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return str(parsed.get("content") or parsed.get("text") or "").strip()
        except (TypeError, ValueError):
            return raw.strip()
        return ""

    @classmethod
    def _private_message_content(cls, raw, msg_type):
        if msg_type == 1:
            return cls._private_json_text(raw), "text"
        if msg_type != 7:
            return "", ""
        if isinstance(raw, dict):
            parsed = raw
        else:
            try:
                parsed = json.loads(str(raw or ""))
            except (TypeError, ValueError):
                return "", ""
        if not isinstance(parsed, dict):
            return "", ""
        bvid = str(parsed.get("bvid") or "").strip()
        aid = str(parsed.get("id") or parsed.get("aid") or "").strip()
        title = str(
            parsed.get("title") or parsed.get("headline") or parsed.get("name") or ""
        ).strip()
        if re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            video_url = f"https://www.bilibili.com/video/{bvid}"
        elif aid.isdigit():
            video_url = f"https://www.bilibili.com/video/av{aid}"
        else:
            return "", ""
        prefix = f"[B站视频分享] {title}" if title else "[B站视频分享]"
        return f"{prefix}\n{video_url}", "video_share"

    def _private_sender_protected(self, mid):
        uid = str(mid or "").strip()
        protected = {
            str(self.config.get("OWNER_MID", "") or "").strip(),
            str(self.config.get("DEDE_USER_ID", "") or "").strip(),
        }
        for key in ("PRIVATE_MESSAGE_BLOCK_WHITELIST_UIDS", "BLOCK_WHITELIST_UIDS"):
            protected.update(
                str(item or "").strip()
                for item in (self.config.get(key, []) or [])
            )
        protected.discard("")
        return uid in protected

    @staticmethod
    def _private_shared_video_id(content):
        match = re.search(
            r"bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)",
            str(content or ""),
            flags=re.IGNORECASE,
        )
        return match.group(1) if match else ""

    def _private_reply_scope_allows(self, mid):
        uid = str(mid or "").strip()
        scope = str(
            self.config.get("PRIVATE_MESSAGE_REPLY_SCOPE", "owner") or "owner"
        ).strip().lower()
        owner = str(self.config.get("OWNER_MID", "") or "").strip()
        whitelist = {
            str(item or "").strip()
            for item in (self.config.get("PRIVATE_MESSAGE_REPLY_WHITELIST_UIDS", []) or [])
        }
        if scope == "all":
            return True
        if scope == "owner":
            return bool(uid and owner and uid == owner)
        if scope == "whitelist":
            return bool(uid and (uid == owner or uid in whitelist))
        return False

    @staticmethod
    def _private_bili_fallback_query(text, action):
        value = str(text or "").strip()
        if action == "up_info":
            patterns = (
                # 优先取“看看/查一下/搜一下”后、最近/最新前的名字。
                # 这样“霜序 试一下 看看泛式最新的几个视频”不会把称呼和口令吞进去。
                r"(?:看一下|看下|看看|查一下|查查|查询一下|查询|搜一下|搜搜|搜索一下|搜索|找一下|找找|了解一下)\s*([\w\-·\u4e00-\u9fff]{1,30}?)\s*(?:的)?\s*(?:最近|近期|最新)",
                r"(?:查|搜|搜索|看看|看下|了解)?(?:一下)?\s*([\w\-·\u4e00-\u9fff]{1,30}?)\s*(?:这个|这位)?\s*(?:UP主|up主|UP|up)",
                r"([\w\-·\u4e00-\u9fff]{1,30}?)(?:最近|近期)?(?:发了什么|有什么|有哪些)(?:新)?(?:视频|投稿)",
                r"([\w\-·\u4e00-\u9fff]{1,30}?)(?:最近|近期)?(?:有没有|有无|有)?(?:更新|新发|发新)(?:了)?(?:什么|哪些)?(?:视频|投稿)",
                r"([\w\-·\u4e00-\u9fff]{1,30}?)(?:的)?(?:最新|最近)(?:的)?(?:几个|几期|一些|什么|哪些)?(?:新)?(?:视频|投稿)",
            )
            for pattern in patterns:
                match = re.search(pattern, value)
                if match:
                    query = re.sub(r"^(?:请|麻烦|帮我|给我|一下)+", "", match.group(1))
                    if query:
                        return query[:80]
        value = re.sub(
            r"(?i)(?:请|麻烦|可以|能不能|能否|帮我|给我|一下|在B站|在b站|"
            r"B站|b站|哔哩哔哩|搜索|搜搜|搜|查找|查|找找|找|推荐|"
            r"你去|你能|看完|看看|看下|看一下|分析一下|点评一下|"
            r"有没有|有哪些|有什么|相关的|相关|几个|一些|一个|个|这个|这位|UP主|up主)",
            " ",
            value,
        )
        value = re.sub(r"(?:视频|投稿)[吗呢呀吧啊？?！!。]*$", "", value)
        value = re.sub(r"\s+", " ", value).strip(" ，,。？?！!：:")
        return value[:80]

    async def _classify_private_bili_request(self, content):
        """Route only clear Bilibili lookup/watch requests; ordinary PMs stay untouched."""
        text = str(content or "").strip()
        if not text or not self.config.get("PRIVATE_MESSAGE_BILI_SEARCH_ENABLED", True):
            return "none", ""
        has_subject = bool(
            re.search(r"(?i)(?:B站|b站|哔哩|视频|投稿|UP主|up主|\bUP\b|\bup\b)", text)
        )
        has_lookup = bool(
            re.search(r"(?:搜|找|查|推荐|看看|看下|看一下|分析|点评|有什么|有哪些|最近发|近期|更新|最新|新视频)", text)
        )
        if not ((has_subject and has_lookup) or re.search(r"(?:搜|搜索|查找)一下", text)):
            return "none", ""

        # UP 主“最近更新了吗”是高频、明确的站内查询。直接规则命中可避免
        # 意图模型把它误判成普通事实问题，继而落到通用联网搜索。
        explicit_up_update = bool(
            re.search(r"(?:最近|近期|最新).{0,12}(?:更新|新发|发新|视频|投稿)", text)
            and re.search(r"(?:视频|投稿|UP主|up主|\bUP\b|\bup\b)", text, re.IGNORECASE)
        )
        if explicit_up_update:
            query = self._private_bili_fallback_query(text, "up_info")
            if query:
                return "up_info", query

        prompt = f"""判断下面这条B站私信是否要求使用B站站内能力。只输出JSON，不要回答用户。
可选 action：
- video_search：搜索、查找或推荐相关视频，只需要列出候选
- search_and_watch：用户明确要求你亲自找一个相关视频并观看、分析或点评
- up_info：查询某个UP主是谁、有什么/最近发布了哪些视频或投稿
- none：普通聊天，或意图不明确

query 只保留用于B站搜索的关键词或UP主名字，不要包含“帮我、搜索、视频、UP主”等命令词。
用户私信：{json.dumps(text, ensure_ascii=False)}
输出：{{"action":"video_search|search_and_watch|up_info|none","query":"..."}}"""
        action = "none"
        query = ""
        try:
            raw = await self._llm_call(prompt)
            parsed = json.loads(self._repair_llm_json(raw or "{}"))
            if isinstance(parsed, dict):
                candidate = str(parsed.get("action") or "none").strip().lower()
                if candidate in {"video_search", "search_and_watch", "up_info", "none"}:
                    action = candidate
                query = str(parsed.get("query") or "").strip()[:80]
        except Exception as exc:
            logger.debug(f"[BiliBot] 私信B站查询意图解析失败，使用规则兜底: {exc}")

        if action == "none":
            if re.search(r"(?i)(?:UP主|up主|\bUP\b|\bup\b|投稿|最近发|最近更新|近期更新|最新视频)", text):
                action = "up_info"
            elif re.search(r"(?:你去|帮我)(?:找|搜)?.{0,12}(?:看完|看看|看一下|分析|点评)", text):
                action = "search_and_watch"
            elif has_lookup:
                action = "video_search"
        if action != "none" and not query:
            query = self._private_bili_fallback_query(text, action)
        if action != "none" and query:
            # 意图模型偶尔会把“看看/帮我查”等命令词留在 query 里，
            # 在调用 B站搜索 API 前统一剥掉，避免搜索“看看泛式”。
            query = re.sub(
                r"^(?:(?:请|麻烦|帮我|给我|试一下|测试一下|看一下|看下|看看|"
                r"查一下|查查|查询一下|查询|搜一下|搜搜|搜索一下|搜索|"
                r"找一下|找找|了解一下)[\s，,。？?！!：:]*)+",
                "",
                query,
            )
            if action == "up_info":
                query = re.sub(
                    r"\s*(?:这个|这位)?(?:UP主|up主|UP|up)[吗呢呀吧啊？?！!。]*$",
                    "",
                    query,
                    flags=re.IGNORECASE,
                )
            query = re.sub(r"\s+", " ", query).strip(" ，,。？?！!：:")[:80]
        return (action, query) if query else ("none", "")

    async def _build_private_bili_context(self, content):
        action, query = await self._classify_private_bili_request(content)
        if action == "none":
            return ""
        return await self._execute_private_bili_request(action, query)

    async def _execute_private_bili_request(self, action, query):
        """Execute one model-selected, read-only Bilibili lookup/watch request."""
        action = str(action or "").strip().lower()
        query = str(query or "").strip()[:100]
        if action not in {"video_search", "search_and_watch", "up_info"} or not query:
            return "后台B站查询参数不完整，未执行查询。"
        try:
            limit = max(
                1,
                min(5, int(self.config.get("PRIVATE_MESSAGE_BILI_SEARCH_LIMIT", 5) or 5)),
            )
        except (TypeError, ValueError):
            limit = 5

        if action in {"video_search", "search_and_watch"}:
            videos = await self.search_bilibili_videos(query, ps=limit)
            if not videos:
                logger.info(f"[BiliBot] 🔎 私信视频搜索无结果：{query}")
                return f"站内视频搜索“{query}”没有找到结果。请如实告诉用户，可以请其换个关键词。"
            lines = [f"站内视频搜索“{query}”结果："]
            for index, video in enumerate(videos, 1):
                bvid = str(video.get("bvid") or "")
                lines.append(
                    f"{index}. 《{video.get('title', '')}》｜UP：{video.get('author', '')}｜"
                    f"{bvid}｜播放：{video.get('play', 0)}｜时长：{video.get('duration', '')}｜"
                    f"https://www.bilibili.com/video/{bvid}"
                )
            if action == "search_and_watch":
                selected = videos[0]
                selected_bvid = str(selected.get("bvid") or "")
                if self.config.get("PRIVATE_MESSAGE_AUTO_WATCH_VIDEO", True) and selected_bvid:
                    watch_result = await self._watch_video_and_save_memory(
                        selected_bvid, memory_source="private_search"
                    )
                    if watch_result.get("ok"):
                        lines.extend((
                            "",
                            f"已按相关度选择第1条并实际看完：{selected_bvid}",
                            str(watch_result.get("message") or ""),
                        ))
                    else:
                        lines.extend((
                            "",
                            f"已选择第1条尝试观看，但读取失败：{selected_bvid}",
                            str(watch_result.get("message") or "未知错误"),
                        ))
                else:
                    lines.append("\n用户要求观看，但私信自动看片未开启；只能提供搜索结果，不要声称已经看过。")
            logger.info(f"[BiliBot] 🔎 私信执行 {action}：{query}（{len(videos)} 条）")
            return "\n".join(lines)

        users = await self.search_bilibili_users(query, ps=3)
        if not users:
            logger.info(f"[BiliBot] 🔎 私信UP查询无结果：{query}")
            return f"站内没有找到名为“{query}”的UP主。请如实告诉用户，可以请其提供准确昵称或UID。"
        normalized_query = re.sub(r"\s+", "", query).casefold()
        exact = [
            user for user in users
            if re.sub(r"\s+", "", str(user.get("uname") or "")).casefold() == normalized_query
        ]
        selected = (exact or users)[0]
        selected_mid = str(selected.get("mid") or "")
        info, videos = await asyncio.gather(
            self.get_up_info(selected_mid),
            self.get_up_recent_videos(selected_mid, ps=limit),
        )
        lines = [
            f"UP搜索“{query}”最相关结果：{selected.get('uname', '')}（UID：{selected_mid}，"
            f"粉丝：{selected.get('fans', 0)}，投稿数：{selected.get('videos', 0)}）"
        ]
        if info:
            lines.append(
                f"签名：{info.get('sign', '') or '未填写'}；认证："
                f"{info.get('official_title', '') or '无'}"
            )
        if videos:
            lines.append("最近投稿：")
            for index, video in enumerate(videos, 1):
                bvid = str(video.get("bvid") or "")
                created = int(video.get("created") or 0)
                date_text = datetime.fromtimestamp(created).strftime("%Y-%m-%d") if created else "日期未知"
                lines.append(
                    f"{index}. [{date_text}]《{video.get('title', '')}》｜{bvid}｜"
                    f"播放：{video.get('play', 0)}｜https://www.bilibili.com/video/{bvid}"
                )
        else:
            lines.append("最近投稿列表为空或暂时读取失败。")
        if not exact and len(users) > 1:
            candidates = "、".join(
                f"{user.get('uname', '')}(UID:{user.get('mid', '')})" for user in users[:3]
            )
            lines.append(f"昵称可能有歧义，本次按最相关结果查询；其他候选：{candidates}")
        logger.info(f"[BiliBot] 🔎 私信执行 up_info：{query} -> {selected_mid}")
        return "\n".join(lines)

    async def _execute_private_model_tool(
        self, tool_request, *, actor_id="", original_content=""
    ):
        """Execute the single read-only tool selected by the private-message reply model."""
        request = tool_request if isinstance(tool_request, dict) else {}
        name = str(request.get("name") or "none").strip().lower()
        query = str(request.get("query") or "").strip()[:100]
        if name == "none":
            return ""
        allowed = self._allowed_bili_tool_names()
        if name not in allowed:
            if self.config.get("BILI_TOOL_AUDIT_ENABLED", True):
                self._log_security_event("bili_tool_denied", "", "", query, f"工具 {name} 不在B站只读白名单")
            logger.warning(f"[BiliBot] B站工具请求被拒绝：{name}")
            return "该后台能力没有对B站端开放，未执行。"
        no_query_tools = {
            "check_following_updates", "check_following_live", "get_bangumi_trending",
            "get_bangumi_timeline", "get_bangumi_updates",
        }
        if not query and name not in no_query_tools:
            return "后台查询缺少关键词，未执行。"
        if self.config.get("BILI_TOOL_AUDIT_ENABLED", True):
            self._log_security_event("bili_tool_requested", "", "", query or name, f"只读工具 {name}")

        def compact_result(lines, limit=4800):
            return "\n".join(str(line) for line in lines if line is not None)[:limit]

        if name == "check_following_updates":
            results = await self.get_following_updates(limit=12)
            if not results:
                return "今天关注的 UP 主暂时没有读取到新动态。"
            lines = [f"今天关注列表有 {len(results)} 条更新："]
            for item in results[:12]:
                up_name = str(item.get("up_name") or "未知 UP")[:40]
                pub_time = str(item.get("pub_time") or "时间未知")[:30]
                if item.get("video_title"):
                    lines.append(f"- {up_name} 投稿《{str(item.get('video_title'))[:80]}》 {item.get('video_bvid', '')}（{pub_time}）")
                elif item.get("live_title"):
                    lines.append(f"- {up_name} 正在直播：{str(item.get('live_title'))[:80]}（{pub_time}）")
                elif item.get("text"):
                    lines.append(f"- {up_name} 发了动态：{str(item.get('text'))[:120]}（{pub_time}）")
                else:
                    lines.append(f"- {up_name} 有一条新动态（{pub_time}）")
            return compact_result(lines)

        if name == "check_following_live":
            results = await self.get_following_live()
            if not results:
                return "关注的人现在没有读取到正在直播的账号。"
            lines = [f"当前有 {len(results)} 个关注账号正在直播："]
            for item in results[:12]:
                lines.append(
                    f"- {str(item.get('uname') or '未知 UP')[:40]}：《{str(item.get('title') or '未命名直播')[:80]}》"
                    f"，分区 {str(item.get('area_name') or '未知')[:30]}，人气 {item.get('online', 0)}，{item.get('link', '')}"
                )
            return compact_result(lines)

        if name == "get_bangumi_info":
            match = re.search(r"\d+", query)
            if not match:
                return "番剧详情工具需要数字 season_id，未执行。"
            season_id = int(match.group(0))
            detail = await self.get_bangumi_detail(season_id=season_id)
            if not detail:
                return f"没有读取到 season_id={season_id} 的番剧详情。"
            lines = [
                f"《{detail.get('title') or '未命名番剧'}》",
                f"评分 {detail.get('score', 0)}（{detail.get('count', 0)} 人评），地区 {detail.get('areas') or '未知'}，类型 {detail.get('styles') or '未知'}",
                f"集数 {detail.get('total_ep', 0)}，{detail.get('new_ep_desc') or '暂无最新集说明'}",
                f"播放 {detail.get('stat_views', 0)}，弹幕 {detail.get('stat_danmakus', 0)}，追番 {detail.get('stat_favorites', 0)}",
            ]
            if detail.get("evaluate"):
                lines.append(f"简介：{str(detail.get('evaluate'))[:240]}")
            episodes = detail.get("episodes") or []
            if episodes:
                lines.append("最近剧集：" + " / ".join(str(ep.get("title") or "")[:35] for ep in episodes[-5:]))
            if detail.get("link"):
                lines.append(str(detail.get("link")))
            return compact_result(lines)

        if name == "get_bangumi_trending":
            season_type = 4 if "国创" in query else 1
            type_name = "国创" if season_type == 4 else "番剧"
            results = await self.get_bangumi_trending(season_type=season_type)
            if not results:
                return f"暂时没有读取到 B站{type_name}排行。"
            lines = [f"B站{type_name}热度排行："]
            for index, item in enumerate(results[:10], 1):
                score = f"，评分 {item.get('score')}" if item.get("score") else ""
                lines.append(f"{index}. 《{str(item.get('title') or '未命名')[:70]}》{score}，{str(item.get('new_ep_desc') or '暂无更新说明')[:60]}")
            return compact_result(lines)

        if name == "get_bangumi_timeline":
            results = await self.get_bangumi_timeline(day_before=2, day_after=3)
            if not results:
                return "暂时没有读取到近期新番时间表。"
            days = {}
            weekday_names = {0: "周日", 1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
            for item in results:
                key = f"{item.get('date', '')}（{weekday_names.get(item.get('day_of_week', 0), '')}）"
                days.setdefault(key, []).append(item)
            lines = ["近期新番时间表："]
            for day, items in list(days.items())[:6]:
                lines.append(day)
                for item in items[:8]:
                    status = "已更新" if item.get("published") else "待更新"
                    episode = f"第{item.get('ep_index')}话" if item.get("ep_index") else ""
                    lines.append(f"- [{status}]《{str(item.get('title') or '未命名')[:60]}》{episode}")
            return compact_result(lines)

        if name == "get_bangumi_updates":
            followed = await self._get_followed_bangumi(follow_status=2)
            if not followed:
                return "当前没有读取到正在追的番剧，或账号追番列表暂时不可用。"
            memory = self._load_bangumi_memory() if hasattr(self, "_load_bangumi_memory") else {}
            lines = [f"当前在追番剧共 {len(followed)} 部："]
            for item in followed[:20]:
                record = memory.get(str(item.get("season_id")), {}) if isinstance(memory, dict) else {}
                watched = len(record.get("episodes", [])) if isinstance(record, dict) else 0
                progress = f"已看 {watched} 集" if watched else "插件暂无观看记录"
                lines.append(f"- 《{str(item.get('title') or '未命名')[:70]}》{str(item.get('new_ep_index') or '')[:30]}，{progress}")
            return compact_result(lines)

        action_map = {
            "bili_up_info": "up_info",
            "get_up_info": "up_info",
            "bili_video_search": "video_search",
            "search_bilibili": "video_search",
            "bili_search_and_watch": "search_and_watch",
            "watch_video": "watch_direct",
        }
        if name in action_map:
            if not self.config.get("PRIVATE_MESSAGE_BILI_SEARCH_ENABLED", True):
                return "B站私信站内查询当前未开启，未执行。"
            action = action_map[name]
            if action == "up_info":
                # 即使模型偶尔留下“看看/最新视频”，也在真正调用API前做一次兜底清洗。
                extracted = self._private_bili_fallback_query(query, action)
                if extracted and re.search(r"(?:最近|近期|最新|视频|投稿|UP主|up主)", query):
                    query = extracted
            query = re.sub(
                r"^(?:(?:请|麻烦|帮我|给我|试一下|测试一下|看一下|看下|看看|"
                r"查一下|查查|查询一下|查询|搜一下|搜搜|搜索一下|搜索|"
                r"找一下|找找|了解一下)[\s，,。？?！!：:]*)+",
                "",
                query,
            ).strip(" ，,。？?！!：:")
            logger.info(f"[BiliBot] 🧰 私信回复模型选择工具 {name}：{query}")
            if action == "watch_direct":
                match = re.search(r"(?i)(BV[0-9A-Za-z]{10})", query)
                if not match:
                    return "观看工具需要有效的 BV 号，未执行。"
                force_rewatch = bool(
                    self._is_owner(str(actor_id or ""))
                    and re.search(
                        r"(?:重新\s*看(?:一次|一遍|一下)?|重看)",
                        str(original_content or ""),
                    )
                )
                result = await self._watch_video_and_save_memory(
                    match.group(1),
                    memory_source="private_tool",
                    force_rewatch=force_rewatch,
                )
                if result.get("ok"):
                    return str(result.get("message") or "已完成视频读取。")
                return str(result.get("message") or result.get("error") or "视频读取失败，未生成回复依据。")
            return await self._execute_private_bili_request(action, query)

        if name == "web_search":
            if not self.config.get("ENABLE_WEB_SEARCH", False):
                return "联网搜索当前未开启，未执行。"
            logger.info(f"[BiliBot] 🧰 私信回复模型选择工具 web_search：{query}")
            result = await self._web_search(query)
            return (
                f"联网搜索“{query}”结果：\n{result}"
                if result else f"联网搜索“{query}”暂时没有取得结果。"
            )
        return "回复模型选择了不受支持的后台能力，未执行。"

    async def _get_private_sessions(self):
        data, _ = await self._http_get(
            BILI_PRIVATE_SESSIONS_URL,
            headers=self._private_message_headers(),
            params={
                "session_type": 1,
                "group_fold": 1,
                "unfollow_fold": 0,
                "sort_rule": 2,
                "size": 100,
                "build": 0,
                "mobi_app": "web",
            },
        )
        if data.get("code") != 0:
            raise RuntimeError(
                f"获取私信会话失败: code={data.get('code')} {data.get('message', '')}"
            )
        return list((data.get("data") or {}).get("session_list") or [])

    async def _fetch_private_session_messages(self, talker_id, session_type, begin_seqno):
        data, _ = await self._http_get(
            BILI_PRIVATE_MESSAGES_URL,
            headers=self._private_message_headers(),
            params={
                "talker_id": talker_id,
                "session_type": session_type,
                "begin_seqno": begin_seqno,
                "size": 20,
                "sender_device_id": 1,
                "build": 0,
                "mobi_app": "web",
            },
        )
        if data.get("code") != 0:
            raise RuntimeError(
                f"获取私信内容失败: code={data.get('code')} {data.get('message', '')}"
            )
        return data.get("data") or {}

    async def _poll_private_inbox(self):
        self_uid = str(self.config.get("DEDE_USER_ID", "") or "").strip()
        state = self._load_json(
            PRIVATE_MESSAGE_STATE_FILE,
            self._default_private_message_state(self_uid),
        )
        if not isinstance(state, dict):
            state = self._default_private_message_state(self_uid)
        previous_account = str(state.get("account_uid") or "")
        account_changed = bool(previous_account and previous_account != self_uid)
        if previous_account != self_uid:
            state = self._default_private_message_state(self_uid)

        session_state = state.setdefault("sessions", {})
        processed = [str(item) for item in state.get("processed_keys", [])]
        processed_set = set(processed)

        try:
            message_limit = max(
                1,
                min(20, int(self.config.get("PRIVATE_MESSAGE_MAX_PER_POLL", 3) or 3)),
            )
        except (TypeError, ValueError):
            message_limit = 3

        now = int(time.time())
        pending = [
            dict(item)
            for item in state.get("pending_messages", [])
            if isinstance(item, dict) and str(item.get("msg_key") or "")
        ]
        # 优先处理已经持久化且到期的消息。即使 B 站接口暂时 -509，或进程在抓取后
        # 重启，也可以继续完成本地待处理消息，而不依赖远端游标再次返回它们。
        due_pending = [
            item
            for item in pending
            if int(item.get("next_retry_at") or 0) <= now
        ]
        if state.get("initialized") and due_pending:
            due_pending.sort(key=lambda item: (
                int(item.get("timestamp") or 0),
                int(item.get("msg_seqno") or 0),
            ))
            return due_pending[:message_limit]

        sessions = await self._get_private_sessions()

        if not state.get("initialized"):
            for session in sessions:
                talker_id = int(session.get("talker_id") or 0)
                session_type = int(session.get("session_type") or 1)
                if talker_id:
                    session_state[f"{session_type}:{talker_id}"] = int(
                        session.get("max_seqno") or 0
                    )
            state["initialized"] = True
            state["initialized_at"] = int(time.time())
            self._save_json(PRIVATE_MESSAGE_STATE_FILE, state)
            reason = "账号已切换，已重置" if account_changed else "首次启用"
            logger.info(f"[BiliBot] ✉️ 私信监听初始化完成（{reason}），已跳过历史消息")
            return []

        try:
            max_age = max(
                60,
                int(self.config.get("PRIVATE_MESSAGE_MAX_MESSAGE_AGE", 3600) or 3600),
            )
        except (TypeError, ValueError):
            max_age = 3600

        new_messages = []
        pending_keys = {str(item.get("msg_key")) for item in pending}
        for session in sessions:
            if len(new_messages) >= message_limit:
                break
            talker_id = int(session.get("talker_id") or 0)
            session_type = int(session.get("session_type") or 1)
            if not talker_id or session_type != 1:
                continue
            key = f"{session_type}:{talker_id}"
            last_seqno = int(session_state.get(key) or 0)
            remote_max = int(session.get("max_seqno") or 0)
            if last_seqno and remote_max and remote_max <= last_seqno:
                continue
            try:
                payload = await self._fetch_private_session_messages(
                    talker_id, session_type, last_seqno
                )
            except Exception as exc:
                # -509 是账号级请求频控，继续遍历其他会话只会增加请求量。
                # 交给外层统一退避，避免每个主循环周期都继续撞接口。
                if "code=-509" in str(exc):
                    raise
                logger.warning(f"[BiliBot] 私信会话 {talker_id} 拉取失败: {exc}")
                continue

            messages = list(payload.get("messages") or [])
            examined_max = last_seqno
            reached_limit = False
            for message in reversed(messages):
                msg_key = str(message.get("msg_key") or message.get("msg_seqno") or "")
                msg_seqno = int(message.get("msg_seqno") or 0)
                sender_uid = str(message.get("sender_uid") or "")
                msg_type = int(message.get("msg_type") or 0)
                timestamp = int(message.get("timestamp") or now)
                if timestamp > 10_000_000_000:
                    timestamp //= 1000
                if msg_seqno:
                    examined_max = max(examined_max, msg_seqno)
                if (
                    not msg_key
                    or msg_key in processed_set
                    or msg_key in pending_keys
                    or sender_uid == self_uid
                    or msg_type not in (1, 7)
                    or (last_seqno and msg_seqno and msg_seqno <= last_seqno)
                    or now - timestamp > max_age
                ):
                    continue
                content, content_type = self._private_message_content(
                    message.get("content"), msg_type
                )
                if not content:
                    continue
                account = session.get("account_info") or {}
                queued_message = {
                    "msg_key": msg_key,
                    "msg_seqno": msg_seqno,
                    "talker_id": talker_id,
                    "session_type": session_type,
                    "sender_uid": sender_uid or str(talker_id),
                    "username": account.get("name") or account.get("uname") or f"UID {talker_id}",
                    "content": content,
                    "content_type": content_type,
                    "timestamp": timestamp,
                    "retry_count": 0,
                    "next_retry_at": 0,
                    "queued_at": now,
                }
                new_messages.append(queued_message)
                pending.append(queued_message)
                pending_keys.add(msg_key)
                if len(new_messages) >= message_limit:
                    reached_limit = True
                    break

            if reached_limit:
                observed_max = examined_max
            else:
                observed_max = max(
                    [last_seqno, remote_max, int(payload.get("max_seqno") or 0)]
                    + [int(item.get("msg_seqno") or 0) for item in messages]
                )
            session_state[key] = observed_max

        state["account_uid"] = self_uid
        state["processed_keys"] = processed[-1000:]
        state["pending_messages"] = pending
        # 本方法 load 与 save 之间有多次网络 await，期间 _reset_private_conversation
        # 可能写入了新的 conversation_id，保存前合并磁盘上的最新值避免回滚
        disk_state = self._load_json(PRIVATE_MESSAGE_STATE_FILE, {})
        if isinstance(disk_state, dict) and isinstance(disk_state.get("conversation_ids"), dict):
            merged_ids = dict(state.get("conversation_ids") or {})
            merged_ids.update(disk_state["conversation_ids"])  # 磁盘上是 reset 刚写入的新值，优先
            state["conversation_ids"] = merged_ids
        self._save_json(PRIVATE_MESSAGE_STATE_FILE, state)
        return new_messages

    def _finish_private_message(self, message, error=None):
        """确认成功消息，或为失败消息安排有限重试。

        抓取阶段已经把完整消息写入 pending_messages，因此远端会话游标可以安全
        前进。只有处理成功才加入 processed_keys；连续失败三次后进入失败隔离记录，
        避免一条坏消息永久阻塞收件箱。
        """
        self_uid = str(self.config.get("DEDE_USER_ID", "") or "").strip()
        state = self._load_json(
            PRIVATE_MESSAGE_STATE_FILE,
            self._default_private_message_state(self_uid),
        )
        if not isinstance(state, dict):
            state = self._default_private_message_state(self_uid)

        msg_key = str(message.get("msg_key") or "")
        pending = [
            dict(item)
            for item in state.get("pending_messages", [])
            if isinstance(item, dict) and str(item.get("msg_key") or "")
        ]
        current = next(
            (item for item in pending if str(item.get("msg_key")) == msg_key),
            dict(message),
        )
        pending = [
            item for item in pending if str(item.get("msg_key")) != msg_key
        ]
        processed = [str(item) for item in state.get("processed_keys", [])]

        if error is None:
            if msg_key and msg_key not in processed:
                processed.append(msg_key)
            state["pending_messages"] = pending
            state["processed_keys"] = processed[-1000:]
            self._save_json(PRIVATE_MESSAGE_STATE_FILE, state)
            return "acknowledged", 0

        retry_count = int(current.get("retry_count") or 0) + 1
        error_text = str(error)[:500]
        if retry_count >= 3:
            if msg_key and msg_key not in processed:
                processed.append(msg_key)
            failed = [
                dict(item)
                for item in state.get("failed_messages", [])
                if isinstance(item, dict)
            ]
            failed.append({
                "msg_key": msg_key,
                "sender_uid": str(message.get("sender_uid") or ""),
                "username": str(message.get("username") or ""),
                "content": str(message.get("content") or "")[:500],
                "retry_count": retry_count,
                "last_error": error_text,
                "failed_at": int(time.time()),
            })
            state["failed_messages"] = failed[-100:]
            state["pending_messages"] = pending
            state["processed_keys"] = processed[-1000:]
            self._save_json(PRIVATE_MESSAGE_STATE_FILE, state)
            return "quarantined", retry_count

        delay = 30 * (2 ** (retry_count - 1))
        current["retry_count"] = retry_count
        current["last_error"] = error_text
        current["next_retry_at"] = int(time.time()) + delay
        pending.append(current)
        state["pending_messages"] = pending
        state["processed_keys"] = processed[-1000:]
        self._save_json(PRIVATE_MESSAGE_STATE_FILE, state)
        return "retry", delay

    def _record_private_block(self, message, reason, blocked):
        mid = str(message.get("sender_uid") or "")
        block_file = os.path.join(DATA_DIR, "block_log.json")
        block_log = self._load_json(block_file, {})
        block_log[mid] = {
            "username": message.get("username", ""),
            "reason": reason,
            "last_comment": message.get("content", ""),
            "last_message": message.get("content", ""),
            "source": "private_message",
            "score": self._affection.get(mid, 0),
            "api_blocked": bool(blocked),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self._save_json(block_file, block_log)

    async def _apply_private_reply_result(self, message, result, thread_id=None):
        if not result.get("_protocol_validated") or result.get("decision") != "reply":
            return False
        mid = str(message["sender_uid"])
        username = message["username"]
        content = message["content"]
        thread_id = thread_id or self._private_conversation_thread_id(mid)
        current_score = self._affection.get(mid, 0)
        try:
            score_delta = int(float(str(result.get("score_delta", 1)).strip()))
        except (ValueError, TypeError):
            score_delta = 1
        ai_reply = str(result.get("reply", "") or "").strip()
        if not ai_reply:
            return False

        if self.config.get("ENABLE_AFFECTION", True):
            if self._is_owner(mid):
                new_score = 100
            else:
                new_score = max(-99, min(99, current_score + score_delta))
            # 只探测不落盘：发送失败时不能把里程碑标成已触发（否则以后再达标也不提示）
            milestone_hit = self._peek_milestone(mid, current_score, new_score, username)
            if milestone_hit:
                ai_reply = milestone_hit[1]
        else:
            milestone_hit = None
            new_score = current_score

        # 先发送私信；失败时不落任何副作用，避免"好感度/画像已更新但用户没收到回复"的不一致。
        # 发送经过统一动作运行时，同一条持久化待处理消息不会并发重复发送。
        outcome = await self.event_runtime.execute(
            ActionRequest(
                key=f"private_reply:{message['msg_key']}",
                kind="private_reply",
                event_key=f"bilibili:private:{message['msg_key']}",
                target_id=mid,
                priority=0 if self._is_owner(mid) else 20,
            ),
            lambda: self._send_bili_private_message(mid, ai_reply),
        )
        if not outcome.success:
            if outcome.state == "unknown":
                logger.warning(
                    f"[BiliBot] 私信回复发送结果未知，UID={mid}；"
                    "本条按已处理收口，不提交好感度/画像且不会自动重发"
                )
                return True
            logger.warning(f"[BiliBot] 私信回复明确失败，UID={mid}，进入有限重试")
            return False

        if self.config.get("ENABLE_AFFECTION", True):
            self._affection[mid] = new_score
            self._save_json(AFFECTION_FILE, self._affection)
            self._record_relationship_interaction(mid, username, score_delta, "private")
            if milestone_hit:
                self._commit_milestone(mid, milestone_hit[0], username)

        commit_signals = getattr(self, "_commit_reply_signals", None)
        if callable(commit_signals):
            await commit_signals(
                event_key=str(message["msg_key"]), actor_id=mid,
                actor_name=username, scope="bili_dm", result=result,
            )

        impression = result.get("impression", "")
        user_facts = result.get("user_facts", [])
        if impression or user_facts:
            self._update_user_profile(
                mid,
                username=username,
                impression=impression or None,
                new_facts=user_facts or None,
                source_scope="bili_dm",
            )

        reply_log = self._load_json(REPLY_LOG_FILE, [])
        reply_log.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "mid": mid,
            "username": username,
            "content": content[:100],
            "reply": ai_reply[:100],
            "score_delta": score_delta,
            "channel": "private",
        })
        self._save_json(REPLY_LOG_FILE, reply_log[-500:])
        await self._save_memory_record(
            message["msg_key"],
            thread_id,
            mid,
            username,
            content,
            ai_reply,
            source="bilibili_private",
        )
        await self._compress_thread_memory(thread_id)
        await self._compress_user_memory(mid, username, "bili_dm")
        logger.info(
            f"[BiliBot] ✉️ 私信回复 {username}（{LEVEL_NAMES[self._get_level(new_score, mid)]}|{new_score}分）：{ai_reply[:80]}"
        )
        return True

    async def _poll_private_messages(self):
        if not self.config.get("ENABLE_PRIVATE_MESSAGES", False):
            return

        try:
            active_interval = max(
                60,
                min(
                    1800,
                    int(self.config.get("PRIVATE_MESSAGE_POLL_INTERVAL", 60) or 60),
                ),
            )
        except (TypeError, ValueError):
            active_interval = 60
        try:
            idle_interval = max(
                active_interval,
                min(
                    3600,
                    int(
                        self.config.get("PRIVATE_MESSAGE_IDLE_POLL_INTERVAL", 180)
                        or 180
                    ),
                ),
            )
        except (TypeError, ValueError):
            idle_interval = max(active_interval, 180)
        try:
            active_window = max(
                60,
                min(
                    3600,
                    int(
                        self.config.get("PRIVATE_MESSAGE_ACTIVE_WINDOW", 600)
                        or 600
                    ),
                ),
            )
        except (TypeError, ValueError):
            active_window = 600

        def jittered(delay):
            """Use a small positive jitter so requests do not form a fixed pattern."""
            return float(delay) + random.uniform(0, min(15.0, float(delay) * 0.2))

        now = time.monotonic()
        if now < getattr(self, "_private_message_next_poll_at", 0.0):
            return
        last_activity = float(
            getattr(self, "_private_message_last_activity_at", 0.0) or 0.0
        )
        current_interval = (
            active_interval
            if last_activity > 0 and now - last_activity <= active_window
            else idle_interval
        )
        # 请求发出前先占住下一次时间，异常时也不会跟随评论主循环连续重试。
        self._private_message_next_poll_at = now + jittered(current_interval)
        try:
            messages = await self._poll_private_inbox()
        except Exception as exc:
            if "code=-509" in str(exc):
                previous = int(
                    getattr(self, "_private_message_backoff_seconds", 0) or 0
                )
                backoff = min(
                    3600,
                    max(600, previous * 2),
                )
                self._private_message_backoff_seconds = backoff
                self._private_message_success_streak = 0
                self._private_message_next_poll_at = time.monotonic() + jittered(backoff)
                if backoff != int(
                    getattr(self, "_private_message_last_warned_backoff", 0) or 0
                ):
                    self._private_message_last_warned_backoff = backoff
                    logger.warning(
                        f"[BiliBot] 私信接口请求过于频繁，已暂停轮询 {backoff} 秒，"
                        "期间不会重复请求"
                    )
                else:
                    logger.info(
                        f"[BiliBot] 私信接口仍处于频控，继续等待 {backoff} 秒"
                    )
            else:
                logger.warning(f"[BiliBot] 私信轮询失败: {exc}")
            return

        completed_at = time.monotonic()
        if messages:
            self._private_message_last_activity_at = completed_at
        last_activity = float(
            getattr(self, "_private_message_last_activity_at", 0.0) or 0.0
        )
        normal_delay = (
            active_interval
            if last_activity > 0 and completed_at - last_activity <= active_window
            else idle_interval
        )
        backoff = int(getattr(self, "_private_message_backoff_seconds", 0) or 0)
        if backoff:
            success_streak = int(
                getattr(self, "_private_message_success_streak", 0) or 0
            ) + 1
            self._private_message_success_streak = success_streak
            if success_streak >= 2:
                reduced_backoff = max(idle_interval, backoff // 2)
                self._private_message_success_streak = 0
                self._private_message_last_warned_backoff = 0
                if reduced_backoff < backoff:
                    self._private_message_backoff_seconds = reduced_backoff
                    next_delay = reduced_backoff
                    logger.info(
                        f"[BiliBot] 私信接口连续成功 2 轮，恢复间隔由 "
                        f"{backoff} 秒降至 {reduced_backoff} 秒"
                    )
                else:
                    self._private_message_backoff_seconds = 0
                    next_delay = normal_delay
                    logger.info(
                        f"[BiliBot] 私信接口已稳定恢复，进入自适应轮询："
                        f"活跃 {active_interval} 秒 / 空闲 {idle_interval} 秒"
                    )
            else:
                next_delay = backoff
                logger.info(
                    f"[BiliBot] 私信接口恢复观察中：连续成功 {success_streak}/2，"
                    f"下一轮仍等待约 {backoff} 秒"
                )
        else:
            self._private_message_success_streak = 0
            next_delay = normal_delay
        self._private_message_next_poll_at = time.monotonic() + jittered(next_delay)

        runtime = getattr(self, "event_runtime", None)

        def private_sort_key(message):
            event = self._private_runtime_event(message)
            if runtime is not None:
                return runtime.event_sort_key(event)
            return int(event.priority), float(event.occurred_at), event.key

        messages.sort(key=private_sort_key)
        trusted_domains = self.config.get("PRIVATE_MESSAGE_TRUSTED_DOMAINS", []) or None
        for message in messages:
            try:
                handled = await self._handle_private_message(message, trusted_domains)
                if handled is False:
                    raise RuntimeError("消息处理未完成")
            except Exception as e:
                outcome, value = self._finish_private_message(message, error=e)
                if outcome == "retry":
                    retry_at = time.monotonic() + value
                    self._private_message_next_poll_at = min(
                        float(getattr(self, "_private_message_next_poll_at", retry_at)),
                        retry_at,
                    )
                    logger.warning(
                        f"[BiliBot] 处理单条私信失败，将在约 {value} 秒后重试"
                        f"（发送者 {message.get('sender_uid')}）: {e}"
                    )
                else:
                    logger.error(
                        f"[BiliBot] 单条私信连续失败 {value} 次，已写入失败隔离记录"
                        f"（发送者 {message.get('sender_uid')}）: {e}"
                    )
            else:
                self._finish_private_message(message)

    async def _handle_private_message(self, message, trusted_domains):
        mid = str(message["sender_uid"])
        username = message["username"]
        content = message["content"]
        runtime_event = self._private_runtime_event(message)
        claim = await self.event_runtime.claim(
            runtime_event,
            allow_retry_failed=int(message.get("retry_count") or 0) > 0,
        )
        if not claim.accepted:
            logger.debug(
                f"[BiliBot] 私信事件运行时去重：{message['msg_key']} ({claim.reason})"
            )
            return True
        decision = assess_private_message(content, trusted_domains)
        if decision.should_block:
            if self._private_sender_protected(mid):
                self._log_security_event(
                    "private_message_protected",
                    mid,
                    username,
                    content,
                    decision.reason,
                )
                logger.warning(
                    f"[BiliBot] 私信命中安全规则但用户受保护，未拉黑：{username}({mid})"
                )
                await self.event_runtime.transition(
                    claim.event_key, EventState.IGNORED, "protected_sender"
                )
                return True
            blocked = False
            if self.config.get("PRIVATE_MESSAGE_AUTO_BLOCK", True):
                block_outcome = await self.event_runtime.execute(
                    ActionRequest(
                        key=f"private_block:{mid}:{message['msg_key']}",
                        kind="block_user",
                        event_key=claim.event_key,
                        target_id=mid,
                        priority=0,
                        metadata={"budget_exempt": True, "safety_action": True},
                    ),
                    lambda: self._block_user(int(mid)),
                )
                blocked = block_outcome.success
            action = "已拉黑" if blocked else "已隔离，未完成拉黑"
            self._record_private_block(message, decision.reason, blocked)
            self._log_security_event(
                "private_message_auto_block" if blocked else "private_message_quarantined",
                mid,
                username,
                content,
                f"{decision.reason}；{action}",
            )
            logger.warning(
                f"[BiliBot] 🚫 私信安全拦截 {username}({mid})：{decision.reason}；{action}"
            )
            if not blocked:
                await self.event_runtime.transition(
                    claim.event_key, EventState.IGNORED, decision.reason
                )
            return True

        if (
            not self.config.get("PRIVATE_MESSAGE_AUTO_REPLY", True)
            or not self._private_reply_scope_allows(mid)
        ):
            await self.event_runtime.transition(
                claim.event_key, EventState.IGNORED, "auto_reply_disabled_or_scope_denied"
            )
            return True

        # Deliberately exact: only the standalone text "new" resets context.
        # "new一下", "/new" and messages merely containing the word stay normal chat.
        if message.get("content_type") == "text" and content.strip() == "new":
            new_thread_id = self._reset_private_conversation(mid)
            reset_reply = "已清除当前私信的对话上下文。"
            reset_outcome = await self.event_runtime.execute(
                ActionRequest(
                    key=f"private_reset_reply:{message['msg_key']}",
                    kind="private_reply",
                    event_key=claim.event_key,
                    target_id=mid,
                    priority=0 if self._is_owner(mid) else 10,
                    metadata={"budget_exempt": True, "control_reply": True},
                ),
                lambda: self._send_bili_private_message(mid, reset_reply),
            )
            sent = reset_outcome.success
            if sent:
                logger.info(
                    f"[BiliBot] 🆕 已重置私信上下文：{username}({mid}) -> {new_thread_id}"
                )
            else:
                logger.warning(f"[BiliBot] 私信上下文已重置，但确认消息发送失败：{mid}")
            return True

        score = self._affection.get(mid, 0)
        logger.info(
            f"[BiliBot] ✉️ 收到私信 {username}（{LEVEL_NAMES[self._get_level(score, mid)]}|{score}分）：{content[:100]}"
        )
        if self._daily_reply_limit_reached("private"):
            self._log_security_event("daily_private_limit", mid, username, content, "私信回复达到管理员硬上限")
            await self.event_runtime.transition(claim.event_key, EventState.IGNORED, "daily_private_limit")
            return True
        if message.get("content_type") == "text" and not self._is_owner(mid):
            filter_reason = self._interaction_filter_reason(content, "private")
            if filter_reason:
                self._log_security_event("private_interaction_filtered", mid, username, content, filter_reason)
                await self.event_runtime.transition(claim.event_key, EventState.IGNORED, filter_reason)
                return True
            interested, interest_reason = await self._should_reply_by_interest(content, username, "private")
            if not interested:
                self._log_security_event("private_interest_skip", mid, username, content, interest_reason)
                await self.event_runtime.transition(claim.event_key, EventState.IGNORED, "interest_skip")
                return True
        reply_content = content
        reference_context = ""
        allow_tool_request = message.get("content_type") == "text"
        if (
            message.get("content_type") == "video_share"
            and self.config.get("PRIVATE_MESSAGE_AUTO_WATCH_VIDEO", True)
        ):
            video_id = self._private_shared_video_id(content)
            if video_id:
                watch_result = await self._watch_video_and_save_memory(
                    video_id, memory_source="private_share"
                )
                if watch_result.get("ok"):
                    reply_content += (
                        "\n\n【你已经实际看完该视频，必须基于以下观看结果回复，"
                        "不要再说还没看或以后再看】\n"
                        + watch_result["message"]
                    )
                else:
                    reply_content += (
                        "\n\n【本次尝试观看失败，请如实说明暂时没能读取，"
                        "不要声称已经看完】\n"
                        + str(watch_result.get("message", "未知错误"))
                    )
        thread_id = self._private_conversation_thread_id(mid)
        result = await self._generate_reply(
            reply_content,
            mid,
            username,
            thread_id,
            0,
            0,
            channel="private",
            reference_context=reference_context,
            allow_tool_request=allow_tool_request,
        )
        decision = str((result or {}).get("decision") or "error")
        if decision in {"ignore", "observe"}:
            await self.event_runtime.transition(
                claim.event_key, EventState.IGNORED, f"model_{decision}"
            )
            return True
        if decision != "reply" or not result.get("reply"):
            reason = str((result or {}).get("error") or "reply_generation_failed")
            logger.warning(f"[BiliBot] 私信回复未生成：{username}({mid}) {reason}")
            await self.event_runtime.transition(
                claim.event_key, EventState.FAILED, reason
            )
            return reason == "invalid_model_output"
        tool_request = result.get("tool_request") or {}
        tool_name = str(tool_request.get("name") or "none").strip().lower()
        if tool_name != "none":
            progress_reply = str(result.get("reply") or "").strip() or "我查一下。"
            progress_outcome = await self.event_runtime.execute(
                ActionRequest(
                    key=f"private_tool_progress:{message['msg_key']}:{tool_name}",
                    kind="private_progress_reply",
                    event_key=claim.event_key,
                    target_id=mid,
                    priority=0 if self._is_owner(mid) else 20,
                ),
                lambda: self._send_bili_private_message(mid, progress_reply),
            )
            progress_sent = progress_outcome.success
            if progress_sent:
                logger.info(
                    f"[BiliBot] ✉️ 私信工具查询前回复 {username}：{progress_reply[:80]}"
                )
            else:
                logger.warning(f"[BiliBot] 私信工具查询前回复发送失败：{mid}")

            reference_context = await self._execute_private_model_tool(
                tool_request, actor_id=mid, original_content=content
            )
            final_result = await self._generate_reply(
                content,
                mid,
                username,
                thread_id,
                0,
                0,
                channel="private",
                reference_context=reference_context,
                allow_tool_request=False,
            )
            final_decision = str((final_result or {}).get("decision") or "error")
            if final_decision in {"ignore", "observe"}:
                await self.event_runtime.transition(
                    claim.event_key, EventState.IGNORED,
                    f"model_{final_decision}_after_tool",
                )
                return True
            if final_decision != "reply" or not final_result.get("reply"):
                logger.warning(
                    f"[BiliBot] 私信工具结果整合失败，已保留查询前回复：{username}({mid})"
                )
                if not progress_sent:
                    await self.event_runtime.transition(
                        claim.event_key, EventState.FAILED, "tool_result_merge_failed"
                    )
                return bool(progress_sent)
            applied = await self._apply_private_reply_result(
                message, final_result, thread_id=thread_id
            )
            return bool(applied)

        applied = await self._apply_private_reply_result(
            message, result, thread_id=thread_id
        )
        return bool(applied)
