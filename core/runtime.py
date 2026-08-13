"""统一入站事件与平台动作运行时。

这个模块刻意不依赖 AstrBot 或 B 站客户端：平台模块负责把原始数据转换为
``InboundEvent``，运行时只负责生命周期、进程内去重和动作幂等。这样评论、
私信、直播弹幕可以逐步迁移，而不需要一次性重写现有业务逻辑。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional


class EventPriority(IntEnum):
    """跨入口统一的事件优先级；数值越小越先处理。"""

    ADMIN = 0
    DIRECT_MENTION = 10
    ACTIVE_CONVERSATION = 20
    INTERESTING = 30
    NORMAL = 40
    BACKGROUND = 50


class EventState(str, Enum):
    RECEIVED = "received"
    PROCESSING = "processing"
    IGNORED = "ignored"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


class ActionState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class InboundEvent:
    """与平台 SDK 解耦的最小入站事件。"""

    source: str
    event_id: str
    actor_id: str
    actor_name: str = ""
    content: str = ""
    conversation_id: str = ""
    target_id: str = ""
    account_id: str = ""
    occurred_at: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    priority: int | EventPriority | None = None
    platform: str = "bilibili"

    def __post_init__(self):
        source = str(self.source or "").strip().lower()
        event_id = str(self.event_id or "").strip()
        actor_id = str(self.actor_id or "").strip()
        if not source:
            raise ValueError("event source cannot be empty")
        if not event_id:
            raise ValueError("event id cannot be empty")
        if not actor_id:
            raise ValueError("event actor id cannot be empty")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "actor_name", str(self.actor_name or "").strip())
        object.__setattr__(self, "content", str(self.content or "").strip())
        object.__setattr__(
            self, "conversation_id", str(self.conversation_id or "").strip()
        )
        object.__setattr__(self, "target_id", str(self.target_id or "").strip())
        object.__setattr__(self, "account_id", str(self.account_id or "").strip())
        object.__setattr__(self, "platform", str(self.platform or "bilibili").strip())
        metadata = dict(self.metadata or {})
        object.__setattr__(self, "metadata", metadata)
        priority = self.priority
        if priority is None:
            if metadata.get("is_admin"):
                priority = EventPriority.ADMIN
            elif metadata.get("direct_mention"):
                priority = EventPriority.DIRECT_MENTION
            elif metadata.get("conversation_active"):
                priority = EventPriority.ACTIVE_CONVERSATION
            elif metadata.get("interesting"):
                priority = EventPriority.INTERESTING
            elif metadata.get("background"):
                priority = EventPriority.BACKGROUND
            else:
                priority = EventPriority.NORMAL
        try:
            priority = max(0, min(100, int(priority)))
        except (TypeError, ValueError):
            priority = int(EventPriority.NORMAL)
        object.__setattr__(self, "priority", priority)
        if not self.occurred_at:
            object.__setattr__(self, "occurred_at", time.time())

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.source}:{self.event_id}"

    @property
    def priority_name(self) -> str:
        try:
            return EventPriority(int(self.priority)).name.lower()
        except ValueError:
            return f"custom_{int(self.priority)}"


@dataclass(frozen=True)
class ActionRequest:
    """一次有外部副作用的平台动作，例如回复、拉黑或分享。"""

    key: str
    kind: str
    event_key: str = ""
    target_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        key = str(self.key or "").strip()
        kind = str(self.kind or "").strip().lower()
        if not key:
            raise ValueError("action key cannot be empty")
        if not kind:
            raise ValueError("action kind cannot be empty")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "event_key", str(self.event_key or "").strip())
        object.__setattr__(self, "target_id", str(self.target_id or "").strip())
        object.__setattr__(self, "metadata", dict(self.metadata or {}))


@dataclass(frozen=True)
class EventClaim:
    accepted: bool
    event_key: str
    reason: str = ""


@dataclass(frozen=True)
class ActionOutcome:
    success: bool
    action_key: str
    value: Any = None
    reason: str = ""
    duplicate: bool = False


@dataclass
class _EventRecord:
    event: InboundEvent
    state: EventState
    reason: str = ""
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)


@dataclass
class _ActionRecord:
    request: ActionRequest
    state: ActionState
    value: Any = None
    reason: str = ""
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)


SuccessPredicate = Callable[[Any], bool]
ActionHandler = Callable[[], Any | Awaitable[Any]]


class EventRuntime:
    """统一维护事件生命周期与外部动作幂等。

    当前实现只保存短期进程内状态。原有持久化去重文件继续作为跨重启保护；待
    后续存储层稳定后，可在不改变平台模块的前提下替换这里的记录后端。
    """

    def __init__(
        self,
        *,
        event_ttl: float = 6 * 3600,
        action_ttl: float = 24 * 3600,
        max_events: int = 2000,
        max_actions: int = 2000,
    ):
        self.event_ttl = max(60.0, float(event_ttl))
        self.action_ttl = max(60.0, float(action_ttl))
        self.max_events = max(100, int(max_events))
        self.max_actions = max(100, int(max_actions))
        self._events: OrderedDict[str, _EventRecord] = OrderedDict()
        self._actions: OrderedDict[str, _ActionRecord] = OrderedDict()
        self._recent_failures = deque(maxlen=50)
        self._lock = asyncio.Lock()

    def _prune_locked(self, now: Optional[float] = None):
        now = time.monotonic() if now is None else now
        while self._events:
            key, record = next(iter(self._events.items()))
            if len(self._events) <= self.max_events and now - record.updated_at <= self.event_ttl:
                break
            self._events.pop(key, None)
        while self._actions:
            key, record = next(iter(self._actions.items()))
            if len(self._actions) <= self.max_actions and now - record.updated_at <= self.action_ttl:
                break
            self._actions.pop(key, None)

    @staticmethod
    def event_sort_key(
        event: InboundEvent,
        *,
        newest_first: bool = False,
    ) -> tuple[int, float, str]:
        """返回稳定排序键：先按优先级，再按事件时间。"""

        occurred_at = float(event.occurred_at or 0.0)
        return (
            int(event.priority),
            -occurred_at if newest_first else occurred_at,
            event.key,
        )

    def rank_events(
        self,
        events: Iterable[InboundEvent],
        *,
        newest_first: bool = False,
    ) -> list[InboundEvent]:
        """按统一优先级排列一个轮询批次，不改变调用方的持久化队列。"""

        return sorted(
            list(events),
            key=lambda event: self.event_sort_key(
                event, newest_first=newest_first
            ),
        )

    async def claim(
        self,
        event: InboundEvent,
        *,
        allow_retry_failed: bool = False,
    ) -> EventClaim:
        """领取事件；TTL 内相同事件默认只允许处理一次。

        持久化收件箱明确安排的失败重试可设置 ``allow_retry_failed``。只有已经
        标记为失败的事件能重新进入处理中；已发送、已忽略或仍在处理的事件不会
        被重复领取。
        """

        now = time.monotonic()
        async with self._lock:
            self._prune_locked(now)
            existing = self._events.get(event.key)
            if existing is not None:
                self._events.move_to_end(event.key)
                if allow_retry_failed and existing.state == EventState.FAILED:
                    existing.state = EventState.PROCESSING
                    existing.reason = "retry"
                    existing.updated_at = now
                    return EventClaim(True, event.key, "retry")
                return EventClaim(False, event.key, f"duplicate:{existing.state.value}")
            self._events[event.key] = _EventRecord(
                event=event,
                state=EventState.PROCESSING,
                created_at=now,
                updated_at=now,
            )
            self._prune_locked(now)
            return EventClaim(True, event.key)

    async def transition(
        self,
        event_key: str,
        state: EventState | str,
        reason: str = "",
    ) -> bool:
        try:
            normalized_state = state if isinstance(state, EventState) else EventState(state)
        except ValueError:
            raise ValueError(f"unknown event state: {state}") from None
        now = time.monotonic()
        async with self._lock:
            record = self._events.get(str(event_key or ""))
            if record is None:
                return False
            record.state = normalized_state
            record.reason = str(reason or "")[:300]
            record.updated_at = now
            self._events.move_to_end(event_key)
            if normalized_state == EventState.FAILED:
                self._recent_failures.append(
                    {
                        "kind": "event",
                        "key": event_key,
                        "reason": record.reason,
                        "at": time.time(),
                    }
                )
            return True

    async def execute(
        self,
        request: ActionRequest,
        handler: ActionHandler,
        *,
        success: Optional[SuccessPredicate] = None,
    ) -> ActionOutcome:
        """执行一次幂等动作。

        相同 ``request.key`` 已成功时不会再次调用平台；正在执行时也不会并发
        重复发送。失败动作允许下一轮重新尝试。
        """

        now = time.monotonic()
        async with self._lock:
            self._prune_locked(now)
            previous = self._actions.get(request.key)
            if previous and previous.state == ActionState.SUCCEEDED:
                self._actions.move_to_end(request.key)
                return ActionOutcome(
                    True,
                    request.key,
                    previous.value,
                    "already_succeeded",
                    duplicate=True,
                )
            if previous and previous.state == ActionState.RUNNING:
                self._actions.move_to_end(request.key)
                return ActionOutcome(
                    False,
                    request.key,
                    reason="already_running",
                    duplicate=True,
                )
            self._actions[request.key] = _ActionRecord(
                request=request,
                state=ActionState.RUNNING,
                created_at=now,
                updated_at=now,
            )
            self._actions.move_to_end(request.key)
            event_record = self._events.get(request.event_key)
            if event_record is not None:
                event_record.state = EventState.SENDING
                event_record.updated_at = now

        try:
            value = handler()
            if inspect.isawaitable(value):
                value = await value
            succeeded = success(value) if success is not None else bool(value)
            reason = "" if succeeded else "handler_returned_unsuccessful"
        except asyncio.CancelledError:
            await self._finish_action(request, False, None, "cancelled")
            raise
        except Exception as exc:
            await self._finish_action(request, False, None, str(exc))
            return ActionOutcome(False, request.key, reason=str(exc))

        await self._finish_action(request, succeeded, value, reason)
        return ActionOutcome(succeeded, request.key, value=value, reason=reason)

    async def _finish_action(
        self,
        request: ActionRequest,
        succeeded: bool,
        value: Any,
        reason: str,
    ):
        now = time.monotonic()
        async with self._lock:
            record = self._actions.get(request.key)
            if record is None:
                record = _ActionRecord(request=request, state=ActionState.RUNNING)
                self._actions[request.key] = record
            record.state = ActionState.SUCCEEDED if succeeded else ActionState.FAILED
            record.value = value
            record.reason = str(reason or "")[:300]
            record.updated_at = now
            self._actions.move_to_end(request.key)
            event_record = self._events.get(request.event_key)
            if event_record is not None:
                event_record.state = EventState.SENT if succeeded else EventState.FAILED
                event_record.reason = record.reason
                event_record.updated_at = now
                self._events.move_to_end(request.event_key)
            if not succeeded:
                self._recent_failures.append(
                    {
                        "kind": request.kind,
                        "key": request.key,
                        "reason": record.reason,
                        "at": time.time(),
                    }
                )
            self._prune_locked(now)

    async def snapshot(self) -> dict[str, Any]:
        """返回可供状态命令和未来 WebUI 使用的脱敏运行快照。"""

        async with self._lock:
            self._prune_locked()
            event_states = {state.value: 0 for state in EventState}
            action_states = {state.value: 0 for state in ActionState}
            event_priorities: dict[str, int] = {}
            for record in self._events.values():
                event_states[record.state.value] += 1
                label = record.event.priority_name
                event_priorities[label] = event_priorities.get(label, 0) + 1
            for record in self._actions.values():
                action_states[record.state.value] += 1
            recent_events = [
                {
                    "source": record.event.source,
                    "state": record.state.value,
                    "priority": record.event.priority_name,
                    "reason": record.reason,
                    "updated_ago": max(0, int(time.monotonic() - record.updated_at)),
                }
                for record in list(self._events.values())[-50:]
            ]
            return {
                "events": len(self._events),
                "event_states": event_states,
                "event_priorities": event_priorities,
                "actions": len(self._actions),
                "action_states": action_states,
                "recent_events": recent_events,
                "recent_failures": list(self._recent_failures),
            }
