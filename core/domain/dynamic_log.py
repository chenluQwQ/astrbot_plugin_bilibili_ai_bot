"""发过的动态：活动日志的 ``dynamic`` 那种只由这里写。

周报、日报是固定投递，也记在这里，但不占最短间隔和每日条数；评论顺带转发出的动态（``comment_sync``）也一样。
"""
from __future__ import annotations

from datetime import datetime

from ..storage.activity_log import ActivityLog


def _not_counted(entry) -> bool:
    return bool(entry.get("weekly") or entry.get("daily_summary") or entry.get("comment_sync"))


class DynamicLog:
    def __init__(self, log: ActivityLog):
        self._log = log

    def entries(self) -> list:
        return self._log.recent()

    def recent_texts(self, count: int) -> list[str]:
        return [item.get("text", "") for item in self._log.recent()[-count:] if item.get("text")]

    def posts_today(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        return sum(1 for item in self._log.on_day(today) if not _not_counted(item))

    def has_video(self, bvid: str) -> bool:
        return any(item.get("bvid") == bvid for item in self.entries())

    def has_key(self, key: str) -> bool:
        """池子里的想法（BV 号或 ``ep:`` 番剧集）发过没有；旧日志只有 ``bvid``。"""
        return bool(key) and any(key in (item.get("bvid"), item.get("idea_key")) for item in self.entries())

    def interval_passed(self, days: int) -> bool:
        today = datetime.now().date()
        for item in reversed(self.entries()):
            if _not_counted(item):
                continue
            try:
                posted = datetime.strptime(str(item.get("time", ""))[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            # 未来日期（改时区或手改日志）不能让动态永久停摆。
            if 0 <= (today - posted).days < days:
                return False
        return True

    def record(self, entry: dict) -> None:
        self._log.append(entry)
