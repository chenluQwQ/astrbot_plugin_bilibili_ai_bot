"""一天：现在是睡着还是醒着，今天是哪天。

和旧代码算法一致（``main.py`` 主循环和 ``ScheduleRules._is_awake_minute``）：
作息按整点配置，可以跨午夜（23 点睡、7 点醒）；开始等于结束表示不睡。
「今天」是日历日期——跨午夜的作息里，凌晨一点已经算第二天。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SleepWindow:
    start_hour: int
    end_hour: int

    def is_asleep_at_minute(self, minute_of_day: int) -> bool:
        start, end = self.start_hour * 60, self.end_hour * 60
        if start == end:
            return False
        if start < end:
            return start <= minute_of_day < end
        return minute_of_day >= start or minute_of_day < end

    def is_asleep(self, now: datetime) -> bool:
        return self.is_asleep_at_minute(now.hour * 60 + now.minute)


def today(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")
