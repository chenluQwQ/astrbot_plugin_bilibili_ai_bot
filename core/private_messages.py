"""B站私信轮询、回复与安全隔离。

仅处理个人会话中的纯文本和 B站视频分享卡片。首次开启时建立游标，
不会补回历史私信；危险链接判断只解析文字，不访问目标地址。
"""
import ipaddress
import json
import os
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
    PERMANENT_MEMORY_FILE,
    PRIVATE_MESSAGE_STATE_FILE,
    REPLY_LOG_FILE,
)


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
        }

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
        sessions = await self._get_private_sessions()
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
        try:
            message_limit = max(
                1,
                min(20, int(self.config.get("PRIVATE_MESSAGE_MAX_PER_POLL", 3) or 3)),
            )
        except (TypeError, ValueError):
            message_limit = 3

        now = int(time.time())
        new_messages = []
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
                new_messages.append({
                    "msg_key": msg_key,
                    "msg_seqno": msg_seqno,
                    "talker_id": talker_id,
                    "session_type": session_type,
                    "sender_uid": sender_uid or str(talker_id),
                    "username": account.get("name") or account.get("uname") or f"UID {talker_id}",
                    "content": content,
                    "content_type": content_type,
                    "timestamp": timestamp,
                })
                processed.append(msg_key)
                processed_set.add(msg_key)
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
        self._save_json(PRIVATE_MESSAGE_STATE_FILE, state)
        return new_messages

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

    async def _apply_private_reply_result(self, message, result):
        mid = str(message["sender_uid"])
        username = message["username"]
        content = message["content"]
        thread_id = f"private:{mid}"
        current_score = self._affection.get(mid, 0)
        score_delta = result.get("score_delta", 1)
        ai_reply = str(result.get("reply", "") or "").strip()
        if not ai_reply:
            return False

        if self.config.get("ENABLE_AFFECTION", True):
            if self._is_owner(mid):
                new_score = 100
            else:
                new_score = max(0, min(99, current_score + score_delta))
            self._affection[mid] = new_score
            self._save_json(AFFECTION_FILE, self._affection)
            milestone = self._check_milestone(mid, current_score, new_score, username)
            if milestone:
                ai_reply = milestone
        else:
            new_score = current_score

        impression = result.get("impression", "")
        user_facts = result.get("user_facts", [])
        if impression or user_facts:
            self._update_user_profile(
                mid,
                username=username,
                impression=impression or None,
                new_facts=user_facts or None,
            )

        permanent = result.get("permanent_memory", "")
        if permanent:
            memories = self._load_json(PERMANENT_MEMORY_FILE, [])
            if len(memories) < 20:
                memories.append({
                    "text": permanent,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                })
                self._save_json(PERMANENT_MEMORY_FILE, memories)

        success = await self._send_bili_private_message(mid, ai_reply)
        if not success:
            logger.warning(f"[BiliBot] 私信回复发送失败，UID={mid}，不会自动重发")
            return False

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
        await self._compress_user_memory(mid, username)
        logger.info(
            f"[BiliBot] ✉️ 私信回复 {username}（{LEVEL_NAMES[self._get_level(new_score, mid)]}|{new_score}分）：{ai_reply[:80]}"
        )
        return True

    async def _poll_private_messages(self):
        if not self.config.get("ENABLE_PRIVATE_MESSAGES", False):
            return

        try:
            poll_interval = max(
                30,
                min(
                    1800,
                    int(self.config.get("PRIVATE_MESSAGE_POLL_INTERVAL", 30) or 30),
                ),
            )
        except (TypeError, ValueError):
            poll_interval = 30

        now = time.monotonic()
        if now < getattr(self, "_private_message_next_poll_at", 0.0):
            return
        # 请求发出前先占住下一次时间，异常时也不会跟随评论主循环连续重试。
        self._private_message_next_poll_at = now + poll_interval
        try:
            messages = await self._poll_private_inbox()
        except Exception as exc:
            if "code=-509" in str(exc):
                previous = int(
                    getattr(self, "_private_message_backoff_seconds", 0) or 0
                )
                backoff = min(
                    1800,
                    max(300, poll_interval * 2, previous * 2),
                )
                self._private_message_backoff_seconds = backoff
                self._private_message_next_poll_at = time.monotonic() + backoff
                logger.warning(
                    f"[BiliBot] 私信接口请求过于频繁，已暂停轮询 {backoff} 秒，"
                    "期间不会重复请求"
                )
            else:
                logger.warning(f"[BiliBot] 私信轮询失败: {exc}")
            return

        self._private_message_backoff_seconds = 0
        self._private_message_next_poll_at = time.monotonic() + poll_interval

        trusted_domains = self.config.get("PRIVATE_MESSAGE_TRUSTED_DOMAINS", []) or None
        for message in messages:
            mid = str(message["sender_uid"])
            username = message["username"]
            content = message["content"]
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
                    continue
                blocked = False
                if self.config.get("PRIVATE_MESSAGE_AUTO_BLOCK", True):
                    blocked = await self._block_user(int(mid))
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
                continue

            if (
                not self.config.get("PRIVATE_MESSAGE_AUTO_REPLY", True)
                or not self._private_reply_scope_allows(mid)
            ):
                continue

            score = self._affection.get(mid, 0)
            logger.info(
                f"[BiliBot] ✉️ 收到私信 {username}（{LEVEL_NAMES[self._get_level(score, mid)]}|{score}分）：{content[:100]}"
            )
            reply_content = content
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
            result = await self._generate_reply(
                reply_content,
                mid,
                username,
                f"private:{mid}",
                0,
                0,
                channel="private",
            )
            if not result or not result.get("reply"):
                logger.warning(f"[BiliBot] 私信回复生成失败，已跳过：{username}({mid})")
                continue
            await self._apply_private_reply_result(message, result)
