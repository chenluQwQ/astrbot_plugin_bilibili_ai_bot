"""Runtime wiring for the new adapter/security/storage/persona layers.

The established :mod:`core.runtime` remains the low-latency dispatcher used by
comments, private messages and live danmaku.  This module is its persistent
observer: it adds cross-restart event/action idempotency, namespaced identities,
auditing and persona/profile state without changing platform reply behaviour.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

from .adapter.events import EventAdapter, EventState as StoredEventState
from .adapter.events import InboundEvent as StoredInboundEvent
from .behavior_budget import BehaviorBudget
from .persona import PersonaEngine
from .runtime import ActionRequest, ActionState, EventState, InboundEvent
from .security import (
    CapabilityStore,
    IdentityResolver,
    PendingConfirmations,
    SessionKey,
    ToolGate,
    ToolSpec,
    Tier,
    scope_for_source,
)
from .storage import (
    Database, FeedbackStore, MediaStore, MemoryStore, PreferenceStore, ProfileStore,
    SeenVideoStore, now,
)


_WRITE_TOOLS = {
    "bili_action",
    "bili_parse_video",
    "watch_and_share_video_private",
}
_PRIVATE_READ_TOOLS = {
    "recall_user",
    "recall_conversation",
    "recall_today",
    "recall_video",
    "recall_dynamic",
    "recall_bangumi",
    "check_following_updates",
    "check_following_live",
    "watch_video",
}


class LayeredRuntime:
    """Own and connect the refactored services for one plugin instance."""

    def __init__(self, config: Any, db_path: str | Path) -> None:
        self.config = config
        self.db = Database(str(db_path))
        self.identity = IdentityResolver(self._get)
        self.events = EventAdapter(self.db, self.identity)
        self.memories = MemoryStore(self.db)
        self.profiles = ProfileStore(self.db)
        self.seen_videos = SeenVideoStore(self.db)
        self.feedback = FeedbackStore(self.db)
        self.preferences = PreferenceStore(self.db)
        self.media = MediaStore(self.db)
        self.persona = PersonaEngine(self.db, self._get)
        self.capabilities = CapabilityStore(self.db)
        self.pending_confirmations = PendingConfirmations(self.db)
        self.tool_gate = ToolGate(self._get, audit=self._audit)
        self.behavior_budget = BehaviorBudget(self._get)
        self._event_ids: dict[str, int] = {}

    @property
    def is_open(self) -> bool:
        return self.db.is_open

    def _get(self, key: str, default: Any = None) -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            data = getattr(self.config, "data", {})
            return data.get(key, default) if isinstance(data, dict) else default

    async def open(self) -> None:
        await self.db.open()
        await self._recover_interrupted_actions()
        # Create the initial state eagerly so WebUI never has to guess whether the
        # persona layer exists. Day-plan generation remains lazy and side-effect free.
        await self.persona.snapshot()

    async def _recover_interrupted_actions(self) -> None:
        """Resolve queue state left by an unclean plugin stop.

        Queued work never reached a handler and is safe to retry, so its reservation
        is refunded. A running request may already have reached Bilibili and becomes
        unknown; it is deliberately not retried or refunded automatically.
        """

        def _recover(conn):
            at = now()
            queued = conn.execute(
                "SELECT key,budget FROM actions WHERE state='queued'"
            ).fetchall()
            for row in queued:
                try:
                    reservations = json.loads(row["budget"] or "[]")
                except (TypeError, ValueError):
                    reservations = []
                self.behavior_budget.refund_in_transaction(conn, reservations, at)
                conn.execute(
                    "UPDATE actions SET state='failed',budget='[]',detail=?,"
                    "updated_at=?,finished_at=? WHERE key=?",
                    ("restart_before_send", at, at, row["key"]),
                )
            running = conn.execute(
                "SELECT key FROM actions WHERE state='running'"
            ).fetchall()
            for row in running:
                conn.execute(
                    "UPDATE actions SET state='unknown',detail=?,updated_at=?,"
                    "finished_at=? WHERE key=?",
                    ("restart_during_send", at, at, row["key"]),
                )
            return {"failed_queued": len(queued), "unknown_running": len(running)}

        await self.db.run(_recover)

    async def close(self) -> None:
        self._event_ids.clear()
        await self.db.close()

    def register_tools(self, tools: Iterable[Any]) -> None:
        """Mirror AstrBot FunctionTools into the declarative security catalogue."""

        for tool in tools:
            name = str(getattr(tool, "name", "") or "").strip()
            if not name:
                continue
            if name in _WRITE_TOOLS:
                tier, read_only, needs_capability = Tier.WRITE, False, True
            elif name in _PRIVATE_READ_TOOLS:
                tier, read_only, needs_capability = Tier.PRIVATE_READ, True, False
            else:
                tier, read_only, needs_capability = Tier.PUBLIC_READ, True, False
            self.tool_gate.register(
                ToolSpec(
                    name=name,
                    tier=tier,
                    description=str(getattr(tool, "description", "") or "")[:500],
                    handler=getattr(tool, "call", None) or (lambda **_: None),
                    parameters=dict(getattr(tool, "parameters", {}) or {}),
                    read_only=read_only,
                    needs_capability=needs_capability,
                )
            )

    async def _audit(
        self,
        *,
        kind: str,
        tool: str = "",
        caller_id: str = "",
        session_id: str = "",
        scope: str = "",
        decision: str = "",
        detail: str = "",
    ) -> None:
        if not self.is_open:
            return
        await self.db.execute(
            "INSERT INTO audit_log(kind,tool,caller_id,session_id,scope,decision,detail,at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                str(kind)[:50],
                str(tool)[:80],
                str(caller_id)[:100],
                str(session_id)[:240],
                str(scope)[:50],
                str(decision)[:80],
                str(detail)[:500],
                now(),
            ),
        )

    @staticmethod
    def _platform_name(event: InboundEvent) -> str:
        platform = str(event.platform or "bilibili").strip().lower()
        if platform in {"bilibili", "bili"}:
            return "bili"
        if platform in {"qq", "aiocqhttp", "onebot"}:
            return "qq"
        return platform or "bili"

    def _stored_event(self, event: InboundEvent):
        scope = scope_for_source(event.source)
        account_id = str(event.account_id or self._get("DEDE_USER_ID", "") or "default")
        thread_id = str(event.conversation_id or "")
        session_key = SessionKey(
            scope=scope,
            key=thread_id or event.target_id or event.actor_id,
            account_id=account_id,
        ).render()
        platform = self._platform_name(event)
        raw_actor_id = str(event.actor_id or "")
        if ":" in raw_actor_id:
            actor_platform, _, actor_value = raw_actor_id.partition(":")
            if actor_platform.lower() in {"bili", "bilibili", "qq", "sys"}:
                platform = (
                    "bili"
                    if actor_platform.lower() == "bilibili"
                    else actor_platform.lower()
                )
                raw_actor_id = actor_value
        caller = self.identity.resolve(
            platform,
            raw_actor_id,
            scope,
            display_name=event.actor_name,
            account_id=account_id,
            session_id=session_key,
        )
        payload = json.loads(
            json.dumps(
                {"runtime_key": event.key, **dict(event.metadata or {})},
                ensure_ascii=False,
                default=str,
            )
        )
        stored = StoredInboundEvent(
            account_id=account_id,
            source_type=event.source,
            source_event_id=event.event_id,
            actor_id=caller.actor_id,
            actor_name=caller.display_name,
            session_id=session_key,
            target_id=event.target_id,
            thread_id=thread_id,
            scope=scope,
            priority=int(event.priority),
            content=event.content,
            payload=payload,
            created_at=float(event.occurred_at or time.time()),
        )
        return stored, caller

    async def before_claim(
        self, event: InboundEvent, allow_retry_failed: bool = False
    ) -> tuple[bool, str] | None:
        """Reserve an event in SQLite before the in-memory runtime accepts it."""

        if not self.is_open:
            return None
        stored, caller = self._stored_event(event)
        event_id, accepted, reason = await self.events.claim_event(
            stored, caller, allow_retry_failed=allow_retry_failed
        )
        self._event_ids[event.key] = event_id
        # 领取事件只证明“看见了”，不代表已经与用户形成互动。用户画像由平台
        # 确认回复成功后的业务提交更新，避免 SQLite 影子画像与实际 JSON 画像
        # 分叉，也避免未回复/被过滤的陌生人出现在 WebUI 画像列表。
        return accepted, reason

    async def _touch_profile(self, actor_id: str, display_name: str) -> None:
        profile = await self.profiles.get(actor_id)
        if profile is None:
            await self.profiles.upsert(actor_id, display_name=display_name)
            await self.db.execute(
                "UPDATE profiles SET interact_count=1 WHERE actor_id=?",
                (actor_id,),
            )
            return
        await self.profiles.upsert(
            actor_id,
            display_name=display_name,
            delta={"interact_count": profile.interact_count + 1},
        )

    async def _event_id(self, event: InboundEvent) -> int | None:
        known = self._event_ids.get(event.key)
        if known is not None:
            return known
        stored, _ = self._stored_event(event)
        row = await self.db.fetch_one(
            "SELECT id FROM events WHERE account_id=? AND source_type=? "
            "AND source_event_id=?",
            (stored.account_id, stored.source_type, stored.source_event_id),
        )
        if row is None:
            return None
        event_id = int(row["id"])
        self._event_ids[event.key] = event_id
        return event_id

    async def on_event_transition(
        self, event: InboundEvent, state: EventState, reason: str = ""
    ) -> None:
        if not self.is_open:
            return
        event_id = await self._event_id(event)
        if event_id is None:
            return
        target = {
            EventState.PROCESSING: StoredEventState.CLAIMED,
            EventState.SENDING: StoredEventState.CLAIMED,
            EventState.SENT: StoredEventState.SENT,
            EventState.IGNORED: StoredEventState.IGNORED,
            EventState.FAILED: StoredEventState.FAILED,
        }.get(state)
        if target is None:
            return
        row = await self.db.fetch_one(
            "SELECT state FROM events WHERE id=?", (event_id,)
        )
        if row is None or str(row["state"]) == target.value:
            return
        await self.events.transition(
            event_id,
            target,
            reason=reason,
            error=reason if target is StoredEventState.FAILED else "",
        )
        if target is StoredEventState.SENT:
            await self.persona.consume_reply()

    async def before_action(
        self, request: ActionRequest
    ) -> tuple[bool, str, bool, str] | None:
        """Persist a queued action and atomically reserve its behaviour budgets."""

        if not self.is_open:
            return None
        def _reserve(conn):
            row = conn.execute(
                "SELECT state FROM actions WHERE key=?", (request.key,)
            ).fetchone()
            at = now()
            if row is not None:
                state = str(row["state"])
                if state == "succeeded":
                    return False, "already_succeeded", True, state
                if state in {"queued", "running"}:
                    return False, f"already_{state}", False, state
                if state == "unknown":
                    return False, "send_state_unknown", False, state

            allowed, reason, reservations = self.behavior_budget.reserve_in_transaction(
                conn, request, at
            )
            if not allowed:
                return False, reason, False, "failed"
            budget_json = json.dumps(
                [reservation.as_dict() for reservation in reservations],
                ensure_ascii=False,
            )
            digest = json.dumps(
                dict(request.metadata or {}), ensure_ascii=False, default=str
            )[:500]
            if row is not None:
                conn.execute(
                    "UPDATE actions SET kind=?,event_key=?,target_id=?,state='queued',"
                    "priority=?,digest=?,budget=?,detail='',created_at=?,updated_at=?,"
                    "finished_at=NULL WHERE key=?",
                    (
                        request.kind,
                        request.event_key,
                        request.target_id,
                        int(request.priority),
                        digest,
                        budget_json,
                        at,
                        at,
                        request.key,
                    ),
                )
                return True, "retry", False, "queued"
            conn.execute(
                "INSERT INTO actions(key,kind,event_key,target_id,state,priority,"
                "digest,budget,detail,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request.key,
                    request.kind,
                    request.event_key,
                    request.target_id,
                    "queued",
                    int(request.priority),
                    digest,
                    budget_json,
                    "",
                    at,
                    at,
                ),
            )
            return True, "", False, "queued"

        return await self.db.run(_reserve)

    async def on_action_started(self, request: ActionRequest) -> None:
        if not self.is_open:
            return
        await self.db.execute(
            "UPDATE actions SET state='running',attempts=attempts+1,updated_at=? "
            "WHERE key=? AND state='queued'",
            (now(), request.key),
        )

    async def on_action_finished(
        self, request: ActionRequest, state: ActionState | str, reason: str = ""
    ) -> None:
        if not self.is_open:
            return
        normalized = state.value if isinstance(state, ActionState) else str(state)

        def _finish(conn):
            at = now()
            row = conn.execute(
                "SELECT budget FROM actions WHERE key=?", (request.key,)
            ).fetchone()
            budget_json = row["budget"] if row is not None else "[]"
            if normalized == ActionState.FAILED.value:
                try:
                    reservations = json.loads(budget_json or "[]")
                except (TypeError, ValueError):
                    reservations = []
                self.behavior_budget.refund_in_transaction(conn, reservations, at)
                budget_json = "[]"
            conn.execute(
                "UPDATE actions SET state=?,budget=?,detail=?,updated_at=?,"
                "finished_at=? WHERE key=?",
                (
                    normalized,
                    budget_json,
                    str(reason or "")[:500],
                    at,
                    at,
                    request.key,
                ),
            )

        await self.db.run(_finish)

    async def snapshot(self) -> dict[str, Any]:
        if not self.is_open:
            return {"open": False}
        persona = await self.persona.snapshot()
        action_rows = await self.db.fetch_all(
            "SELECT state,COUNT(*) AS count FROM actions GROUP BY state"
        )
        action_states = {
            str(row["state"]): int(row["count"]) for row in action_rows
        }
        day_key, minute_key = self.behavior_budget.window_keys(now())
        counter_rows = await self.db.fetch_all(
            "SELECT bucket,window_key,count FROM counters WHERE window_key IN (?,?)",
            (day_key, minute_key),
        )
        return {
            "open": True,
            "events": await self.events.stats(),
            "tables": await self.db.table_counts(),
            "database_bytes": await self.db.db_size_bytes(),
            "persona": {
                "energy": round(float(persona.energy) * 100),
                "social": round(float(persona.social) * 100),
                "mood": persona.mood,
                "phase": persona.phase,
            },
            "behavior": {
                "action_states": action_states,
                "current_counters": [dict(row) for row in counter_rows],
            },
            "tools": {
                "total": len(self.tool_gate.all_specs()),
                "by_tier": {
                    tier.value: sum(
                        1 for spec in self.tool_gate.all_specs() if spec.tier is tier
                    )
                    for tier in Tier
                },
            },
        }

    async def recent_profiles(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.is_open:
            return []
        rows = await self.db.fetch_all(
            "SELECT * FROM profiles ORDER BY last_seen DESC LIMIT ?", (int(limit),)
        )
        return [dict(row) for row in rows]

    async def security_stats(self) -> dict[str, Any]:
        if not self.is_open:
            return {"today_total": 0, "by_type": {}}
        cutoff = time.time() - 86400
        rows = await self.db.fetch_all(
            "SELECT kind,COUNT(*) AS count FROM audit_log WHERE at>=? GROUP BY kind",
            (cutoff,),
        )
        by_type = {str(row["kind"]): int(row["count"]) for row in rows}
        return {"today_total": sum(by_type.values()), "by_type": by_type}

    async def purge_expired(self) -> dict[str, int]:
        if not self.is_open:
            return {"memories": 0, "profile_facts": 0, "media": 0, "counters": 0}
        return {
            "memories": await self.memories.purge_expired(),
            "profile_facts": await self.profiles.purge_expired_facts(),
            "media": await self.media.purge_expired(),
            "counters": await self.db.execute(
                "DELETE FROM counters WHERE updated_at<?", (now() - 8 * 86400,)
            ),
        }
