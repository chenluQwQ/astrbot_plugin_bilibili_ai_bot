"""私信轮询：多久拉一次、被频控后怎么退避、新消息怎么排队，再一条条交给私信管线。

首次开启时建立游标，不会补回历史私信。"""
from __future__ import annotations

import json
import random
import re
import time

from astrbot.api import logger
from ..config import PRIVATE_MESSAGE_MIN_INTERVAL
from ..infra.bili.request_log import recent_request_counts
from ..infra.host_view import HostView

# 被频控后轮询间隔下限按这几档往上抬；连续 6 小时没再被限就放回一档。
PRIVATE_MESSAGE_FLOOR_STEPS = (60, 120, 180, 300, 600, 900, 1200, 1800)
PRIVATE_MESSAGE_FLOOR_RELAX_SECONDS = 6 * 3600


class PrivatePoll(HostView):
    """长期对象：``_private_message_*`` 这些退避状态在它身上。"""

    HOST_API = frozenset({
        "bili_messages", "event_runtime", "private_inbox", "private_reply", "settings",
    })

    def __init__(self, host):
        super().__init__(host)
        self._reset_poll_state()

    def _reset_poll_state(self):
        """插件停用时清掉轮询节奏；频控下限不清（原来就这样）。"""
        self._private_message_next_poll_at = 0.0
        self._private_message_backoff_seconds = 0
        self._private_message_success_streak = 0
        self._private_message_last_activity_at = 0.0
        self._private_message_last_warned_backoff = 0

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

    async def _poll_private_inbox(self):
        self_uid = str(self.settings.get("DEDE_USER_ID") or "").strip()
        state = self.private_inbox.load(self_uid)
        previous_account = str(state.get("account_uid") or "")
        account_changed = bool(previous_account and previous_account != self_uid)
        if previous_account != self_uid:
            state = self.private_inbox.default_state(self_uid)

        session_state = state.setdefault("sessions", {})
        processed = [str(item) for item in state.get("processed_keys", [])]
        processed_set = set(processed)

        try:
            message_limit = max(
                1,
                min(20, int(self.settings.get("PRIVATE_MESSAGE_MAX_PER_POLL") or 3)),
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

        sessions = await self.bili_messages._get_private_sessions()

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
            self.private_inbox.save(state)
            reason = "账号已切换，已重置" if account_changed else "首次启用"
            logger.info(f"[BiliBot] ✉️ 私信监听初始化完成（{reason}），已跳过历史消息")
            return []

        try:
            max_age = max(
                60,
                int(self.settings.get("PRIVATE_MESSAGE_MAX_MESSAGE_AGE") or 3600),
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
                payload = await self.bili_messages._fetch_private_session_messages(
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
        self.private_inbox.save_keeping_conversations(state)
        return new_messages

    def _finish_private_message(self, message, error=None):
        """确认成功消息，或为失败消息安排有限重试（见 ``PrivateInbox.finish``）。"""
        self_uid = str(self.settings.get("DEDE_USER_ID") or "").strip()
        return self.private_inbox.finish(message, error, self_uid)

    def _raise_private_message_floor(self, failed_gap):
        """被频控后，把这次失败时用的间隔往上抬一档，以后轮询不再低于它。"""
        floor = int(getattr(self, "_private_message_floor_seconds", 0) or 0)
        new_floor = next(
            (step for step in PRIVATE_MESSAGE_FLOOR_STEPS if step > failed_gap),
            PRIVATE_MESSAGE_FLOOR_STEPS[-1],
        )
        self._private_message_last_limited_at = time.monotonic()
        if new_floor > floor:
            self._private_message_floor_seconds = new_floor
            logger.warning(
                f"[BiliBot] 私信接口在间隔约 {failed_gap} 秒时被频控，"
                f"恢复后轮询间隔不低于 {new_floor} 秒"
            )
        recent = recent_request_counts(300)
        if recent:
            top = "，".join(f"{key} ×{count}" for key, count in recent.most_common(8))
            logger.info(
                f"[BiliBot] 被频控前 5 分钟共 {sum(recent.values())} 次 B站请求：{top}"
            )

    def _relax_private_message_floor(self, now):
        """很久没被频控，就把下限放回一档。"""
        floor = int(getattr(self, "_private_message_floor_seconds", 0) or 0)
        limited_at = float(
            getattr(self, "_private_message_last_limited_at", 0.0) or 0.0
        )
        if not floor or now - limited_at < PRIVATE_MESSAGE_FLOOR_RELAX_SECONDS:
            return floor
        lower = [step for step in PRIVATE_MESSAGE_FLOOR_STEPS if step < floor]
        floor = lower[-1] if lower else 0
        self._private_message_floor_seconds = floor
        self._private_message_last_limited_at = now
        logger.info(
            f"[BiliBot] 私信接口 {PRIVATE_MESSAGE_FLOOR_RELAX_SECONDS // 3600} 小时"
            f"没有被频控，轮询间隔下限放宽到 {floor or '配置值'}"
            f"{' 秒' if floor else ''}"
        )
        return floor

    async def _poll_private_messages(self):
        if not self.settings.get("ENABLE_PRIVATE_MESSAGES"):
            return

        try:
            active_interval = max(
                PRIVATE_MESSAGE_MIN_INTERVAL,
                min(
                    1800,
                    int(
                        self.settings.get("PRIVATE_MESSAGE_POLL_INTERVAL")
                        or PRIVATE_MESSAGE_MIN_INTERVAL
                    ),
                ),
            )
        except (TypeError, ValueError):
            active_interval = PRIVATE_MESSAGE_MIN_INTERVAL
        try:
            idle_interval = max(
                active_interval,
                min(
                    3600,
                    int(
                        self.settings.get("PRIVATE_MESSAGE_IDLE_POLL_INTERVAL")
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
                        self.settings.get("PRIVATE_MESSAGE_ACTIVE_WINDOW")
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
                # 只按成功轮询后定下的间隔抬下限；被限后的第一次试探不算，
                # 那时可能还在 B站的惩罚期里，翻倍退避已经管着它。
                failed_gap = int(getattr(self, "_private_message_last_delay", 0) or 0)
                self._private_message_last_delay = 0
                if failed_gap:
                    self._raise_private_message_floor(failed_gap)
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
        floor = self._relax_private_message_floor(completed_at)
        normal_delay = max(
            floor,
            active_interval
            if last_activity > 0 and completed_at - last_activity <= active_window
            else idle_interval,
        )
        backoff = int(getattr(self, "_private_message_backoff_seconds", 0) or 0)
        if backoff:
            success_streak = int(
                getattr(self, "_private_message_success_streak", 0) or 0
            ) + 1
            self._private_message_success_streak = success_streak
            if success_streak >= 2:
                reduced_backoff = max(idle_interval, floor, backoff // 2)
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
                        f"活跃 {max(floor, active_interval)} 秒 / "
                        f"空闲 {max(floor, idle_interval)} 秒"
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
        self._private_message_last_delay = next_delay
        self._private_message_next_poll_at = time.monotonic() + jittered(next_delay)

        runtime = getattr(self, "event_runtime", None)

        def private_sort_key(message):
            event = self.private_reply._private_runtime_event(message)
            if runtime is not None:
                return runtime.event_sort_key(event)
            return int(event.priority), float(event.occurred_at), event.key

        messages.sort(key=private_sort_key)
        trusted_domains = self.settings.get("PRIVATE_MESSAGE_TRUSTED_DOMAINS") or None
        for message in messages:
            try:
                handled = await self.private_reply._handle_private_message(message, trusted_domains)
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
