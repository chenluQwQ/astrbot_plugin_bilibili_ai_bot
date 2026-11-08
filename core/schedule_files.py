"""五种日程的生成和 json：有每日计划照计划，没有就按固定时刻或随机排；各写各的日程 json。

五种写法几乎一样但各有怪癖（tests/test_schedule_snapshot.py 钉着），原样搬过来没合并。
主动看片的时间段（``_proactive_windows``）只存在这里和 ``schedule.json``。
今天的时刻和「起过没有」不归这里管，在 ``day_schedules``。
"""
from __future__ import annotations

import random
from datetime import datetime

from astrbot.api import logger
from .infra.host_view import HostView


class ScheduleFiles(HostView):
    HOST_API = frozenset({"states", "daily_plan", "json_store", "schedule_rules", "settings"})

    def __init__(self, host):
        super().__init__(host)
        self._proactive_windows = []

    def _generate_daily_schedule(self):
        if not self.schedule_rules._schedule_feature_enabled("proactive"):
            self._proactive_windows = []
            self._save_schedule_state([], set())
            return [], set()
        plan = self.daily_plan._autonomous_plan_for_today()
        if self.settings.get("ENABLE_AUTONOMOUS_DAILY_PLAN"):
            planned = self.daily_plan._plan_time_pairs("proactive_times")
            self._proactive_windows = list(plan.get("proactive_windows", [])) if plan else []
            self._save_schedule_state(planned, set())
            return planned, set()
        configured_round_limit = max(0, int(self.settings.get("PROACTIVE_TIMES_COUNT") or 0))
        round_cap = min(configured_round_limit, self.schedule_rules._autonomous_limit_max("proactive"))
        windows = self.schedule_rules._fixed_window_entries()[:round_cap]
        self._proactive_windows = [{key: item[key] for key in ("start_time", "end_time", "scheduled_time")} | {"trigger_policy": "once_in_window"} for item in windows]
        times = [self.schedule_rules._parse_time_value(item["scheduled_time"]) for item in windows]
        times = [item for item in times if item is not None]
        self._save_schedule_state(times, set())
        return times, set()

    def _load_or_generate_schedule(self):
        if not self.schedule_rules._schedule_feature_enabled("proactive"):
            self._proactive_windows = []
            self._save_schedule_state([], set())
            return [], set()
        schedule = self.states.schedule.read({})
        if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
            pairs = [self.schedule_rules._parse_time_value(value) for value in schedule.get("proactive_times", [])]
            self._proactive_windows = list(schedule.get("proactive_windows", []))
            return [value for value in pairs if value is not None], set(schedule.get("proactive_triggered", []))
        return self._generate_daily_schedule()

    def _save_schedule_state(self, times, triggered):
        schedule = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "proactive_times": self.schedule_rules._format_time_pairs(times),
            "proactive_windows": list(self._proactive_windows),
            "proactive_triggered": sorted(triggered),
        }
        self.states.schedule.write(schedule)

    def _generate_dynamic_schedule(self):
        if not self.schedule_rules._schedule_feature_enabled("dynamic"):
            self._save_dynamic_schedule_state([], set())
            return [], set()
        planned = self.daily_plan._plan_time_pairs("dynamic_times")
        if self.settings.get("ENABLE_AUTONOMOUS_DAILY_PLAN"):
            self._save_dynamic_schedule_state(planned, set())
            return planned, set()
        fixed = self.schedule_rules._fixed_time_pairs("FIXED_DYNAMIC_TIMES")
        if fixed:
            times = fixed
        else:
            raw_times = self.settings.get("DYNAMIC_TIMES_COUNT")
            if raw_times is None:
                raw_times = self.settings.get("DYNAMIC_DAILY_COUNT")
            try:
                n_times = max(0, int(1 if raw_times is None else raw_times))
            except (TypeError, ValueError):
                n_times = 1
            # 发布侧把 DYNAMIC_DAILY_COUNT=0 定义为“不额外限条数”；若只是回退读到了 0，
            # 这里按默认 1 次排期，避免“0=不限”被解读成“0=不自动发”。
            if n_times == 0 and self.settings.get("DYNAMIC_TIMES_COUNT") is None:
                n_times = 1
            daily_cap = self.schedule_rules._autonomous_limit_max("dynamic")
            if daily_cap > 0:  # 0 表示该项不设限，与动态发布侧一致
                n_times = min(n_times, daily_cap)
            # 与固定时段一致：睡眠时间不发；候选为空时保留旧行为，避免功能被睡眠配置完全关掉。
            hours = [hour for hour in range(10, 23) if self.schedule_rules._is_awake_minute(hour * 60 + 30)]
            if not hours:
                hours = list(range(10, 23))
            n_times = min(n_times, len(hours))
            picked = sorted(random.sample(hours, n_times)) if n_times else []
            times = [(hour, random.randint(0, 59)) for hour in picked]
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "dynamic_times": [f"{h}:{m:02d}" for h, m in times], "dynamic_triggered": []}
        self.states.dynamic_schedule.write(schedule)
        return times, set()

    def _load_or_generate_dynamic_schedule(self):
        if not self.schedule_rules._schedule_feature_enabled("dynamic"):
            self._save_dynamic_schedule_state([], set())
            return [], set()
        try:
            schedule = self.states.dynamic_schedule.read({})
            if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
                times = []
                for t in schedule.get("dynamic_times", []):
                    h, m = t.split(":")
                    times.append((int(h), int(m)))
                triggered = set(schedule.get("dynamic_triggered", []))
                return times, triggered
        except Exception:
            pass
        return self._generate_dynamic_schedule()

    def _save_dynamic_schedule_state(self, times, triggered):
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "dynamic_times": [f"{h}:{m:02d}" for h, m in times], "dynamic_triggered": list(triggered)}
        self.states.dynamic_schedule.write(schedule)

    def _generate_bangumi_schedule(self):
        if not self.schedule_rules._schedule_feature_enabled("bangumi"):
            self._save_bangumi_schedule_state([], set(), False)
            return [], set(), False
        planned = self.daily_plan._plan_time_pairs("bangumi_times")
        if self.settings.get("ENABLE_AUTONOMOUS_DAILY_PLAN"):
            self._save_bangumi_schedule_state(planned, set(), False)
            return planned, set(), False
        fixed = self.schedule_rules._fixed_time_pairs("FIXED_BANGUMI_TIMES")
        if fixed:
            times = fixed
        else:
            n_times = self.settings.get("BANGUMI_DAILY_LIMIT")
            available_hours = list(range(10, 23))
            n_times = min(n_times, len(available_hours))
            times = sorted(random.sample(available_hours, n_times))
            times = [(h, random.randint(0, 59)) for h in times]
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "bangumi_times": [f"{h}:{m:02d}" for h, m in times], "bangumi_triggered": [], "update_checked": False}
        self.states.bangumi_schedule.write(schedule)
        return times, set(), False

    def _load_or_generate_bangumi_schedule(self):
        if not self.schedule_rules._schedule_feature_enabled("bangumi"):
            self._save_bangumi_schedule_state([], set(), False)
            return [], set(), False
        try:
            schedule = self.states.bangumi_schedule.read({})
            if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
                times = []
                for t in schedule.get("bangumi_times", []):
                    h, m = t.split(":")
                    times.append((int(h), int(m)))
                triggered = set(schedule.get("bangumi_triggered", []))
                update_checked = schedule.get("update_checked", False)
                return times, triggered, update_checked
        except Exception:
            pass
        return self._generate_bangumi_schedule()

    def _save_bangumi_schedule_state(self, times, triggered, update_checked=False):
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "bangumi_times": [f"{h}:{m:02d}" for h, m in times], "bangumi_triggered": list(triggered), "update_checked": update_checked}
        self.states.bangumi_schedule.write(schedule)

    def _get_special_follow_config_fingerprint(self):
        """当前特关配置的指纹，用于检测配置变更。"""
        plan = self.daily_plan._autonomous_plan_for_today()
        if plan:
            return f"autonomous|{plan.get('config_fingerprint', '')}"
        fixed = self.settings.get("FIXED_SPECIAL_FOLLOW_TIMES")
        return f"fixed-plan|{','.join(str(t) for t in fixed)}"

    def _generate_special_follow_schedule(self):
        if not self.schedule_rules._schedule_feature_enabled("special_follow"):
            self._save_special_follow_schedule_state([], set())
            return [], set()
        planned = self.daily_plan._plan_time_pairs("special_follow_times")
        if self.settings.get("ENABLE_AUTONOMOUS_DAILY_PLAN"):
            self._save_special_follow_schedule_state(planned, set())
            return planned, set()
        # 固定计划模式统一从“当天计划生成方式”板块读取，不再使用能力抽屉中的旧触发方式/次数。
        times = self.schedule_rules._fixed_time_pairs("FIXED_SPECIAL_FOLLOW_TIMES")
        schedule = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "special_follow_times": [f"{h}:{m:02d}" for h, m in times],
            "special_follow_triggered": [],
            "config_fingerprint": self._get_special_follow_config_fingerprint(),
        }
        self.states.special_follow_schedule.write(schedule)
        logger.info(f"[BiliBot] ⭐ 特关计划已生成：{[f'{h}:{m:02d}' for h, m in times]}")
        return times, set()

    def _load_or_generate_special_follow_schedule(self):
        if not self.schedule_rules._schedule_feature_enabled("special_follow"):
            self._save_special_follow_schedule_state([], set())
            return [], set()
        try:
            schedule = self.states.special_follow_schedule.read({})
            if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
                # 配置变了就重新生成
                if schedule.get("config_fingerprint") != self._get_special_follow_config_fingerprint():
                    logger.info("[BiliBot] ⭐ 检测到特关配置变更，重新生成计划")
                    return self._generate_special_follow_schedule()
                times = []
                for t in schedule.get("special_follow_times", []):
                    h, m = t.split(":")
                    times.append((int(h), int(m)))
                triggered = set(schedule.get("special_follow_triggered", []))
                return times, triggered
        except Exception:
            pass
        return self._generate_special_follow_schedule()

    def _save_special_follow_schedule_state(self, times, triggered):
        schedule = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "special_follow_times": [f"{h}:{m:02d}" for h, m in times],
            "special_follow_triggered": list(triggered),
            "config_fingerprint": self._get_special_follow_config_fingerprint(),
        }
        self.states.special_follow_schedule.write(schedule)

    def _generate_dynamic_watch_schedule(self):
        if not self.schedule_rules._schedule_feature_enabled("dynamic_watch"):
            self._save_dynamic_watch_schedule_state([], set())
            return [], set()
        planned = self.daily_plan._plan_time_pairs("dynamic_watch_times")
        if self.settings.get("ENABLE_AUTONOMOUS_DAILY_PLAN"):
            self._save_dynamic_watch_schedule_state(planned, set())
            return planned, set()
        fixed = self.schedule_rules._fixed_time_pairs("FIXED_DYNAMIC_WATCH_TIMES")
        if fixed:
            times = fixed
        else:
            count = max(0, min(12, int(self.settings.get("DYNAMIC_WATCH_TIMES_COUNT"))))
            hours = sorted(random.sample(range(9, 23), min(count, 14)))
            times = [(hour, random.randint(0, 59)) for hour in hours]
        self._save_dynamic_watch_schedule_state(times, set())
        return times, set()

    def _load_or_generate_dynamic_watch_schedule(self):
        if not self.schedule_rules._schedule_feature_enabled("dynamic_watch"):
            self._save_dynamic_watch_schedule_state([], set())
            return [], set()
        schedule = self.states.dynamic_watch_schedule.read({})
        if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
            pairs = [self.schedule_rules._parse_time_value(value) for value in schedule.get("dynamic_watch_times", [])]
            return [value for value in pairs if value is not None], set(schedule.get("dynamic_watch_triggered", []))
        return self._generate_dynamic_watch_schedule()

    def _save_dynamic_watch_schedule_state(self, times, triggered):
        self.states.dynamic_watch_schedule.write({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "dynamic_watch_times": self.schedule_rules._format_time_pairs(times),
            "dynamic_watch_triggered": sorted(triggered),
        })
