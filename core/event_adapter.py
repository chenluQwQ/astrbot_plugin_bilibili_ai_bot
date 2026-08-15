"""兼容旧 ``core.event_adapter`` 导入的事件适配器。

新代码请优先使用 :mod:`core.adapter.events` 中的 ``EventAdapter``、
``InboundEvent`` 和 ``Caller``。本模块保留旧测试/旧扩展使用的便捷 API，
内部仍写入四层重构后的统一 ``events`` 表。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .storage import Database, now


class EventAdapter:
    """Legacy facade around the unified ``events`` table.

    旧版测试和外部扩展使用 ``ingest_message_event`` / ``claim_event`` /
    ``complete_event`` / ``fail_event``。四层重构后真实状态仍落在同一张
    ``events`` 表，因此这里提供一个薄兼容层，避免旧导入直接失败。
    """

    def __init__(self, db: Database, claim_timeout: float = 1800) -> None:
        self.db = db
        self.claim_timeout = claim_timeout

    async def ingest_message_event(
        self,
        event: Any,
        platform_id: str | None = None,
        source_type: str = "message",
        account_id: str = "default",
        **extra: Any,
    ) -> str:
        """标准化并入库一条旧 ``AstrMessageEvent`` 风格事件。"""
        created_at = now()
        content = (
            getattr(event, "message_str", "") or getattr(event, "raw_message", "") or ""
        )
        source_event_id = (
            platform_id
            or getattr(event, "message_id", "")
            or f"legacy:{hashlib.sha256(repr(event).encode('utf-8')).hexdigest()[:16]}"
        )
        session_id = (
            getattr(event, "session_id", "")
            or getattr(event, "unified_msg_origin", "")
            or "legacy"
        )
        actor_id = (
            getattr(event, "user_id", "")
            or getattr(event, "sender_id", "")
            or session_id
        )
        actor_name = (
            getattr(event, "sender_name", "") or getattr(event, "nickname", "") or ""
        )
        target_id = (
            getattr(event, "group_id", "") or getattr(event, "room_id", "") or ""
        )
        payload = {
            "message_type": str(getattr(event, "message_type", "")),
            "message_id": str(getattr(event, "message_id", "")),
            "raw_message": str(getattr(event, "raw_message", "")),
            "unified_msg_origin": str(getattr(event, "unified_msg_origin", "")),
            **extra,
        }
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]

        event_id = await self.db.execute(
            "INSERT INTO events("
            "account_id,source_type,source_event_id,actor_id,actor_name,"
            "session_id,target_id,thread_id,priority,ignore_level,content,"
            "content_hash,payload,state,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                account_id,
                source_type,
                source_event_id,
                str(actor_id),
                str(actor_name),
                str(session_id),
                str(target_id),
                "",
                50,
                "normal",
                content[:1200],
                content_hash,
                json.dumps(payload, ensure_ascii=False),
                "received",
                created_at,
                created_at,
            ),
        )
        return f"evt_{event_id}"

    async def claim_event(
        self, source_types: Iterable[str] | None = None
    ) -> dict[str, Any] | None:
        """领取一条待处理事件，超时的 claimed 事件会被回收。"""
        source_types_tuple = tuple(source_types or ())
        cutoff = now() - self.claim_timeout

        def _claim(conn):
            params: list[Any] = ["received", "claimed", cutoff]
            where = "(state=? OR (state=? AND COALESCE(claimed_at, 0) < ?))"
            if source_types_tuple:
                placeholders = ",".join("?" * len(source_types_tuple))
                where += f" AND source_type IN ({placeholders})"
                params.extend(source_types_tuple)

            row = conn.execute(
                f"SELECT * FROM events WHERE {where} "
                "ORDER BY priority ASC, created_at ASC LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return None
            at = now()
            conn.execute(
                "UPDATE events SET state=?, claimed_at=?, updated_at=? WHERE id=?",
                ("claimed", at, at, row["id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM events WHERE id=?", (row["id"],)
            ).fetchone()
            result = dict(claimed)
            result["event_id"] = f"evt_{result['id']}"
            result["status"] = result.get("state")
            return result

        return await self.db.run(_claim)

    async def complete_event(self, event_id: str | int, draft: str = "") -> None:
        numeric_id = self._numeric_id(event_id)
        await self.db.transaction(
            [
                (
                    "INSERT INTO event_transitions(event_id,from_state,to_state,reason,at) "
                    "VALUES(?,?,?,?,?)",
                    (numeric_id, "claimed", "sent", "legacy complete", now()),
                ),
                (
                    "UPDATE events SET state=?, draft=?, error='', updated_at=?, sent_at=? "
                    "WHERE id=?",
                    ("sent", draft[:1200], now(), now(), numeric_id),
                ),
            ]
        )

    async def fail_event(self, event_id: str | int, error: str = "") -> None:
        numeric_id = self._numeric_id(event_id)
        await self.db.transaction(
            [
                (
                    "INSERT INTO event_transitions(event_id,from_state,to_state,reason,at) "
                    "VALUES(?,?,?,?,?)",
                    (numeric_id, "claimed", "failed", "legacy fail", now()),
                ),
                (
                    "UPDATE events SET state=?, error=?, updated_at=? WHERE id=?",
                    ("failed", error[:500], now(), numeric_id),
                ),
            ]
        )

    @staticmethod
    def _numeric_id(event_id: str | int) -> int:
        if isinstance(event_id, int):
            return event_id
        return int(str(event_id).removeprefix("evt_"))
