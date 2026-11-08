"""BiliBot 核心包。

这里采用 PEP 562 惰性导入：只想用 ``core.security.scopes`` 这类小模块时，不会连带拉起 aiohttp、
astrbot 和整条 B站 API 链路，各层可以独立导入、独立测试。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

#: 属性名 → 所在子模块。
_EXPORTS: dict[str, str] = {
    "ConsolidationEngine": "consolidation",
    "BiliBotMemoryAPI": "memory_api",
    "LayeredRuntime": "layered_runtime",
    "ActionRequest": "runtime",
    "EventPriority": "runtime",
    "EventRuntime": "runtime",
    "EventState": "runtime",
    "InboundEvent": "runtime",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """按需导入。未登记的名字照常抛 AttributeError。"""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module_name}", __name__), name)
    globals()[name] = value  # 缓存，后续访问不再走 __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))


if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查器看见真实符号
    from .consolidation import ConsolidationEngine
    from .memory_api import BiliBotMemoryAPI
    from .layered_runtime import LayeredRuntime
    from .runtime import ActionRequest, EventPriority, EventRuntime, EventState, InboundEvent
