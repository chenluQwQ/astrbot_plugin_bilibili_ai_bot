"""BiliBot 本体的 B站直播弹幕监听与自动回复。"""

import asyncio
import os
import re
import time
from collections import deque
from datetime import datetime

from astrbot.api import logger

from .config import (
    AFFECTION_FILE,
    DATA_DIR,
    LEVEL_NAMES,
    REPLY_LOG_FILE,
)
from .runtime import ActionRequest, EventState, InboundEvent


BILI_LIVE_HISTORY_URL = (
    "https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory"
)
BILI_LIVE_SEND_URL = "https://api.live.bilibili.com/msg/send"
_LIVE_TASK_NAME = "astrbot_plugin_bilibili_ai_bot.live_danmaku"


class LiveDanmakuMixin:
    """低频轮询直播历史弹幕，并用 BiliBot 自身人格和记忆回复。"""

    def _init_live_danmaku_state(self):
        self._live_danmaku_task = None
        self._live_reply_task = None
        self._live_memory_tasks = set()
        self._live_seen_keys = set()
        self._live_seen_order = deque()
        self._live_pending_events = deque(maxlen=30)
        self._live_recent_events = deque(maxlen=30)
        self._live_sent_echoes = deque(maxlen=30)
        self._live_reply_marks = deque(maxlen=120)
        self._live_reply_lock = asyncio.Lock()
        self._live_initialized = False
        self._live_session_id = ""
        self._live_last_reply_at = 0.0
        self._live_last_event_at = 0.0
        self._live_last_event_text = ""
        self._live_listener_last_error = ""
        self._live_send_last_error = ""
        self._live_last_warning_at = 0.0
        self._live_listener_stopping = False
        self._live_send_backoff_until = 0.0

    def _live_room_id(self):
        try:
            return max(0, int(self.config.get("LIVE_DANMAKU_ROOM_ID", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _live_poll_interval(self):
        try:
            value = float(self.config.get("LIVE_DANMAKU_POLL_INTERVAL", 5) or 5)
        except (TypeError, ValueError):
            value = 5
        return max(2.0, min(60.0, value))

    def _live_reply_cooldown(self):
        try:
            value = float(self.config.get("LIVE_DANMAKU_REPLY_COOLDOWN", 12) or 12)
        except (TypeError, ValueError):
            value = 12
        return max(3.0, min(300.0, value))

    async def _start_live_danmaku_listener(self):
        if not self.config.get("ENABLE_LIVE_DANMAKU_REPLY", False):
            return False, "直播弹幕回复未开启"
        room_id = self._live_room_id()
        if not room_id:
            return False, "未配置直播间房间号"
        if not self._running:
            return False, "BiliBot 主循环尚未启动"
        if self._live_danmaku_task and not self._live_danmaku_task.done():
            return True, f"直播弹幕监听已在运行（房间 {room_id}）"

        current = asyncio.current_task()
        stale_tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not current
            and not task.done()
            and task.get_name() == _LIVE_TASK_NAME
        ]
        for task in stale_tasks:
            task.cancel()
        if stale_tasks:
            await asyncio.gather(*stale_tasks, return_exceptions=True)
            logger.warning(
                f"[BiliBot] 已清理 {len(stale_tasks)} 个热重载残留直播弹幕任务"
            )

        self._live_initialized = False
        self._live_listener_stopping = False
        self._live_seen_keys.clear()
        self._live_seen_order.clear()
        self._live_pending_events.clear()
        self._live_session_id = f"bilibot:{room_id}:{int(time.time())}"
        self._live_listener_last_error = ""
        self._live_danmaku_task = asyncio.create_task(
            self._live_danmaku_loop(),
            name=_LIVE_TASK_NAME,
        )
        logger.info(f"[BiliBot] 🎙️ 直播弹幕回复监听启动：房间 {room_id}")
        return True, f"直播弹幕监听已启动（房间 {room_id}）"

    async def _stop_live_danmaku_listener(self):
        self._live_listener_stopping = True
        self._live_pending_events.clear()
        task = self._live_danmaku_task
        self._live_danmaku_task = None
        if task and not task.done():
            task.cancel()
            if task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
        reply_task = self._live_reply_task
        self._live_reply_task = None
        if reply_task and not reply_task.done():
            reply_task.cancel()
            if reply_task is not asyncio.current_task():
                await asyncio.gather(reply_task, return_exceptions=True)
        memory_tasks = [task for task in self._live_memory_tasks if not task.done()]
        for memory_task in memory_tasks:
            memory_task.cancel()
        if memory_tasks:
            await asyncio.gather(*memory_tasks, return_exceptions=True)
        self._live_memory_tasks.clear()
        self._live_pending_events.clear()
        self._live_listener_stopping = False

    def _live_listener_running(self):
        return bool(
            self._live_danmaku_task and not self._live_danmaku_task.done()
        )

    async def _live_danmaku_loop(self):
        while self._running and self.config.get(
            "ENABLE_LIVE_DANMAKU_REPLY", False
        ):
            try:
                rows = await self._fetch_live_danmaku_rows()
                if not self._live_initialized:
                    for row in rows:
                        self._mark_live_event_seen(self._live_event_key(row))
                    self._live_initialized = True
                    self._live_listener_last_error = ""
                    logger.info(
                        f"[BiliBot] 🎙️ 直播弹幕当前位置建立完成，已跳过 {len(rows)} 条历史弹幕"
                    )
                else:
                    new_rows = []
                    for row in rows:
                        key = self._live_event_key(row)
                        if not key or key in self._live_seen_keys:
                            continue
                        self._mark_live_event_seen(key)
                        new_rows.append((key, row))
                    new_rows.sort(
                        key=lambda item: (
                            str(item[1].get("timeline") or ""),
                            str(item[1].get("rnd") or item[0]),
                        )
                    )
                    for key, row in new_rows:
                        await self._handle_live_danmaku_row(key, row)
                    self._live_listener_last_error = ""
                await asyncio.sleep(self._live_poll_interval())
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._record_live_error("listener", str(exc))
                await asyncio.sleep(max(10.0, self._live_poll_interval() * 2))

    async def _fetch_live_danmaku_rows(self):
        room_id = self._live_room_id()
        headers = {
            **self._headers(),
            "Origin": "https://live.bilibili.com",
            "Referer": f"https://live.bilibili.com/{room_id}",
        }
        payload, _ = await self._http_get(
            BILI_LIVE_HISTORY_URL,
            headers=headers,
            params={"roomid": room_id, "room_type": 0},
            timeout=10,
            retries=0,
        )
        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else "unknown"
            message = (
                payload.get("message") or payload.get("msg")
                if isinstance(payload, dict)
                else "响应格式异常"
            )
            raise RuntimeError(f"历史弹幕获取失败: code={code} message={message}")
        rows = (payload.get("data") or {}).get("room") or []
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _live_event_key(row):
        return str(
            row.get("id_str")
            or row.get("rnd")
            or f"{row.get('timeline')}|{row.get('uid')}|{row.get('text')}"
        ).strip()

    def _mark_live_event_seen(self, key):
        if not key or key in self._live_seen_keys:
            return
        while len(self._live_seen_order) >= 1000:
            old = self._live_seen_order.popleft()
            self._live_seen_keys.discard(old)
        self._live_seen_order.append(key)
        self._live_seen_keys.add(key)

    def _is_own_live_danmaku(self, uid, text):
        bot_uid = str(self.config.get("DEDE_USER_ID", "") or "").strip()
        if bot_uid and str(uid or "").strip() == bot_uid:
            return True
        now = time.time()
        while self._live_sent_echoes and now - self._live_sent_echoes[0][0] > 45:
            self._live_sent_echoes.popleft()
        normalized = self._normalize_live_text(text)
        return bool(
            normalized
            and any(normalized == sent for _, sent in self._live_sent_echoes)
        )

    def _live_runtime_event(self, event):
        uid = str(event.get("uid") or "")
        return InboundEvent(
            source="live",
            event_id=str(event.get("event_id") or ""),
            actor_id=uid,
            actor_name=event.get("username", ""),
            content=event.get("content", ""),
            conversation_id=self._live_session_id,
            target_id=str(self._live_room_id()),
            account_id=str(self.config.get("DEDE_USER_ID", "") or ""),
            occurred_at=float(event.get("occurred_at") or 0),
            metadata={
                "listener": "history",
                "is_admin": self._is_owner(uid),
            },
        )

    async def _handle_live_danmaku_row(self, key, row):
        uid = str(row.get("uid") or "").strip()
        username = str(row.get("nickname") or row.get("uname") or "观众").strip()
        content = self._normalize_live_text(row.get("text"))
        if not uid or not content or self._is_own_live_danmaku(uid, content):
            return
        block_log = self._load_json(os.path.join(DATA_DIR, "block_log.json"), {})
        if uid in block_log:
            return

        event = {
            "event_id": f"history:{self._live_room_id()}:{key}",
            "uid": uid,
            "username": username or "观众",
            "content": content,
            "timeline": str(row.get("timeline") or ""),
            "occurred_at": time.time(),
        }
        runtime_event = self._live_runtime_event(event)
        claim = await self.event_runtime.claim(runtime_event)
        if not claim.accepted:
            return
        self._live_last_event_at = time.time()
        self._live_last_event_text = f"{event['username']}: {content}"
        self._live_recent_events.append(event)
        logger.info(
            f"[BiliBot] 🎙️ 直播弹幕 {event['username']}({uid}): {content[:100]}"
        )

        memory_task = asyncio.create_task(self._record_live_event_safe(event))
        self._live_memory_tasks.add(memory_task)
        memory_task.add_done_callback(self._live_memory_tasks.discard)

        self._live_pending_events.append(event)
        self._schedule_live_reply()

    async def _record_live_event_safe(self, event):
        try:
            await self.memory_api.record_live_event(
                user_id=event["uid"],
                username=event["username"],
                event_type="danmaku",
                content=event["content"],
                session_id=self._live_session_id,
                event_id=event["event_id"],
                room_id=str(self._live_room_id()),
                extra={"listener": "bilibot"},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[BiliBot] 直播弹幕记忆写入失败: {exc}")

    def _schedule_live_reply(self):
        if self._live_reply_task and not self._live_reply_task.done():
            return
        self._live_reply_task = asyncio.create_task(self._live_reply_worker())

    def _live_context_for_prompt(self, current_event_content=""):
        rows = []
        current = self._normalize_live_text(current_event_content)
        recent = list(self._live_recent_events)[-6:]
        for index, event in enumerate(recent):
            content = self._normalize_live_text(event.get("content"))
            if current and content == current and index == len(recent) - 1:
                continue
            rows.append(f"- {event.get('username') or '观众'}：{content}")
        if not rows:
            return ""
        return (
            "【本场最近弹幕】这些只是直播间公共上下文，注意区分观众：\n"
            + "\n".join(rows)
            + "\n"
        )

    async def _live_reply_worker(self):
        try:
            remaining = self._live_reply_cooldown() - (
                time.time() - self._live_last_reply_at
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
            send_backoff = self._live_send_backoff_until - time.monotonic()
            if send_backoff > 0:
                await asyncio.sleep(send_backoff)
            if not self._live_pending_events:
                return
            if self._live_reply_rate_limited():
                dropped = list(self._live_pending_events)
                self._live_pending_events.clear()
                await asyncio.gather(
                    *(
                        self.event_runtime.transition(
                            f"bilibili:live:{item['event_id']}",
                            EventState.IGNORED,
                            "rate_limited",
                        )
                        for item in dropped
                    )
                )
                return

            ranked = sorted(
                list(self._live_pending_events),
                key=lambda item: self.event_runtime.event_sort_key(
                    self._live_runtime_event(item), newest_first=True
                ),
            )
            event = ranked[0]
            coalesced = ranked[1:]
            self._live_pending_events.clear()
            if coalesced:
                await asyncio.gather(
                    *(
                        self.event_runtime.transition(
                            f"bilibili:live:{item['event_id']}",
                            EventState.IGNORED,
                            "coalesced_into_newer_danmaku",
                        )
                        for item in coalesced
                    )
                )
            result = await self._generate_reply(
                event["content"],
                event["uid"],
                event["username"],
                f"live:{self._live_room_id()}:{event['uid']}",
                0,
                0,
                channel="live",
            )
            decision = str((result or {}).get("decision") or "error")
            if decision in {"ignore", "observe"}:
                await self.event_runtime.transition(
                    f"bilibili:live:{event['event_id']}",
                    EventState.IGNORED,
                    f"model_{decision}",
                )
                return
            if decision != "reply" or not result.get("reply"):
                await self.event_runtime.transition(
                    f"bilibili:live:{event['event_id']}",
                    EventState.FAILED,
                    str((result or {}).get("error") or "reply_generation_failed"),
                )
                return
            applied = await self._apply_live_reply_result(event, result)
            if not applied:
                await self.event_runtime.transition(
                    f"bilibili:live:{event['event_id']}",
                    EventState.FAILED,
                    "reply_not_sent",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[BiliBot] 直播弹幕回复处理失败: {exc}")
        finally:
            self._live_reply_task = None
            if (
                self._live_pending_events
                and self._running
                and not self._live_listener_stopping
                and self.config.get("ENABLE_LIVE_DANMAKU_REPLY", False)
            ):
                self._schedule_live_reply()

    def _live_reply_rate_limited(self):
        try:
            limit = int(self.config.get("LIVE_DANMAKU_MAX_PER_MINUTE", 4) or 4)
        except (TypeError, ValueError):
            limit = 4
        if limit <= 0:
            return False
        now = time.time()
        while self._live_reply_marks and now - self._live_reply_marks[0] >= 60:
            self._live_reply_marks.popleft()
        return len(self._live_reply_marks) >= max(1, min(30, limit))

    async def _apply_live_reply_result(self, event, result):
        if not result.get("_protocol_validated") or result.get("decision") != "reply":
            return False
        uid = event["uid"]
        username = event["username"]
        content = event["content"]
        reply_text = self._normalize_live_text(result.get("reply"))
        if not reply_text:
            return False

        outcome = await self.event_runtime.execute(
            ActionRequest(
                key=f"live_reply:{event['event_id']}",
                kind="live_reply",
                event_key=f"bilibili:live:{event['event_id']}",
                target_id=str(self._live_room_id()),
                priority=int(self._live_runtime_event(event).priority),
            ),
            lambda: self._send_live_danmaku_text(reply_text),
            success=lambda value: int(value or 0) > 0,
        )
        if not outcome.success:
            return False

        old_score = self._affection.get(uid, 0)
        try:
            score_delta = int(result.get("score_delta", 1) or 0)
        except (TypeError, ValueError):
            score_delta = 1
        if self.config.get("ENABLE_AFFECTION", True):
            new_score = 100 if self._is_owner(uid) else max(
                0, min(99, old_score + score_delta)
            )
            self._affection[uid] = new_score
            self._save_json(AFFECTION_FILE, self._affection)
        else:
            new_score = old_score

        commit_signals = getattr(self, "_commit_reply_signals", None)
        if callable(commit_signals):
            await commit_signals(
                event_key=str(event["event_id"]), actor_id=uid,
                actor_name=username, scope="bili_live", result=result,
            )

        impression = str(result.get("impression", "") or "").strip()
        user_facts = result.get("user_facts", [])
        if not isinstance(user_facts, list):
            user_facts = []
        if impression or user_facts:
            self._update_user_profile(
                uid,
                username=username,
                impression=impression or None,
                new_facts=user_facts or None,
                source_scope="bili_live",
            )

        self._live_last_reply_at = time.time()
        self._live_reply_marks.append(self._live_last_reply_at)
        reply_log = self._load_json(REPLY_LOG_FILE, [])
        reply_log.append(
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "mid": uid,
                "username": username,
                "content": content[:100],
                "reply": reply_text[:100],
                "score_delta": score_delta,
                "channel": "live",
                "room_id": str(self._live_room_id()),
            }
        )
        self._save_json(REPLY_LOG_FILE, reply_log[-500:])
        memory_task = asyncio.create_task(
            self._record_live_reply_memory_safe(event, reply_text)
        )
        self._live_memory_tasks.add(memory_task)
        memory_task.add_done_callback(self._live_memory_tasks.discard)
        logger.info(
            f"[BiliBot] 🎙️ 直播回复 {username}（{LEVEL_NAMES[self._get_level(new_score, uid)]}|{new_score}分）：{reply_text[:80]}"
        )
        return True

    async def _record_live_reply_memory_safe(self, event, reply_text):
        try:
            await self.memory_api.record(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] "
                f"{event['username']}在直播中说：{event['content']}；"
                f"Bot回复：{reply_text}",
                user_id=event["uid"],
                username=event["username"],
                source="bilibili_live",
                memory_type="live",
                level="today",
                importance=6,
                extra={
                    "session_id": self._live_session_id,
                    "room_id": str(self._live_room_id()),
                    "external_event_id": f"{event['event_id']}:reply",
                    "live_event_type": "reply",
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[BiliBot] 直播回复记忆写入失败: {exc}")

    async def _send_live_danmaku_text(self, text):
        chunks = self._split_live_danmaku_text(text)
        if not chunks:
            self._record_live_error("send", "弹幕内容为空")
            return 0
        async with self._live_reply_lock:
            sent = 0
            for index, chunk in enumerate(chunks):
                if not await self._send_live_danmaku_chunk(chunk):
                    break
                normalized = self._normalize_live_text(chunk)
                self._live_sent_echoes.append((time.time(), normalized))
                sent += 1
                if index + 1 < len(chunks):
                    await asyncio.sleep(1.5)
            return sent

    async def _send_live_danmaku_chunk(self, text):
        room_id = self._live_room_id()
        csrf = str(self.config.get("BILI_JCT", "") or "").strip()
        if not room_id or not self._has_cookie() or not csrf:
            self._record_live_error(
                "send", "房间号、SESSDATA 或 bili_jct 未配置完整"
            )
            return False
        headers = {
            **self._headers(),
            "Origin": "https://live.bilibili.com",
            "Referer": f"https://live.bilibili.com/{room_id}",
        }
        try:
            payload, _ = await self._http_post(
                BILI_LIVE_SEND_URL,
                headers=headers,
                data={
                    "bubble": 0,
                    "msg": text,
                    "color": 16777215,
                    "mode": 1,
                    "fontsize": 25,
                    "rnd": int(time.time()),
                    "roomid": room_id,
                    "csrf": csrf,
                    "csrf_token": csrf,
                },
                timeout=10,
                retries=0,
            )
        except Exception as exc:
            self._live_send_backoff_until = time.monotonic() + 60
            self._record_live_error("send", f"弹幕发送请求失败: {exc}")
            return False
        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else "unknown"
            message = (
                payload.get("message") or payload.get("msg")
                if isinstance(payload, dict)
                else "响应格式异常"
            )
            self._record_live_error(
                "send", f"弹幕发送失败: code={code} message={message}"
            )
            self._live_send_backoff_until = time.monotonic() + (
                300 if str(code) in {"-352", "-412", "-509"} else 60
            )
            return False
        self._live_send_last_error = ""
        self._live_send_backoff_until = 0.0
        return True

    def _split_live_danmaku_text(self, text):
        cleaned = self._normalize_live_text(text)
        try:
            max_total = int(
                self.config.get("LIVE_DANMAKU_REPLY_MAX_LENGTH", 60) or 60
            )
        except (TypeError, ValueError):
            max_total = 60
        max_total = max(10, min(120, max_total))
        if len(cleaned) > max_total:
            cleaned = cleaned[: max_total - 1].rstrip() + "…"
        max_length = 20
        max_chunks = 3
        chunks = []
        remaining = cleaned
        break_chars = "，。！？；、,.!?;：: "
        while remaining and len(chunks) < max_chunks:
            slots_left = max_chunks - len(chunks)
            if len(remaining) <= max_length:
                chunks.append(remaining)
                break
            if slots_left == 1:
                chunks.append(remaining[: max_length - 1].rstrip() + "…")
                break
            window = remaining[:max_length]
            split_at = max(window.rfind(char) for char in break_chars)
            if split_at < max_length // 2:
                split_at = max_length
            elif window[split_at] in break_chars.strip():
                split_at += 1
            chunk = remaining[:split_at].strip()
            if chunk:
                chunks.append(chunk)
            remaining = remaining[split_at:].strip()
        return chunks

    @staticmethod
    def _normalize_live_text(text):
        cleaned = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", str(text or "").strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip().strip('"“”')

    def _record_live_error(self, kind, message):
        message = self._normalize_live_text(message)[:200] or "未知错误"
        if kind == "listener":
            old = self._live_listener_last_error
            self._live_listener_last_error = message
        else:
            old = self._live_send_last_error
            self._live_send_last_error = message
        now = time.time()
        if message != old or now - self._live_last_warning_at >= 300:
            self._live_last_warning_at = now
            label = "监听" if kind == "listener" else "发送"
            logger.warning(f"[BiliBot] 直播弹幕{label}失败: {message}")

    def _live_danmaku_status(self):
        return {
            "enabled": bool(
                self.config.get("ENABLE_LIVE_DANMAKU_REPLY", False)
            ),
            "running": self._live_listener_running(),
            "initialized": self._live_initialized,
            "room_id": self._live_room_id(),
            "poll_interval": self._live_poll_interval(),
            "cooldown": self._live_reply_cooldown(),
            "recent_count": len(self._live_recent_events),
            "pending_count": len(self._live_pending_events),
            "last_event": self._live_last_event_text,
            "last_event_at": self._live_last_event_at,
            "last_reply_at": self._live_last_reply_at,
            "listener_error": self._live_listener_last_error,
            "send_error": self._live_send_last_error,
            "send_backoff_seconds": max(
                0, int(self._live_send_backoff_until - time.monotonic())
            ),
        }
