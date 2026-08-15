"""适配层：平台事件 → 标准化入站 → 状态机处理。

对应 issue #5。
"""

from .events import (
    ActionRegistry,
    ActionRequest,
    EventAdapter,
    EventState,
    InboundEvent,
)

__all__ = [
    "ActionRegistry",
    "ActionRequest",
    "EventAdapter",
    "EventState",
    "InboundEvent",
]
