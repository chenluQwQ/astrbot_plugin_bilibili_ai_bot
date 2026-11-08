"""回复日志：活动日志的 ``reply`` 那种只由这里写，评论和私信都记在这一份里（``channel`` 区分）。

不受记忆压缩影响；WebUI、日报、记忆接口只读。
"""
from __future__ import annotations

from datetime import datetime

from ..storage.activity_log import ActivityLog


def _same_channel(item: dict, channel: str) -> bool:
    return (item.get("channel") == "private") if channel == "private" else item.get("channel") not in {"private", "live"}


class ReplyLog:
    def __init__(self, log: ActivityLog):
        self._log = log

    def today_count(self, channel: str = "comment") -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        return sum(
            1 for item in self._log.on_day(today) if _same_channel(item, channel)
        )

    def recent_contents(self, channel: str, count: int) -> list[str]:
        """最近 ``count`` 条里同一通道的原话，新的在前；判断「一字不差的重复」用。

        这里私信以外都算评论（含旧的直播弹幕），和 ``today_count`` 不一样，是原来就这样。
        """
        recent = self._log.recent()[-count:]
        same = (lambda item: item.get("channel") == "private") if channel == "private" else (lambda item: item.get("channel") != "private")
        return [item.get("content", "") for item in reversed(recent) if isinstance(item, dict) and same(item)]

    def record(self, entry: dict) -> None:
        self._log.append(entry)
