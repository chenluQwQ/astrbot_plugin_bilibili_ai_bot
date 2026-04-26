"""定时任务调度：主动视频和动态发布的时间管理。"""
import random
from datetime import datetime
from astrbot.api import logger
from .config import DYNAMIC_SCHEDULE_FILE, SCHEDULE_FILE


class ScheduleMixin:
    """日程管理。"""

    # ── 主动视频调度 ──
    def _generate_daily_schedule(self):
        n_times = self.config.get("PROACTIVE_TIMES_COUNT", 2)
        times = sorted(random.sample(range(10, 23), min(n_times, 12)))
        times = [(h, random.randint(0, 59)) for h in times]
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "proactive_times": [f"{h}:{m:02d}" for h, m in times], "proactive_triggered": []}
        self._save_json(SCHEDULE_FILE, schedule)
        return times, set()

    def _load_or_generate_schedule(self):
        try:
            schedule = self._load_json(SCHEDULE_FILE, {})
            if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
                times = []
                for t in schedule.get("proactive_times", []):
                    h, m = t.split(":")
                    times.append((int(h), int(m)))
                triggered = set(schedule.get("proactive_triggered", []))
                return times, triggered
        except Exception:
            pass
        return self._generate_daily_schedule()

    def _save_schedule_state(self, times, triggered):
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "proactive_times": [f"{h}:{m:02d}" for h, m in times], "proactive_triggered": list(triggered)}
        self._save_json(SCHEDULE_FILE, schedule)

    # ── 动态调度 ──
    def _generate_dynamic_schedule(self):
        n_times = self.config.get("DYNAMIC_TIMES_COUNT", 1)
        times = sorted(random.sample(range(10, 23), min(n_times, 12)))
        times = [(h, random.randint(0, 59)) for h in times]
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "dynamic_times": [f"{h}:{m:02d}" for h, m in times], "dynamic_triggered": []}
        self._save_json(DYNAMIC_SCHEDULE_FILE, schedule)
        return times, set()

    def _load_or_generate_dynamic_schedule(self):
        try:
            schedule = self._load_json(DYNAMIC_SCHEDULE_FILE, {})
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
        self._save_json(DYNAMIC_SCHEDULE_FILE, schedule)

    # ── 通用工具 ──
    @staticmethod
    def _format_time_pairs(times):
        return [f"{h}:{m:02d}" for h, m in times]

    def _ensure_today_schedules(self):
        today = datetime.now().strftime("%Y-%m-%d")
        sched = self._load_json(SCHEDULE_FILE, {})
        if sched.get("date") != today or not self._proactive_times:
            self._proactive_times, self._proactive_triggered = self._load_or_generate_schedule()
        dsched = self._load_json(DYNAMIC_SCHEDULE_FILE, {})
        if dsched.get("date") != today or not self._dynamic_times:
            self._dynamic_times, self._dynamic_triggered = self._load_or_generate_dynamic_schedule()

    def _get_schedule_snapshot(self):
        self._ensure_today_schedules()
        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "proactive_times": self._format_time_pairs(self._proactive_times),
            "proactive_triggered": sorted(self._proactive_triggered),
            "dynamic_times": self._format_time_pairs(self._dynamic_times),
            "dynamic_triggered": sorted(self._dynamic_triggered),
        }

    def _mark_overdue_schedule_as_triggered_on_startup(self):
        now_dt = datetime.now()
        changed = False
        self._ensure_today_schedules()
        proactive_overdue = {f"{h}:{m:02d}" for h, m in self._proactive_times if (now_dt.hour > h or (now_dt.hour == h and now_dt.minute > m))}
        overdue_to_add = proactive_overdue - self._proactive_triggered
        if overdue_to_add:
            self._proactive_triggered.update(overdue_to_add)
            self._save_schedule_state(self._proactive_times, self._proactive_triggered)
            changed = True
            logger.info(f"[BiliBot] 启动时跳过已过期的主动视频计划：{sorted(overdue_to_add)}")
        dynamic_overdue = {f"{h}:{m:02d}" for h, m in self._dynamic_times if (now_dt.hour > h or (now_dt.hour == h and now_dt.minute > m))}
        overdue_dynamic_to_add = dynamic_overdue - self._dynamic_triggered
        if overdue_dynamic_to_add:
            self._dynamic_triggered.update(overdue_dynamic_to_add)
            self._save_dynamic_schedule_state(self._dynamic_times, self._dynamic_triggered)
            changed = True
            logger.info(f"[BiliBot] 启动时跳过已过期的动态计划：{sorted(overdue_dynamic_to_add)}")
        if not changed:
            logger.debug(f"[BiliBot] 启动时无需跳过过期计划（{now_dt.strftime('%Y-%m-%d')}）")
