"""私信收件箱状态：``private_message_state.json`` 只由这里写。

里面有：每个会话看到哪条（``sessions``）、处理过的消息（``processed_keys``）、抓到还没处理完的
（``pending_messages``）、连续失败三次隔离起来的（``failed_messages``），以及每个人当前的对话编号
（``conversation_ids``，发 ``new`` 时换一个）。账号换了整份重来，由调用方按 ``account_uid`` 判断。
"""
from __future__ import annotations

import time
import uuid

from ..storage.json_file import JsonFile


class PrivateInbox:
    def __init__(self, file: JsonFile):
        self._file = file

    @staticmethod
    def default_state(account_uid=""):
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

    def load(self, account_uid):
        state = self._file.read(self.default_state(account_uid))
        if not isinstance(state, dict):
            state = self.default_state(account_uid)
        return state

    def initialized(self):
        state = self._file.read({})
        return isinstance(state, dict) and bool(state.get("initialized"))

    def save(self, state):
        self._file.write(state)

    def save_keeping_conversations(self, state):
        """抓取时 load 和 save 之间隔着多次网络请求，期间 ``reset_conversation`` 可能写了新的
        对话编号；保存前合并磁盘上的最新值，不让它回滚。"""
        disk_state = self._file.read({})
        if isinstance(disk_state, dict) and isinstance(disk_state.get("conversation_ids"), dict):
            merged_ids = dict(state.get("conversation_ids") or {})
            merged_ids.update(disk_state["conversation_ids"])  # 磁盘上是 reset 刚写入的新值，优先
            state["conversation_ids"] = merged_ids
        self._file.write(state)

    def thread_id(self, mid, account_uid):
        """Return the current lightweight conversation id for one Bilibili UID."""
        uid = str(mid or "").strip()
        state = self.load(account_uid)
        conversation_ids = state.setdefault("conversation_ids", {})
        conversation_id = str(conversation_ids.get(uid) or "").strip()
        return f"private:{uid}:{conversation_id}" if conversation_id else f"private:{uid}"

    def reset_conversation(self, mid, account_uid):
        """Start a new private-message context without deleting profile or long-term memory."""
        uid = str(mid or "").strip()
        state = self.load(account_uid)
        conversation_id = uuid.uuid4().hex[:12]
        state.setdefault("conversation_ids", {})[uid] = conversation_id
        self._file.write(state)
        return f"private:{uid}:{conversation_id}"

    def finish(self, message, error, account_uid):
        """确认成功消息，或为失败消息安排有限重试。

        抓取阶段已经把完整消息写入 pending_messages，因此远端会话游标可以安全
        前进。只有处理成功才加入 processed_keys；连续失败三次后进入失败隔离记录，
        避免一条坏消息永久阻塞收件箱。
        """
        state = self.load(account_uid)

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
            self._file.write(state)
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
            self._file.write(state)
            return "quarantined", retry_count

        delay = 30 * (2 ** (retry_count - 1))
        current["retry_count"] = retry_count
        current["last_error"] = error_text
        current["next_retry_at"] = int(time.time()) + delay
        pending.append(current)
        state["pending_messages"] = pending
        state["processed_keys"] = processed[-1000:]
        self._file.write(state)
        return "retry", delay
