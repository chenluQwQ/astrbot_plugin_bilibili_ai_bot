"""日程的时间和开关规则：几点算醒着、时间段怎么解析、哪种活动开着、各项自主上限。

都是纯规则，只读配置（每次现读）。WebUI、每日计划、五种日程的生成、动态和主动看片都在用。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Callable

from .day import SleepWindow


class ScheduleRules:
    def __init__(self, settings: Callable[[], object]):
        self._settings = settings

    @property
    def settings(self):
        return self._settings()

    _AUTONOMOUS_LIMITS = {
        "reply": "AUTONOMOUS_REPLY_DAILY_MAX",
        "private": "AUTONOMOUS_PRIVATE_DAILY_MAX",
        "dynamic": "AUTONOMOUS_DYNAMIC_DAILY_MAX",
        "proactive": "AUTONOMOUS_PROACTIVE_DAILY_MAX",
    }

    _SCHEDULE_TRIGGER_GRACE_MINUTES = 5

    def _autonomous_limit_range(self, kind):
        """Return ``(0, hard maximum)`` for an autonomous safety budget.

        The old ``*_DAILY_LIMIT`` keys are resolved by ``core.settings.LEGACY_KEYS``.
        Old lower-bound keys are deliberately ignored: no behaviour is performed
        to fill a quota.
        """
        return 0, self.settings.int(self._AUTONOMOUS_LIMITS[kind], minimum=0)

    def _autonomous_limit_max(self, kind):
        return self._autonomous_limit_range(kind)[1]

    @staticmethod
    def _parse_time_value(value):
        try:
            hour, minute = str(value).strip().split(":", 1)
            hour, minute = int(hour), int(minute)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except (TypeError, ValueError):
            pass
        return None

    @classmethod
    def _parse_window_value(cls, value):
        """Parse ``HH:MM-HH:MM`` or a schedule-window mapping."""
        if isinstance(value, dict):
            start_raw = value.get("start_time") or value.get("start")
            end_raw = value.get("end_time") or value.get("end")
            scheduled_raw = value.get("scheduled_time") or value.get("time")
        else:
            parts = re.split(r"\s*(?:-|—|–|~|至)\s*", str(value or "").strip(), maxsplit=1)
            if len(parts) != 2:
                return None
            start_raw, end_raw = parts
            scheduled_raw = None
        start = cls._parse_time_value(start_raw)
        end = cls._parse_time_value(end_raw)
        if start is None or end is None:
            return None
        start_minute = start[0] * 60 + start[1]
        end_minute = end[0] * 60 + end[1]
        duration = (end_minute - start_minute) % 1440
        if duration < 15:
            return None
        scheduled = cls._parse_time_value(scheduled_raw) if scheduled_raw else None
        return {
            "start_time": f"{start[0]:02d}:{start[1]:02d}",
            "end_time": f"{end[0]:02d}:{end[1]:02d}",
            "start_minute": start_minute,
            "end_minute": end_minute,
            "duration_minutes": duration,
            "scheduled_time": f"{scheduled[0]:02d}:{scheduled[1]:02d}" if scheduled else "",
        }

    @staticmethod
    def _minute_text(minute):
        minute %= 1440
        return f"{minute // 60:02d}:{minute % 60:02d}"

    @staticmethod
    def _format_time_pairs(times):
        return [f"{h}:{m:02d}" for h, m in times]

    def _window_trigger_minute(self, start_minute, duration_minutes, seed):
        """Pick a stable 15-minute trigger inside the middle half of a window."""
        safe_duration = max(15, int(duration_minutes))
        low = max(0, safe_duration // 4)
        high = max(low, safe_duration - low - 1)
        digest = int(hashlib.sha256(str(seed).encode()).hexdigest()[:8], 16)
        offset = low + digest % max(1, high - low + 1)
        return ((start_minute + offset) // 15 * 15) % 1440

    def _fixed_window_entries(self):
        entries = []
        for index, raw in enumerate(self.settings.get("FIXED_PROACTIVE_WINDOWS") or []):
            parsed = self._parse_window_value(raw)
            if parsed and self._is_awake_minute(parsed["start_minute"]):
                if not parsed["scheduled_time"]:
                    trigger = self._window_trigger_minute(parsed["start_minute"], parsed["duration_minutes"], f"fixed|{index}|{raw}")
                    parsed["scheduled_time"] = self._minute_text(trigger)
                entries.append(parsed)
        if entries:
            return entries
        # Backward compatibility: turn old exact times into centered windows.
        duration = max(30, int(self.settings.get("AUTONOMOUS_PROACTIVE_WINDOW_MINUTES")))
        for index, pair in enumerate(self._fixed_time_pairs("FIXED_PROACTIVE_TIMES")):
            center = pair[0] * 60 + pair[1]
            start = max(0, center - duration // 2)
            end = min(1439, start + duration)
            entries.append({
                "start_time": self._minute_text(start), "end_time": self._minute_text(end),
                "start_minute": start, "end_minute": end, "duration_minutes": end - start,
                "scheduled_time": self._minute_text(center),
            })
        return entries

    def _fixed_time_pairs(self, key):
        pairs = []
        for value in self.settings.get(key) or []:
            parsed = self._parse_time_value(value)
            if parsed is not None and self._is_awake_minute(parsed[0] * 60 + parsed[1]):
                pairs.append(parsed)
        return sorted(set(pairs))

    def _in_sleep_hours(self):
        return SleepWindow(int(self.settings.get("SLEEP_START")), int(self.settings.get("SLEEP_END"))).is_asleep(datetime.now())

    def _is_awake_minute(self, minute):
        start = int(self.settings.get("SLEEP_START")) * 60
        end = int(self.settings.get("SLEEP_END")) * 60
        if start == end:
            return True
        sleeping = start <= minute < end if start < end else minute >= start or minute < end
        return not sleeping

    def _schedule_feature_enabled(self, kind):
        """Return whether a schedule type can really execute in the main loop."""
        if kind == "proactive":
            return bool(self.settings.get("ENABLE_PROACTIVE"))
        if kind == "dynamic":
            return bool(self.settings.get("ENABLE_DYNAMIC"))
        if kind == "bangumi":
            return bool(self.settings.get("ENABLE_BANGUMI") and self.settings.get("BANGUMI_PROACTIVE"))
        if kind == "special_follow":
            return bool(self.settings.get("SPECIAL_FOLLOW_ENABLED"))
        if kind == "dynamic_watch":
            return bool(self.settings.get("ENABLE_DYNAMIC_WATCH"))
        return False

    def _activity_awake_window(self):
        """Use activity as a soft active-time window without bypassing sleep or hard gaps."""
        activity = max(0, min(100, int(self.settings.get("AUTONOMOUS_ACTIVITY_LEVEL"))))
        if activity < 25:
            return 11 * 60, 20 * 60 + 15
        if activity < 50:
            return 10 * 60, 21 * 60 + 45
        if activity < 75:
            return 9 * 60, 22 * 60 + 30
        return 8 * 60, 23 * 60 + 15
