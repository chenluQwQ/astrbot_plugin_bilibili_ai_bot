"""把插件接到主干上：五种定时活动、例行检查、分支交接注册到 ``core/lifecycle`` 的调度器和交接上。

主干（``Scheduler``、``Handoffs``）不认识插件；这里按名字在插件上找入口（``proactive_browse._run_proactive``
这种带点的名字用 ``attrgetter`` 取），到点时写触发日志。日程状态在 ``day_schedules``。
手动触发（命令、工具、聊天）也走 ``_run_activity_now``。各活动正在跑的任务记在插件的 ``_xxx_task`` 上，
停用时 ``Runner`` 靠它取消。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from operator import attrgetter

from astrbot.api import logger
from .day_schedules import DAY_SCHEDULES
from .infra.host_view import HostView
from .lifecycle.day import SleepWindow
from .lifecycle.handoffs import Handoffs, WantsToPostDynamic
from .lifecycle.scheduler import ASLEEP, AWAKE, Routine, Scheduler


@dataclass(frozen=True)
class ScheduledActivity:
    """一种按日程触发的活动怎么触发。``generate`` 是 ``ScheduleFiles`` 上的方法名，入口是插件上的
    （带点的是插件上某个对象的方法）。日程状态见 ``core/day_schedules.py`` 的 ``DAY_SCHEDULES``。"""
    kind: str
    generate: str              # 换天时重新生成：() -> (times, triggered[, update_checked])
    restore_in_loop: bool      # 同一天日程丢了主循环要不要从文件恢复（关注动态巡视原来就不恢复）
    entry: str                 # 到点后做的事
    log_type: str | None       # 写进触发日志的 type；None = 不写（发动态原来就不写）
    new_day_log: str           # 换天时打的日志前缀
    trigger_log: str           # 触发时打的日志前缀
    first_of_day: tuple | None = None  # 每天第一次到点改做别的：(入口, 日志 type, 日志前缀)


# 顺序就是主干每轮检查的顺序（和旧主循环一样）。
SCHEDULED_ACTIVITIES = {
    "proactive": ScheduledActivity(
        kind="proactive", generate="_generate_daily_schedule", restore_in_loop=True,
        entry="proactive_browse._run_proactive",
        log_type="proactive_video", new_day_log="📅 新的一天！主动视频时间", trigger_log="🎯 触发主动视频",
    ),
    "dynamic": ScheduledActivity(
        kind="dynamic", generate="_generate_dynamic_schedule", restore_in_loop=True,
        entry="dynamic_post.run",
        log_type=None, new_day_log="📅 动态时间", trigger_log="📢 触发动态发布",
    ),
    "bangumi": ScheduledActivity(
        kind="bangumi", generate="_generate_bangumi_schedule", restore_in_loop=True,
        entry="bangumi.run",
        log_type="bangumi_watch", new_day_log="📅 番剧时间", trigger_log="🎬 触发主动看番",
        first_of_day=("bangumi.check_updates", "bangumi_update_check", "📺 触发每日追番更新检查"),
    ),
    "dynamic_watch": ScheduledActivity(
        kind="dynamic_watch", generate="_generate_dynamic_watch_schedule", restore_in_loop=False,
        entry="dynamic_watch.run",
        log_type="dynamic_watch", new_day_log="📅 关注动态巡视时间", trigger_log="📰 触发关注动态巡视",
    ),
    "special_follow": ScheduledActivity(
        kind="special_follow", generate="_generate_special_follow_schedule", restore_in_loop=True,
        entry="special_follow.run",
        log_type="special_follow", new_day_log="📅 特关时间", trigger_log="⭐ 触发特别关注巡视",
    ),
}


# 主干每轮都看一眼的事：(名字, 方法名（插件上某个对象的）, 醒着/睡着/都跑, 开关配置项, 在定时活动之后)。
# 顺序和旧主循环一样：醒着时 心情 → 人格 → 每日计划 → Cookie → 定时活动 → 私信 → 评论；
# 睡着时 人格 → 每日计划 → 夜里的清算和总结。
ROUTINES = (
    ("mood", "mood.settle", AWAKE, None, False),
    ("personality", "personality_evolution.maybe_evolve", None, None, False),
    ("daily_plan", "daily_plan._ensure_autonomous_daily_plan", None, None, False),
    ("night", "runner.night_routine", ASLEEP, None, False),
    ("memo_review", "memo_review.maybe_review", ASLEEP, "ENABLE_MEMO", False),
    ("favorite_profile", "favorite_digest.maybe_run", ASLEEP, "ENABLE_FAVORITE_PROFILE", False),
    ("cookie", "runner.maybe_check_cookie", AWAKE, None, False),
    ("yt_dlp_update", "runner.maybe_update_yt_dlp", AWAKE, "ENABLE_YTDLP_AUTO_UPDATE", False),
    ("private_messages", "private_poll._poll_private_messages", AWAKE, "ENABLE_PRIVATE_MESSAGES", True),
    ("comments", "comment_reply._poll_unified", AWAKE, "ENABLE_REPLY", True),
)

# 分支之间的交接（``core/lifecycle/handoffs.py``）：(交接的东西, 接手的方法；带点的是插件上某个对象的方法)。
HANDOFF_ROUTES = (
    (WantsToPostDynamic, "dynamic_post.take_video_handoff"),
)


class _ScheduledBranch:
    """一种定时活动在主干（``core/lifecycle/scheduler.py``）上的样子。

    日程归 ``day_schedules`` 管；任务记在插件的 ``_xxx_task`` 上，停用插件时靠它取消。
    """
    phase = AWAKE

    def __init__(self, activities, kind):
        self._activities = activities
        self._host = activities._host
        self.kind = kind
        self._task_attr = f"{DAY_SCHEDULES[kind].attr}_task"

    def enabled(self):
        return self._activities.schedule_rules._schedule_feature_enabled(self.kind)

    def prepare(self, day, now):
        self._activities._prepare_day_schedule(self.kind, day)

    def busy(self):
        task = getattr(self._host, self._task_attr, None)
        return task is not None and not task.done()

    async def run(self, day, slot):
        setattr(self._host, self._task_attr, asyncio.current_task())
        await self._activities._run_scheduled_slot(self.kind, day, slot)

    async def run_now(self, **kwargs):
        return await attrgetter(SCHEDULED_ACTIVITIES[self.kind].entry)(self._host)(**kwargs)


class Activities(HostView):
    HOST_API = frozenset({"activity_logs", "daily_plan", "day_schedules", "json_store", "schedule_files", "schedule_rules", "settings", "states"})

    def __init__(self, host):
        super().__init__(host)
        self._activity_scheduler = None
        self._branch_handoffs = None

    def _scheduler(self):
        """主干调度器：五种定时活动和 ``ROUTINES`` 都注册在上面，主循环每轮调一次 ``tick``。"""
        scheduler = self._activity_scheduler
        if scheduler is None:
            scheduler = Scheduler(
                self.day_schedules._runs,
                lambda: SleepWindow(int(self.settings.get("SLEEP_START")), int(self.settings.get("SLEEP_END"))),
                grace_minutes=self.schedule_rules._SCHEDULE_TRIGGER_GRACE_MINUTES,
            )
            for kind in SCHEDULED_ACTIVITIES:
                scheduler.register(_ScheduledBranch(self, kind))
            for name, method, phase, switch, after_slots in ROUTINES:
                scheduler.add_routine(Routine(
                    name, attrgetter(method)(self._host), phase,
                    enabled=(lambda key=switch: bool(self.settings.get(key))) if switch else (lambda: True),
                    after_slots=after_slots,
                ))
            self._activity_scheduler = scheduler
        return scheduler

    def _activity_busy(self, kind):
        return self._scheduler().busy(kind)

    def _run_activity_now(self, kind, **kwargs):
        """手动叫一次（命令、工具、聊天）：记进 ``activity_runs``，但不占日程。正忙返回 None。"""
        task = self._scheduler().run_now(kind, **kwargs)
        if task is not None:
            setattr(self._host, f"{DAY_SCHEDULES[kind].attr}_task", task)
        return task

    def _handoffs(self):
        """主干的交接：分支递过来的东西按 ``HANDOFF_ROUTES`` 交给接手的分支。"""
        handoffs = self._branch_handoffs
        if handoffs is None:
            handoffs = Handoffs()
            for kind, method in HANDOFF_ROUTES:
                handoffs.route(kind, attrgetter(method)(self._host))
            self._branch_handoffs = handoffs
        return handoffs

    def _prepare_day_schedule(self, kind, day):
        """每轮到点检查前：换天了就重新排，同一天日程丢了就从文件恢复（关注动态巡视不恢复）。"""
        activity = SCHEDULED_ACTIVITIES[kind]
        sched = getattr(self.states, DAY_SCHEDULES[kind].state).read({})
        if sched.get("date") != day:
            result = getattr(self.schedule_files, activity.generate)()
            update_checked = result[2] if kind == "bangumi" else None
            self.day_schedules._plan_day_schedule(kind, result[0], day=day, update_checked=update_checked)
            logger.info(f"[BiliBot] {activity.new_day_log}：{[f'{h}:{m:02d}' for h, m in result[0]]}")
        elif activity.restore_in_loop and not self.day_schedules._day_schedule(kind, day)[0]:
            self.day_schedules._restore_day_schedule(kind, day)

    def _slot_entry(self, kind):
        """这一格做什么：(入口, 触发日志 type, 日志前缀)。追番每天第一次到点先查更新。"""
        activity = SCHEDULED_ACTIVITIES[kind]
        if activity.first_of_day and not self.day_schedules._bangumi_update_checked:
            self.day_schedules._bangumi_update_checked = True
            entry, log_type, trigger_log = activity.first_of_day
            return attrgetter(entry)(self._host), log_type, trigger_log, False
        return attrgetter(activity.entry)(self._host), activity.log_type, activity.trigger_log, True

    async def _run_scheduled_slot(self, kind, day, slot):
        entry, log_type, trigger_log, regular = self._slot_entry(kind)
        # 每日计划给这一格定的「看什么」（只有平常的入口收，追番每天第一次查更新不收）。
        extra = self._slot_focus(kind, slot) if regular else {}
        # 主干已经把这一格记成 running；json 镜像（含追番查过更新）跟着写。
        self.day_schedules._save_day_schedule(kind, day=day)
        if log_type:
            self.activity_logs.trigger.append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": log_type,
                "scheduled": slot, "status": "triggered",
            })
        logger.info(f"[BiliBot] {trigger_log}（{slot}）")
        await entry(**extra)

    def _slot_focus(self, kind, slot):
        """出错也只当没有重点，不能挡住这一格照常跑。"""
        try:
            return self.daily_plan.slot_focus(kind, slot)
        except Exception as exc:
            logger.warning(f"[BiliBot] 读这一格的重点出错，照常做：{exc}")
            return {}
