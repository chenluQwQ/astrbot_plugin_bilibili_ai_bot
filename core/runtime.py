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
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


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
    priority: int | EventPriority = EventPriority.NORMAL
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
        try:
            priority = max(0, min(100, int(self.priority)))
        except (TypeError, ValueError):
            priority = int(EventPriority.NORMAL)
        object.__setattr__(self, "priority", priority)
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
    state: str = ActionState.FAILED.value


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
        action_timeout: float = 45.0,
        max_events: int = 2000,
        max_actions: int = 2000,
        observer: Any = None,
    ):
        self.event_ttl = max(60.0, float(event_ttl))
        self.action_ttl = max(60.0, float(action_ttl))
        self.action_timeout = max(0.01, float(action_timeout))
        self.max_events = max(100, int(max_events))
        self.max_actions = max(100, int(max_actions))
        self._events: OrderedDict[str, _EventRecord] = OrderedDict()
        self._actions: OrderedDict[str, _ActionRecord] = OrderedDict()
        self._recent_failures = deque(maxlen=50)
        self._lock = asyncio.Lock()
        self._action_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._action_queue_lock = asyncio.Lock()
        self._action_sequence = 0
        self._action_worker_task: asyncio.Task | None = None
        self._observer = observer

    def set_observer(self, observer: Any = None) -> None:
        """Attach an optional persistent observer without coupling this module to it."""

        self._observer = observer

    async def close(self) -> None:
        """Stop the action worker before its persistent observer is closed."""

        async with self._action_queue_lock:
            worker = self._action_worker_task
            if worker is not None and not worker.done():
                worker.cancel()
        if worker is not None:
            try:
                await worker
            except asyncio.CancelledError:
                pass

        pending = []
        async with self._action_queue_lock:
            self._action_worker_task = None
            while not self._action_queue.empty():
                pending.append(self._action_queue.get_nowait())
        for _, _, request, _, _, future in pending:
            reason = "plugin_stopped_before_send"
            await self._finish_action(request, ActionState.FAILED, None, reason)
            if not future.done():
                future.set_result(
                    ActionOutcome(
                        False,
                        request.key,
                        reason=reason,
                        state=ActionState.FAILED.value,
                    )
                )
            self._action_queue.task_done()

    async def _notify_observer(self, method: str, *args: Any) -> Any:
        observer = self._observer
        handler = getattr(observer, method, None) if observer is not None else None
        if handler is None:
            return None
        try:
            result = handler(*args)
            return await result if inspect.isawaitable(result) else result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Persistence/telemetry is fail-open: a damaged auxiliary database must
            # never take the existing comment, DM or live reply chain down with it.
            self._recent_failures.append(
                {
                    "kind": "runtime_observer",
                    "key": method,
                    "reason": str(exc)[:300],
                    "at": time.time(),
                }
            )
            return None

    def _prune_locked(self, now: Optional[float] = None):
        now = time.monotonic() if now is None else now
        while self._events:
            key, record = next(iter(self._events.items()))
            if (
                len(self._events) <= self.max_events
                and now - record.updated_at <= self.event_ttl
            ):
                break
            self._events.pop(key, None)
        while self._actions:
            key, record = next(iter(self._actions.items()))
            if (
                len(self._actions) <= self.max_actions
                and now - record.updated_at <= self.action_ttl
            ):
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
            key=lambda event: self.event_sort_key(event, newest_first=newest_first),
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

        persistent = await self._notify_observer(
            "before_claim", event, allow_retry_failed
        )
        if persistent is not None:
            allowed = (
                bool(persistent[0])
                if isinstance(persistent, tuple)
                else bool(persistent)
            )
            reason = (
                str(persistent[1])
                if isinstance(persistent, tuple) and len(persistent) > 1
                else "persistent_duplicate"
            )
            if not allowed:
                return EventClaim(False, event.key, reason)

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
            normalized_state = (
                state if isinstance(state, EventState) else EventState(state)
            )
        except ValueError:
            raise ValueError(f"unknown event state: {state}") from None
        now = time.monotonic()
        event = None
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
            event = record.event
        await self._notify_observer(
            "on_event_transition", event, normalized_state, record.reason
        )
        return True

    async def execute(
        self,
        request: ActionRequest,
        handler: ActionHandler,
        *,
        success: Optional[SuccessPredicate] = None,
    ) -> ActionOutcome:
        """将一次幂等动作放入统一优先队列并等待执行结果。

        已成功动作不会重发；明确失败可重试；超时或执行中取消会进入
        ``unknown``，不会盲目重试可能已经被平台接收的请求。
        """

        persistent = await self._notify_observer("before_action", request)
        if persistent is not None:
            allowed = (
                bool(persistent[0])
                if isinstance(persistent, tuple)
                else bool(persistent)
            )
            reason = (
                str(persistent[1])
                if isinstance(persistent, tuple) and len(persistent) > 1
                else "persistent_duplicate"
            )
            previous_success = bool(
                persistent[2]
                if isinstance(persistent, tuple) and len(persistent) > 2
                else False
            )
            persistent_state = (
                str(persistent[3])
                if isinstance(persistent, tuple) and len(persistent) > 3
                else (ActionState.SUCCEEDED.value if previous_success else ActionState.FAILED.value)
            )
            if not allowed:
                return ActionOutcome(
                    previous_success,
                    request.key,
                    reason=reason,
                    duplicate=reason.startswith("already_") or persistent_state in {
                        ActionState.QUEUED.value,
                        ActionState.RUNNING.value,
                        ActionState.SUCCEEDED.value,
                        ActionState.UNKNOWN.value,
                    },
                    state=persistent_state,
                )

        now = time.monotonic()
        sending_event = None
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
                    state=ActionState.SUCCEEDED.value,
                )
            if previous and previous.state in {ActionState.QUEUED, ActionState.RUNNING}:
                self._actions.move_to_end(request.key)
                return ActionOutcome(
                    False,
                    request.key,
                    reason=f"already_{previous.state.value}",
                    duplicate=True,
                    state=previous.state.value,
                )
            if previous and previous.state == ActionState.UNKNOWN:
                self._actions.move_to_end(request.key)
                return ActionOutcome(
                    False,
                    request.key,
                    reason="send_state_unknown",
                    duplicate=True,
                    state=ActionState.UNKNOWN.value,
                )
            self._actions[request.key] = _ActionRecord(
                request=request,
                state=ActionState.QUEUED,
                created_at=now,
                updated_at=now,
            )
            self._actions.move_to_end(request.key)

        future = asyncio.get_running_loop().create_future()
        async with self._action_queue_lock:
            self._action_sequence += 1
            self._action_queue.put_nowait(
                (
                    int(request.priority),
                    self._action_sequence,
                    request,
                    handler,
                    success,
                    future,
                )
            )
            if self._action_worker_task is None or self._action_worker_task.done():
                self._action_worker_task = asyncio.create_task(
                    self._action_worker(), name="bilibot-action-worker"
                )
        return await asyncio.shield(future)

    async def _action_worker(self) -> None:
        """Drain queued side effects one at a time, choosing the highest priority."""

        while True:
            async with self._action_queue_lock:
                if self._action_queue.empty():
                    self._action_worker_task = None
                    return
                item = self._action_queue.get_nowait()
            _, _, request, handler, success, future = item
            try:
                outcome = await self._run_queued_action(request, handler, success)
                if not future.done():
                    future.set_result(outcome)
            except asyncio.CancelledError:
                if not future.done():
                    future.set_result(
                        ActionOutcome(
                            False,
                            request.key,
                            reason="send_state_unknown:worker_cancelled",
                            state=ActionState.UNKNOWN.value,
                        )
                    )
                async with self._action_queue_lock:
                    if self._action_worker_task is asyncio.current_task():
                        self._action_worker_task = None
                raise
            except Exception as exc:
                # A runtime bookkeeping failure must not leave the caller waiting
                # forever. Platform handler failures are normally converted by
                # _run_queued_action before they reach this guard.
                if not future.done():
                    future.set_result(
                        ActionOutcome(
                            False,
                            request.key,
                            reason=f"action_worker_failed:{exc}",
                            state=ActionState.FAILED.value,
                        )
                    )
            finally:
                self._action_queue.task_done()

    async def _run_queued_action(
        self,
        request: ActionRequest,
        handler: ActionHandler,
        success: Optional[SuccessPredicate],
    ) -> ActionOutcome:
        now = time.monotonic()
        sending_event = None
        async with self._lock:
            record = self._actions.get(request.key)
            if record is None:
                record = _ActionRecord(request=request, state=ActionState.QUEUED)
                self._actions[request.key] = record
            record.state = ActionState.RUNNING
            record.updated_at = now
            event_record = self._events.get(request.event_key)
            if event_record is not None:
                event_record.state = EventState.SENDING
                event_record.updated_at = now
                sending_event = event_record.event

        await self._notify_observer("on_action_started", request)
        if sending_event is not None:
            await self._notify_observer(
                "on_event_transition", sending_event, EventState.SENDING, ""
            )

        try:
            value = handler()
            if inspect.isawaitable(value):
                value = await asyncio.wait_for(value, timeout=self.action_timeout)
            succeeded = success(value) if success is not None else bool(value)
            state = ActionState.SUCCEEDED if succeeded else ActionState.FAILED
            reason = "" if succeeded else "handler_returned_unsuccessful"
        except asyncio.CancelledError:
            reason = "send_state_unknown:worker_cancelled"
            await self._finish_action(request, ActionState.UNKNOWN, None, reason)
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            reason = f"send_state_unknown:{exc or 'timeout'}"
            await self._finish_action(request, ActionState.UNKNOWN, None, reason)
            return ActionOutcome(
                False,
                request.key,
                reason=reason,
                state=ActionState.UNKNOWN.value,
            )
        except Exception as exc:
            reason = str(exc)
            await self._finish_action(request, ActionState.FAILED, None, reason)
            return ActionOutcome(
                False,
                request.key,
                reason=reason,
                state=ActionState.FAILED.value,
            )

        await self._finish_action(request, state, value, reason)
        return ActionOutcome(
            succeeded,
            request.key,
            value=value,
            reason=reason,
            state=state.value,
        )

    async def _finish_action(
        self,
        request: ActionRequest,
        state: ActionState,
        value: Any,
        reason: str,
    ):
        now = time.monotonic()
        finished_event = None
        succeeded = state == ActionState.SUCCEEDED
        finished_state = EventState.SENT if succeeded else EventState.FAILED
        async with self._lock:
            record = self._actions.get(request.key)
            if record is None:
                record = _ActionRecord(request=request, state=ActionState.RUNNING)
                self._actions[request.key] = record
            record.state = state
            record.value = value
            record.reason = str(reason or "")[:300]
            record.updated_at = now
            self._actions.move_to_end(request.key)
            event_record = self._events.get(request.event_key)
            if event_record is not None:
                event_record.state = finished_state
                event_record.reason = record.reason
                event_record.updated_at = now
                self._events.move_to_end(request.event_key)
                finished_event = event_record.event
            if state in {ActionState.FAILED, ActionState.UNKNOWN}:
                self._recent_failures.append(
                    {
                        "kind": request.kind,
                        "key": request.key,
                        "reason": record.reason,
                        "at": time.time(),
                    }
                )
            self._prune_locked(now)
        await self._notify_observer(
            "on_action_finished", request, state, record.reason
        )
        if finished_event is not None:
            await self._notify_observer(
                "on_event_transition", finished_event, finished_state, record.reason
            )

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
                "queue_depth": self._action_queue.qsize(),
                "action_timeout": self.action_timeout,
                "recent_events": recent_events,
                "recent_failures": list(self._recent_failures),
            }
