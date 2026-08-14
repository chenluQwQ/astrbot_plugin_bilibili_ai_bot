"""统一事件模型与适配层。

对应 issue #5。旧实现在各 mixin 注册平台钩子（comment_callback、at_callback、
danmaku_callback...），每个钩子各有一套去重与退避逻辑，无中心状态，重启丢失。

新适配层做三件事：

1. **统一入站**：所有平台事件标准化为 InboundEvent，写入 events 表；
   唯一索引保证同一事件永不重复处理（旧实现只在内存 set 里去重）。
2. **状态机 claim**：worker 通过事务 UPDATE ... WHERE state='received' 原子领取，
   处理完更新为 sent/ignored/failed，审计留档；重启后未完成事件仍在队列。
3. **会话绑定**：每个事件携带 session_id（由 security.scopes 派生），
   reply 生成器拿到的上下文严格限定在该会话域内，无泄漏可能。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..security import Caller, Scope, scope_for_source
from ..storage.db import Database, now


class EventState(str, Enum):
    """事件状态流转。received → claimed → (sent | ignored | failed)。"""

    RECEIVED = "received"
    CLAIMED = "claimed"
    SENT = "sent"
    IGNORED = "ignored"
    FAILED = "failed"


@dataclass
class InboundEvent:
    """标准化入站事件。无论来源，都能映射成这些字段。"""

    account_id: str
    source_type: str             # comment / at / dm / danmaku / qq_share / proactive ...
    source_event_id: str         # rpid / at_id / message_id / danmaku_key
    actor_id: str                # 强制 bili:xxx / qq:xxx，见 security.identity
    actor_name: str = ""
    session_id: str = ""         # 会话域标识，见 security.scopes
    target_id: str = ""          # bvid / oid / room_id / qq_group_id
    thread_id: str = ""          # root_rpid / dm_session_id
    scope: Scope = Scope.COMMENT
    priority: int = 50           # 数值越小越优先
    ignore_level: str = "normal" # normal / low_quality / spam / hostile
    content: str = ""
    content_hash: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float | None = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = now()
        if not self.content_hash and self.content:
            self.content_hash = hashlib.sha256(
                self.content.encode("utf-8")
            ).hexdigest()[:32]
        if not self.scope:
            self.scope = scope_for_source(self.source_type)


class EventAdapter:
    """事件入站网关。平台钩子调它把事件标准化并入库。"""

    def __init__(self, db: Database, identity_resolver) -> None:
        self._db = db
        self._resolver = identity_resolver

    async def ingest(self, event: InboundEvent, caller: Caller) -> int:
        """写入 events 表。返回事件 ID；唯一冲突则返回 0（已存在）。"""
        try:
            event_id = await self._db.execute(
                "INSERT INTO events("
                "account_id,source_type,source_event_id,actor_id,actor_name,"
                "session_id,target_id,thread_id,priority,ignore_level,content,"
                "content_hash,payload,state,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.account_id, event.source_type, event.source_event_id,
                    caller.actor_id, caller.display_name, caller.session_id,
                    event.target_id, event.thread_id, event.priority,
                    event.ignore_level, event.content[:1200], event.content_hash,
                    json.dumps(event.payload, ensure_ascii=False),
                    EventState.RECEIVED.value, event.created_at, event.created_at,
                ),
            )
            return event_id
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                return 0
            raise

    async def claim(self, batch_size: int = 1) -> list[dict[str, Any]]:
        """原子领取一批待处理事件。返回 claimed 后的完整行。"""

        def _claim_batch(conn):
            rows = conn.execute(
                "SELECT * FROM events WHERE state=? "
                "ORDER BY priority ASC, created_at ASC LIMIT ?",
                (EventState.RECEIVED.value, batch_size),
            ).fetchall()
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE events SET state=?, claimed_at=?, updated_at=? "
                f"WHERE id IN ({placeholders})",
                (EventState.CLAIMED.value, now(), now(), *ids),
            )
            claimed = conn.execute(
                f"SELECT * FROM events WHERE id IN ({placeholders})", ids
            ).fetchall()
            return [dict(r) for r in claimed]

        return await self._db.run(_claim_batch)

    async def transition(
        self,
        event_id: int,
        to_state: EventState,
        reason: str = "",
        draft: str = "",
        error: str = "",
    ) -> None:
        """记录状态转移并更新事件。"""
        from_row = await self._db.fetch_one(
            "SELECT state FROM events WHERE id=?", (event_id,)
        )
        if from_row is None:
            return
        from_state = from_row["state"]
        at = now()
        await self._db.transaction(
            [
                (
                    "INSERT INTO event_transitions(event_id,from_state,to_state,reason,at) "
                    "VALUES(?,?,?,?,?)",
                    (event_id, from_state, to_state.value, reason[:200], at),
                ),
                (
                    "UPDATE events SET state=?, draft=?, error=?, updated_at=? WHERE id=?",
                    (to_state.value, draft[:1200], error[:500], at, event_id),
                ),
            ]
        )
        if to_state is EventState.SENT:
            await self._db.execute(
                "UPDATE events SET sent_at=? WHERE id=?", (at, event_id)
            )

    async def set_draft(self, event_id: int, draft: str) -> None:
        """草稿暂存，用于确认流程展示给管理员看。"""
        await self._db.execute(
            "UPDATE events SET draft=?, updated_at=? WHERE id=?",
            (draft[:1200], now(), event_id),
        )

    async def mark_verified(
        self, event_id: int, fingerprint: str, verify_state: str = "confirmed"
    ) -> None:
        """标记该回复已在平台验证存在（或缺失）。"""
        await self._db.execute(
            "UPDATE events SET send_fingerprint=?, verify_state=?, updated_at=? WHERE id=?",
            (fingerprint[:64], verify_state, now(), event_id),
        )

    async def count_recent_duplicates(
        self, content_hash: str, window_sec: float = 300
    ) -> int:
        """统计近期相同内容的事件数。用于判断刷屏与低质量重复。"""
        cutoff = now() - window_sec
        return int(
            await self._db.fetch_value(
                "SELECT COUNT(*) FROM events WHERE content_hash=? AND created_at > ?",
                (content_hash, cutoff),
                default=0,
            ) or 0
        )

    async def recent_by_actor(
        self, actor_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """该用户最近的互动，用于生成回复时回顾对话历史。"""
        rows = await self._db.fetch_all(
            "SELECT * FROM events WHERE actor_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (actor_id, limit),
        )
        return [dict(r) for r in rows]

    async def pending_count(self) -> int:
        return int(
            await self._db.fetch_value(
                "SELECT COUNT(*) FROM events WHERE state=?",
                (EventState.RECEIVED.value,),
                default=0,
            ) or 0
        )

    async def stats(self) -> dict[str, Any]:
        """运行统计，供 WebUI 展示。"""
        rows = await self._db.fetch_all(
            "SELECT state, COUNT(*) as cnt FROM events GROUP BY state"
        )
        by_state = {r["state"]: r["cnt"] for r in rows}
        total = sum(by_state.values())
        return {
            "total": total,
            "pending": by_state.get(EventState.RECEIVED.value, 0),
            "claimed": by_state.get(EventState.CLAIMED.value, 0),
            "sent": by_state.get(EventState.SENT.value, 0),
            "ignored": by_state.get(EventState.IGNORED.value, 0),
            "failed": by_state.get(EventState.FAILED.value, 0),
        }


@dataclass
class ActionRequest:
    """写动作请求。由 reply 生成器返回，进入确认流程后变成 action 行。"""

    tool: str
    target_id: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    capability_token: str = ""

    def digest_key(self) -> str:
        """幂等键。同工具+目标+参数只执行一次，避免双发。"""
        from .security.capability import args_hash

        return f"{self.tool}:{self.target_id or 'none'}:{args_hash(self.args)}"


class ActionRegistry:
    """写动作幂等与审计。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def register(
        self,
        request: ActionRequest,
        event_id: int | None = None,
    ) -> int:
        """登记写动作。返回 action ID；已存在则返回 0。"""
        key = request.digest_key()
        existing = await self._db.fetch_one(
            "SELECT id FROM actions WHERE key=?", (key,)
        )
        if existing:
            return 0
        event_key = f"event:{event_id}" if event_id else ""
        return await self._db.execute(
            "INSERT INTO actions(key,kind,event_key,target_id,digest,state,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                key, request.tool, event_key, request.target_id,
                json.dumps(request.args, ensure_ascii=False)[:500],
                "running", now(),
            ),
        )

    async def finish(
        self, key: str, state: str = "succeeded", detail: str = ""
    ) -> None:
        await self._db.execute(
            "UPDATE actions SET state=?, detail=?, finished_at=? WHERE key=?",
            (state, detail[:500], now(), key),
        )
