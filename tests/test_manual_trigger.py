"""手动叫一次（命令、工具、聊天）走主干：记进 activity_runs，但不占日程。"""
import asyncio
import sqlite3
import types
import unittest
from unittest.mock import patch
from datetime import datetime

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from core.lifecycle.activities import ActivityRuns, ActivityState
from core.lifecycle.day import SleepWindow
from core.lifecycle.now_doing import NowDoing
from core.lifecycle.scheduler import Scheduler
from core.features.proactive_browse import ProactiveBrowse
from core.activities import ROUTINES
from schedule_parts import ScheduleParts
from core.settings import Settings

DAY = "2026-10-28"


class ManualRunTableTests(unittest.TestCase):
    def setUp(self):
        self.runs = ActivityRuns(sqlite3.connect(":memory:"))

    def test_manual_run_does_not_count_as_planned(self):
        self.runs.start_manual(DAY, "watch", "09:00:00", "手动触发")
        self.assertFalse(self.runs.is_planned(DAY, "watch"))

    def test_same_second_gets_a_serial(self):
        first = self.runs.start_manual(DAY, "watch", "09:00:00", "手动触发")
        self.runs.finish(DAY, "watch", first, ok=True)
        second = self.runs.start_manual(DAY, "watch", "09:00:00", "手动触发")
        self.assertEqual((first, second), ("manual-09:00:00", "manual-09:00:00-2"))

    def test_replan_and_override_leave_manual_runs_alone(self):
        slot = self.runs.start_manual(DAY, "watch", "09:00:00", "手动触发")
        self.runs.replan(DAY, "watch", ["10:00"])
        self.runs.override(DAY, "watch", ["11:00"], set(), "网页")
        states = {run.slot: run.state for run in self.runs.runs(DAY, "watch")}
        self.assertEqual(states, {"11:00": ActivityState.PLANNED, slot: ActivityState.RUNNING})


class FakeBranch:
    phase = "awake"

    def __init__(self, kind="watch"):
        self.kind = kind
        self.calls = []
        self.gate = asyncio.Event()

    def enabled(self):
        return True

    def plan(self, day, now):
        return ["10:00"]

    async def run(self, day, slot):
        self.calls.append(slot)

    async def run_now(self, **kwargs):
        self.calls.append(kwargs)
        await self.gate.wait()
        if kwargs.get("boom"):
            raise RuntimeError("坏了")
        return "结果"


class SchedulerRunNowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 28, 9, 0, 5)
        self.runs = ActivityRuns(sqlite3.connect(":memory:"))
        self.scheduler = Scheduler(self.runs, lambda: SleepWindow(2, 8), clock=lambda: self.now)
        self.branch = FakeBranch()
        self.scheduler.register(self.branch)

    def only_run(self):
        runs = [run for run in self.runs.runs(DAY, "watch") if run.slot.startswith("manual-")]
        self.assertEqual(len(runs), 1)
        return runs[0]

    async def test_records_the_run_and_passes_arguments(self):
        task = self.scheduler.run_now("watch", max_watch=1)
        self.assertIs(self.only_run().state, ActivityState.RUNNING)
        self.branch.gate.set()
        self.assertEqual(await task, "结果")
        self.assertEqual(self.branch.calls, [{"max_watch": 1}])
        run = self.only_run()
        self.assertIs(run.state, ActivityState.DONE)
        self.assertEqual(run.reason, "手动触发")

    async def test_busy_is_decided_once_and_nothing_is_recorded(self):
        first = self.scheduler.run_now("watch")
        self.assertIsNone(self.scheduler.run_now("watch"))
        self.assertTrue(self.scheduler.busy("watch"))
        self.only_run()
        self.branch.gate.set()
        await first

    async def test_failure_is_recorded_and_returns_none(self):
        self.branch.gate.set()
        self.assertIsNone(await self.scheduler.run_now("watch", boom=True))
        run = self.only_run()
        self.assertIs(run.state, ActivityState.FAILED)
        self.assertEqual(run.reason, "坏了")

    async def test_cancel_is_recorded_as_interrupted(self):
        task = self.scheduler.run_now("watch")
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIs(self.only_run().state, ActivityState.INTERRUPTED)

    async def test_due_slot_waits_while_a_manual_run_is_going(self):
        task = self.scheduler.run_now("watch")
        self.now = datetime(2026, 10, 28, 10, 1)
        self.assertEqual(await self.scheduler.tick(), [])
        self.branch.gate.set()
        await task
        self.assertEqual(await self.scheduler.tick(), ["watch@10:00"])

    async def test_manual_run_before_the_first_tick_does_not_stop_planning(self):
        self.branch.gate.set()
        await self.scheduler.run_now("watch")
        await self.scheduler.tick()
        self.assertTrue(self.runs.is_planned(DAY, "watch"))


class Bot(ScheduleParts):
    """真的日程对象 + 假的活动入口。"""

    def __init__(self):
        self.settings = Settings({"SLEEP_START": 2, "SLEEP_END": 8})
        self._proactive_task = None
        self._dynamic_task = None
        self.calls = []
        self.dynamic_result = True
        self.files = {}
        self.now_doing = NowDoing()
        self.proactive_browse = types.SimpleNamespace(_run_proactive=self._run_proactive)
        # 例行检查都换成什么也不做的；带点的（``runner.xxx``）挂在同名的假对象上。
        for _name, method, *_rest in ROUTINES:
            owner, _, attr = method.rpartition(".")
            if owner and not hasattr(self, owner):
                setattr(self, owner, types.SimpleNamespace())
            setattr(getattr(self, owner) if owner else self, attr, lambda: None)

    def _load_json(self, path, default=None):
        return self.files.get(path, default)

    def _save_json(self, path, value):
        self.files[path] = value

    async def _run_proactive(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(0)

    @property
    def dynamic_post(self):
        return types.SimpleNamespace(run=self._run_dynamic)

    async def _run_dynamic(self, **kwargs):
        self.calls.append(kwargs)
        return self.dynamic_result


class PluginManualTriggerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Bot()
        self.addCleanup(self.bot.day_schedules._runs().close)

    def manual_runs(self, kind):
        day = datetime.now().strftime("%Y-%m-%d")
        return [run for run in self.bot.day_schedules._runs().runs(day, kind) if run.slot.startswith("manual-")]

    async def test_task_is_kept_where_stopping_the_bot_cancels_it(self):
        task = self.bot.activities._run_activity_now("proactive", max_watch=1)
        self.assertIs(self.bot._proactive_task, task)
        self.assertTrue(self.bot.activities._activity_busy("proactive"))
        self.assertIsNone(self.bot.activities._run_activity_now("proactive", max_watch=1))
        await task
        self.assertEqual(self.bot.calls, [{"max_watch": 1}])
        self.assertIs(self.manual_runs("proactive")[0].state, ActivityState.DONE)

    async def test_manual_run_stays_out_of_the_day_schedule(self):
        await self.bot.activities._run_activity_now("proactive", max_watch=1)
        self.assertEqual(self.bot.day_schedules._day_schedule("proactive"), ([], set()))

    async def test_saving_the_schedule_page_keeps_the_manual_record(self):
        await self.bot.activities._run_activity_now("proactive", max_watch=1)
        times = [(23, 30)]
        self.bot.day_schedules._runs().override(self.bot.day_schedules._today(), "proactive", self.bot.schedule_rules._format_time_pairs(times), set(), "网页")
        self.assertEqual(self.bot.day_schedules._day_schedule("proactive"), (times, set()))
        self.assertEqual(len(self.manual_runs("proactive")), 1)

    async def test_dynamic_command_still_learns_it_was_skipped(self):
        self.bot.dynamic_result = False
        self.assertIs(await self.bot.activities._run_activity_now("dynamic", human_initiated=True), False)
        self.assertEqual(self.bot.calls, [{"human_initiated": True}])

    async def test_cancelling_a_real_entry_is_recorded_as_interrupted(self):
        """入口函数不能把取消吞掉，否则停用插件时被取消的活动会记成做完。"""
        started = asyncio.Event()

        async def inner(_browse, **_kwargs):
            started.set()
            await asyncio.Event().wait()

        self.bot.proactive_browse = ProactiveBrowse(self.bot)
        with patch.object(ProactiveBrowse, "_run_proactive_inner", inner):
            task = self.bot.activities._run_activity_now("proactive", max_watch=1)
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            await asyncio.wait({task})
        self.assertTrue(task.cancelled())
        self.assertIs(self.manual_runs("proactive")[0].state, ActivityState.INTERRUPTED)


if __name__ == "__main__":
    unittest.main()
