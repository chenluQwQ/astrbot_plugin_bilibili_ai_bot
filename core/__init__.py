"""BiliBot 核心包。

这里采用 PEP 562 惰性导入：旧实现在包初始化时一次性 import 全部 18 个 mixin，
任何人只想用 ``core.security.scopes`` 也会连带拉起 aiohttp、astrbot 和整条
B 站 API 链路。惰性化之后，四个新层（adapter/security/storage/persona）可以
独立导入与独立测试，插件启动路径的行为保持不变。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

#: 属性名 → 所在子模块。新增 mixin 时在这里登记即可。
_EXPORTS: dict[str, str] = {
    "UtilsMixin": "utils",
    "LLMMixin": "llm",
    "VisionMixin": "vision",
    "MemoryMixin": "memory",
    "AffectionMixin": "affection",
    "PersonalityMixin": "personality",
    "BilibiliAPIMixin": "bilibili",
    "BangumiMixin": "bangumi",
    "WebSearchMixin": "search",
    "VideoMixin": "video",
    "ReplyMixin": "reply",
    "ProactiveMixin": "proactive",
    "DynamicMixin": "dynamic",
    "ScheduleMixin": "schedule_mixin",
    "WeeklySummaryMixin": "weekly",
    "ShareMixin": "share",
    "PrivateMessageMixin": "private_messages",
    "LiveDanmakuMixin": "live_danmaku",
    "ConsolidationEngine": "consolidation",
    "BiliBotMemoryAPI": "memory_api",
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
    from .affection import AffectionMixin
    from .bangumi import BangumiMixin
    from .bilibili import BilibiliAPIMixin
    from .consolidation import ConsolidationEngine
    from .dynamic import DynamicMixin
    from .live_danmaku import LiveDanmakuMixin
    from .llm import LLMMixin
    from .memory import MemoryMixin
    from .memory_api import BiliBotMemoryAPI
    from .personality import PersonalityMixin
    from .private_messages import PrivateMessageMixin
    from .proactive import ProactiveMixin
    from .reply import ReplyMixin
    from .runtime import ActionRequest, EventPriority, EventRuntime, EventState, InboundEvent
    from .schedule_mixin import ScheduleMixin
    from .search import WebSearchMixin
    from .share import ShareMixin
    from .utils import UtilsMixin
    from .video import VideoMixin
    from .vision import VisionMixin
    from .weekly import WeeklySummaryMixin
