"""五种定时活动今天的日程：几点做、哪几格起过、日程 json 镜像，唯一的读写入口。

「几点做、哪几格起过」在 ``core/lifecycle`` 的 activity_runs（sqlite，插件启动时打开）；
日程 json 怎么生成、怎么写在 ``schedule_files``。追番「今天查过更新没有」只记在这里和追番的 json 里。
WebUI（经 ``BotApp``）、``Runner``、``Activities``、每日计划都调它。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from astrbot.api import logger
from .infra.host_view import HostView
from .lifecycle.activities import ActivityRuns, ActivityState


@dataclass(frozen=True)
class DaySchedule:
    """一种活动的日程放在哪。``save`` / ``restore`` 是 ``ScheduleFiles`` 上的方法名。

    「今天几点做、哪几格起过」存在 ``core/lifecycle`` 的 activity_runs（kind 就是这张表的键）；
    日程 json 只存日期、配置指纹这些，外加一份时刻的镜像给旧版本用。只有 ``DaySchedules`` 碰它们，
    别处都调它。
    """
    attr: str                  # 任务属性前缀：插件上的 <attr>_task
    state: str                 # bot.states 上那份日程 json 的名字
    save: str                  # 写 json：(times, triggered)；追番多一个 update_checked
    restore: str               # 今天的从文件读，不是今天的就重新生成
    startup_label: str         # 启动时跳过过期时刻打的日志里怎么称呼


DAY_SCHEDULES = {
    "proactive": DaySchedule(
        "_proactive", "schedule", "_save_schedule_state", "_load_or_generate_schedule", "主动视频计划",
    ),
    "dynamic": DaySchedule(
        "_dynamic", "dynamic_schedule", "_save_dynamic_schedule_state",
        "_load_or_generate_dynamic_schedule", "动态计划",
    ),
    "bangumi": DaySchedule(
        "_bangumi", "bangumi_schedule", "_save_bangumi_schedule_state",
        "_load_or_generate_bangumi_schedule", "番剧计划",
    ),
    "special_follow": DaySchedule(
        "_special_follow", "special_follow_schedule", "_save_special_follow_schedule_state",
        "_load_or_generate_special_follow_schedule", "特关计划",
    ),
    "dynamic_watch": DaySchedule(
        "_dynamic_watch", "dynamic_watch_schedule", "_save_dynamic_watch_schedule_state",
        "_load_or_generate_dynamic_watch_schedule", "关注动态巡视计划",
    ),
}


class DaySchedules(HostView):
    HOST_API = frozenset({"json_store", "schedule_files", "schedule_rules", "states"})
    KINDS = tuple(DAY_SCHEDULES)

    def __init__(self, host):
        super().__init__(host)
        # 没打开过（测试、打开失败）时 _runs() 现建一份只在内存里的
        self._activity_runs = None
        self._bangumi_update_checked = False

    def _runs(self):
        """今天各活动时刻的状态表。插件初始化时打开 sqlite 里的那份；没打开过（测试、打开失败）
        就用一份只在内存里的，重启后从日程 json 的镜像导回来。"""
        runs = self._activity_runs
        if runs is None:
            runs = ActivityRuns(sqlite3.connect(":memory:", check_same_thread=False))
            self._activity_runs = runs
        return runs

    def _open_activity_runs(self, path):
        try:
            runs = ActivityRuns.open(path)
            interrupted = runs.interrupt_running()
        except Exception as exc:
            logger.error(f"[BiliBot] 活动状态表打开失败，今天的日程只记在内存里: {exc}")
            return
        self._activity_runs = runs
        if interrupted:
            logger.info(f"[BiliBot] 上次停用时还有 {interrupted} 个定时活动在跑，已记为中断（不重跑）")

    def _close_activity_runs(self):
        runs, self._activity_runs = self._activity_runs, None
        if runs is not None:
            runs.close()

    @staticmethod
    def _today(day=None):
        return day or datetime.now().strftime("%Y-%m-%d")

    def _day_schedule(self, kind, day=None):
        """这一天（默认今天）的 (时刻, 已起过的时刻)。只读；改要调下面的方法。"""
        times, triggered = [], set()
        for run in self._runs().runs(self._today(day), kind):
            parsed = self.schedule_rules._parse_time_value(run.slot)
            if parsed is None:
                continue
            times.append(parsed)
            if run.state is not ActivityState.PLANNED:
                triggered.add(run.slot)
        return sorted(times), triggered

    def _plan_day_schedule(self, kind, times, *, day=None, save=True, update_checked=None):
        """排今天的时刻。今天真起过的保留（不会因为重排再跑一次）。``update_checked`` 只对追番有意义。"""
        self._runs().replan(self._today(day), kind, self.schedule_rules._format_time_pairs(times))
        if kind == "bangumi" and update_checked is not None:
            self._bangumi_update_checked = update_checked
        if save:
            self._save_day_schedule(kind, day=day)

    def _override_day_schedule(self, kind, times, done, *, update_checked=None):
        """WebUI 改日程：照页面给的时刻和「做过没有」整体换掉。"""
        self._runs().override(self._today(), kind, self.schedule_rules._format_time_pairs(times), set(done), "网页上标为已做过")
        if kind == "bangumi" and update_checked is not None:
            self._bangumi_update_checked = update_checked
        self._save_day_schedule(kind)

    def _save_day_schedule(self, kind, *, day=None):
        """把今天的状态镜像进日程 json。json 里的时刻和已触发只给退回旧版本用，不再读回来
        （除了第一次升级时导入）；日期、配置指纹、主动看片时间段、追番查过更新还是只存在 json 里。"""
        times, triggered = self._day_schedule(kind, day)
        save = getattr(self.schedule_files, DAY_SCHEDULES[kind].save)
        if kind == "bangumi":
            save(times, triggered, self._bangumi_update_checked)
        else:
            save(times, triggered)

    def _restore_day_schedule(self, kind, day=None):
        """日程 json 是今天的就照它排，不是今天的就重新生成（生成时会存盘）。
        库里还没有今天这种活动时，json 里记的「已触发」导入一次（升级当天重启不重跑）。"""
        today = self._today(day)
        first_time = not self._runs().is_planned(today, kind)
        result = getattr(self.schedule_files, DAY_SCHEDULES[kind].restore)()
        if kind == "bangumi":
            times, triggered, checked = result
            self._plan_day_schedule(kind, times, day=today, save=False, update_checked=checked)
        else:
            times, triggered = result
            self._plan_day_schedule(kind, times, day=today, save=False)
        if first_time:
            for slot in set(triggered) & set(self.schedule_rules._format_time_pairs(times)):
                self._runs().mark_skipped(today, kind, slot, "从日程文件导入的已触发")

    def _clear_day_schedules(self):
        """五种日程今天还没做的时刻全部撤掉（真起过的留着），等下次换天或重新生成。不碰文件。"""
        today = self._today()
        for kind in DAY_SCHEDULES:
            self._runs().replan(today, kind, [])
        self._bangumi_update_checked = False

    def _skip_slot(self, kind, slot, reason, day=None):
        self._runs().mark_skipped(self._today(day), kind, slot, reason)

    def _ensure_today_schedules(self):
        today = datetime.now().strftime("%Y-%m-%d")
        for kind, day in DAY_SCHEDULES.items():
            if not self.schedule_rules._schedule_feature_enabled(kind):
                self._plan_day_schedule(kind, [], save=False, update_checked=False)
                continue
            sched = getattr(self.states, day.state).read({})
            if sched.get("date") != today or not self._day_schedule(kind)[0]:
                self._restore_day_schedule(kind)

    def _get_schedule_snapshot(self):
        self._ensure_today_schedules()
        snapshot = {"date": datetime.now().strftime("%Y-%m-%d")}
        for kind in DAY_SCHEDULES:
            enabled = self.schedule_rules._schedule_feature_enabled(kind)
            times, triggered = self._day_schedule(kind)
            snapshot[f"{kind}_times"] = self.schedule_rules._format_time_pairs(times) if enabled else []
            snapshot[f"{kind}_triggered"] = sorted(triggered) if enabled else []
            if kind == "proactive":
                snapshot["proactive_windows"] = list(self.schedule_files._proactive_windows) if enabled else []
        return snapshot

    def _mark_overdue_schedule_as_triggered_on_startup(self):
        now_dt = datetime.now()
        changed = False
        self._ensure_today_schedules()
        for kind, day in DAY_SCHEDULES.items():
            times, triggered = self._day_schedule(kind)
            overdue = {f"{h}:{m:02d}" for h, m in times if (now_dt.hour > h or (now_dt.hour == h and now_dt.minute > m))}
            to_add = overdue - triggered
            if to_add:
                for slot in to_add:
                    self._skip_slot(kind, slot, "启动时已经过了时间")
                self._save_day_schedule(kind)
                changed = True
                logger.info(f"[BiliBot] 启动时跳过已过期的{day.startup_label}：{sorted(to_add)}")
        if not changed:
            logger.debug(f"[BiliBot] 启动时无需跳过过期计划（{now_dt.strftime('%Y-%m-%d')}）")
