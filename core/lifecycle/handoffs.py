"""交接：一个分支做完事，想让另一个分支接着干，交给主干转交。

分支不认识别的分支，只说「发生了什么 / 想要什么」（这里的一个数据类），
主干按注册把它交给接手的那个分支。插件里的注册表是 ``core/activities.py`` 的 ``HANDOFF_ROUTES``。

叫「交接」不叫「事件」：插件里已经有 ``event_runtime``（外部事件的去重、优先级、行为额度），
两个都叫事件很容易看混。

规矩：
- 每种交接只有一个接手的；重复注册直接报错
- ``send`` 等接手的做完才返回（和原来直接 await 一样），接手的抛的异常照常往上抛，
  要不要吞由接手的自己决定
- 没人接手的交接不算错：记一条 debug，返回 False
"""

from __future__ import annotations

from astrbot.api import logger
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class WantsToPostDynamic:
    """看完一轮视频，有想发动态的，已经放进动态池子。``fresh`` 是这一轮放进去的池子编号。"""

    fresh: tuple


class Handoffs:
    def __init__(self):
        self._routes: dict[type, Callable[[Any], Awaitable[Any]]] = {}

    def route(self, kind: type, handler: Callable[[Any], Awaitable[Any]]) -> None:
        if kind in self._routes:
            raise ValueError(f"交接 {kind.__name__} 已经有接手的了")
        self._routes[kind] = handler

    async def send(self, item: Any) -> bool:
        handler = self._routes.get(type(item))
        if handler is None:
            logger.debug(f"[BiliBot] 交接 {type(item).__name__} 没有接手的，丢掉")
            return False
        await handler(item)
        return True
