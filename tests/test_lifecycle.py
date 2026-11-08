"""core.lifecycle: the day, the activity state machine and the single main loop."""

import asyncio
import logging
import sqlite3
import unittest
from unittest import mock
from datetime import datetime

from core.lifecycle.activities import ActivityRuns, ActivityState, IllegalTransition
from core.lifecycle import scheduler as scheduler_module
from core.lifecycle.day import SleepWindow, today
from core.lifecycle.scheduler import ASLEEP, AWAKE, Routine, Scheduler


class SleepWindowTests(unittest.TestCase):
    def test_same_day_window(self):
        sleep = SleepWindow(2, 8)
        self.assertTrue(sleep.is_asleep(datetime(2026, 1, 1, 2, 0)))
        self.assertTrue(sleep.is_asleep(datetime(2026, 1, 1, 7, 59)))
        self.assertFalse(sleep.is_asleep(datetime(2026, 1, 1, 8, 0)))
        self.assertFalse(sleep.is_asleep(datetime(2026, 1, 1, 1, 59)))

    def test_window_across_midnight(self):
        sleep = SleepWindow(23, 7)
        self.assertTrue(sleep.is_asleep(datetime(2026, 1, 1, 23, 30)))
        self.assertTrue(sleep.is_asleep(datetime(2026, 1, 1, 3, 0)))
        self.assertFalse(sleep.is_asleep(datetime(2026, 1, 1, 7, 0)))
        self.assertFalse(sleep.is_asleep(datetime(2026, 1, 1, 22, 59)))

    def test_equal_start_and_end_never_sleeps(self):
        sleep = SleepWindow(5, 5)
        self.assertFalse(any(sleep.is_asleep_at_minute(m) for m in range(0, 1440, 7)))

    def test_matches_the_old_hour_based_check(self):
        for start in range(24):
            for end in range(24):
                for hour in range(24):
                    old = (start <= hour < end) if start <= end else (hour >= start or hour < end)
                    if start == end:
                        old = False
                    self.assertEqual(SleepWindow(start, end).is_asleep(datetime(2026, 1, 1, hour, 30)), old)

    def test_today_is_the_calendar_date(self):
        self.assertEqual(today(datetime(2026, 10, 28, 0, 30)), "2026-10-28")


class ActivityRunsTests(unittest.TestCase):
    def setUp(self):
        self.runs = ActivityRuns(sqlite3.connect(":memory:"))
        self.runs.plan("d", "watch", ["10:00", "15:00"])

    def states(self):
        return {run.slot: run.state for run in self.runs.runs("d", "watch")}

    def test_happy_path(self):
        self.runs.start("d", "watch", "10:00")
        self.assertEqual([r.slot for r in self.runs.running()], ["10:00"])
        self.runs.finish("d", "watch", "10:00", ok=True)
        self.assertEqual(self.states()["10:00"], ActivityState.DONE)
        self.assertEqual(self.runs.running(), [])

    def test_illegal_transitions_raise(self):
        with self.assertRaises(IllegalTransition):
            self.runs.finish("d", "watch", "10:00", ok=True)       # never started
        self.runs.start("d", "watch", "10:00")
        with self.assertRaises(IllegalTransition):
            self.runs.start("d", "watch", "10:00")                 # already running
        with self.assertRaises(IllegalTransition):
            self.runs.skip("d", "watch", "10:00", "late")          # running cannot be skipped
        with self.assertRaises(IllegalTransition):
            self.runs.start("d", "watch", "23:00")                 # never planned

    def test_planning_again_keeps_existing_states(self):
        self.runs.start("d", "watch", "10:00")
        self.runs.plan("d", "watch", ["10:00", "20:00"])
        self.assertEqual(self.states(), {
            "10:00": ActivityState.RUNNING, "15:00": ActivityState.PLANNED, "20:00": ActivityState.PLANNED,
        })

    def test_restart_marks_running_as_interrupted(self):
        self.runs.start("d", "watch", "10:00")
        self.assertEqual(self.runs.interrupt_running(), 1)
        self.assertEqual(self.states()["10:00"], ActivityState.INTERRUPTED)
        with self.assertRaises(IllegalTransition):
            self.runs.start("d", "watch", "10:00")                 # interrupted is never rerun

    def test_replan_keeps_what_really_happened(self):
        self.runs.plan("d", "watch", ["9:00"])
        self.runs.skip("d", "watch", "9:00", "late")
        self.runs.start("d", "watch", "10:00")
        self.runs.replan("d", "watch", ["15:00", "18:00"])
        # 10:00 真跑过，留着；9:00 只是跳过、没跑过，和 15:00 之外的 planned 一样删掉。
        self.assertEqual(self.states(), {
            "10:00": ActivityState.RUNNING, "15:00": ActivityState.PLANNED, "18:00": ActivityState.PLANNED,
        })

    def test_override_follows_the_page(self):
        self.runs.plan("d", "watch", ["11:00"])
        self.runs.start("d", "watch", "10:00")
        self.runs.finish("d", "watch", "10:00", ok=True)
        self.runs.start("d", "watch", "11:00")
        # 页面：10:00 删了，11:00 标回没做，15:00 标成做过，新加 20:00。
        self.runs.override("d", "watch", ["11:00", "15:00", "20:00"], {"15:00"}, "page")
        self.assertEqual(self.states(), {
            "11:00": ActivityState.PLANNED, "15:00": ActivityState.SKIPPED, "20:00": ActivityState.PLANNED,
        })

    def test_mark_skipped_leaves_started_slots_alone(self):
        self.runs.start("d", "watch", "10:00")
        self.runs.mark_skipped("d", "watch", "10:00", "overdue")
        self.runs.mark_skipped("d", "watch", "12:00", "imported")
        self.assertEqual(self.states()["10:00"], ActivityState.RUNNING)
        self.assertEqual(self.states()["12:00"], ActivityState.SKIPPED)


class FakeBranch:
    def __init__(self, kind, slots, phase=AWAKE, fail=False):
        self.kind, self.phase, self.slots, self.fail = kind, phase, slots, fail
        self.on = True
        self.plan_calls = 0
        self.ran = []
        self.release = asyncio.Event()
        self.release.set()

    def enabled(self):
        return self.on

    def plan(self, day, now):
        self.plan_calls += 1
        return list(self.slots)

    async def run(self, day, slot):
        self.ran.append(slot)
        await self.release.wait()
        if self.fail:
            raise RuntimeError("boom")


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 别的测试装的假 astrbot 可能先被导入，日志断言要的是真的 "astrbot" logger
        patcher = mock.patch.object(scheduler_module, "logger", logging.getLogger("astrbot"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = datetime(2026, 10, 28, 9, 0)
        self.runs = ActivityRuns(sqlite3.connect(":memory:"))
        self.scheduler = Scheduler(self.runs, lambda: SleepWindow(2, 8), clock=lambda: self.now)

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0)

    def state(self, kind, slot):
        return {r.slot: r.state for r in self.runs.runs("2026-10-28", kind)}[slot]

    async def test_plans_once_and_runs_a_due_slot_once(self):
        branch = FakeBranch("watch", ["10:00", "15:00"])
        self.scheduler.register(branch)
        self.assertEqual(await self.scheduler.tick(), [])
        self.now = datetime(2026, 10, 28, 10, 2)
        self.assertEqual(await self.scheduler.tick(), ["watch@10:00"])
        await self.settle()
        self.assertEqual(await self.scheduler.tick(), [])
        self.assertEqual(branch.ran, ["10:00"])
        self.assertEqual(branch.plan_calls, 1)
        self.assertEqual(self.state("watch", "10:00"), ActivityState.DONE)

    async def test_failure_is_recorded(self):
        self.scheduler.register(FakeBranch("watch", ["09:00"], fail=True))
        with self.assertLogs("astrbot", level="ERROR"):
            await self.scheduler.tick()
            await self.settle()
        run = self.runs.runs("2026-10-28", "watch")[0]
        self.assertEqual(run.state, ActivityState.FAILED)
        self.assertEqual(run.reason, "boom")

    async def test_busy_branch_does_not_start_twice_and_late_slot_is_skipped(self):
        branch = FakeBranch("watch", ["09:00", "09:03"])
        branch.release.clear()
        self.scheduler.register(branch)
        await self.scheduler.tick()
        await self.settle()
        self.now = datetime(2026, 10, 28, 9, 4)
        self.assertEqual(await self.scheduler.tick(), [])
        self.now = datetime(2026, 10, 28, 9, 9)
        await self.scheduler.tick()
        self.assertEqual(branch.ran, ["09:00"])
        self.assertEqual(self.state("watch", "09:03"), ActivityState.SKIPPED)
        branch.release.set()
        await self.settle()
        self.assertEqual(self.state("watch", "09:00"), ActivityState.DONE)

    async def test_missed_slot_is_skipped_not_replayed(self):
        branch = FakeBranch("watch", ["08:30"])
        self.scheduler.register(branch)
        await self.scheduler.tick()
        self.assertEqual(branch.ran, [])
        self.assertEqual(self.state("watch", "08:30"), ActivityState.SKIPPED)

    async def test_grace_window_edges(self):
        # 原来 ScheduleMixin._schedule_slot_due 钉的三条（默认 5 分钟）：晚 3 分钟照跑，晚整 5 分钟、晚半天都不补
        for now, ran in ((datetime(2026, 10, 28, 12, 3), ["12:00"]), (datetime(2026, 10, 28, 12, 5), []),
                         (datetime(2026, 10, 28, 23, 3), [])):
            with self.subTest(now=now):
                self.runs = ActivityRuns(sqlite3.connect(":memory:"))
                self.now = now
                self.scheduler = Scheduler(self.runs, lambda: SleepWindow(2, 8), clock=lambda: self.now)
                branch = FakeBranch("watch", ["12:00"])
                self.scheduler.register(branch)
                await self.scheduler.tick()
                await self.settle()
                self.assertEqual(branch.ran, ran)

    async def test_one_slot_per_branch_per_tick_and_branches_are_independent(self):
        a = FakeBranch("watch", ["09:00", "09:01"])
        b = FakeBranch("post", ["09:00"])
        self.now = datetime(2026, 10, 28, 9, 1)
        self.scheduler.register(a)
        self.scheduler.register(b)
        self.assertEqual(sorted(await self.scheduler.tick()), ["post@09:00", "watch@09:00"])

    async def test_phase_and_switch_gate_branches(self):
        night = FakeBranch("consolidate", ["03:00"], phase=ASLEEP)
        day = FakeBranch("watch", ["03:00"])
        self.scheduler.register(night)
        self.scheduler.register(day)
        self.now = datetime(2026, 10, 28, 3, 1)
        self.assertEqual(await self.scheduler.tick(), ["consolidate@03:00"])
        self.assertEqual(day.plan_calls, 0)

        off = FakeBranch("post", ["09:00"])
        off.on = False
        self.scheduler.register(off)
        self.now = datetime(2026, 10, 28, 9, 0)
        await self.scheduler.tick()
        self.assertEqual(off.plan_calls, 0)

    async def test_cancelled_run_is_recorded_interrupted(self):
        branch = FakeBranch("watch", ["09:00"])
        branch.release.clear()
        self.scheduler.register(branch)
        await self.scheduler.tick()
        await self.settle()
        self.scheduler._tasks["watch"].cancel()
        await self.settle()
        self.assertEqual(self.state("watch", "09:00"), ActivityState.INTERRUPTED)

    async def test_branch_hooks_prepare_and_external_busy(self):
        branch = FakeBranch("watch", ["09:00"])
        prepared = []
        branch.prepare = lambda day, now: (prepared.append(day), self.runs.plan(day, "watch", ["09:00"]))
        branch.busy = lambda: True          # 别处（手动触发）正占着
        self.scheduler.register(branch)
        self.assertEqual(await self.scheduler.tick(), [])
        self.assertEqual((prepared, branch.plan_calls), (["2026-10-28"], 0))
        branch.busy = lambda: False
        self.assertEqual(await self.scheduler.tick(), ["watch@09:00"])

    async def test_routines_run_in_order_around_slots_and_by_phase(self):
        calls = []

        async def poll():
            calls.append("poll")

        branch = FakeBranch("watch", ["09:00"])
        branch.run = lambda day, slot: calls.append(f"slot {slot}") or asyncio.sleep(0)
        self.scheduler.register(branch)
        self.scheduler.add_routine(Routine("poll", poll, AWAKE, after_slots=True))
        self.scheduler.add_routine(Routine("mood", lambda: calls.append("mood"), AWAKE))
        self.scheduler.add_routine(Routine("plan", lambda: calls.append("plan")))
        self.scheduler.add_routine(Routine("night", lambda: calls.append("night"), ASLEEP))
        self.scheduler.add_routine(Routine("off", lambda: calls.append("off"), enabled=lambda: False))
        await self.scheduler.tick()
        await self.settle()
        self.assertEqual(calls, ["mood", "plan", "poll", "slot 09:00"])  # 任务在 tick 之后才跑
        calls.clear()
        self.now = datetime(2026, 10, 28, 3, 0)
        await self.scheduler.tick()
        self.assertEqual(calls, ["plan", "night"])
        with self.assertRaises(ValueError):
            self.scheduler.add_routine(Routine("plan", lambda: None))

    async def test_one_broken_routine_does_not_stop_the_rest(self):
        # 附属的（心情、每日计划）出错，后面的（评论轮询）照跑，下一轮也照常再试
        calls = []

        def broken():
            calls.append("mood")
            raise RuntimeError("心情文件坏了")

        async def comments():
            calls.append("comments")

        self.scheduler.add_routine(Routine("mood", broken, AWAKE))
        self.scheduler.add_routine(Routine("plan", lambda: calls.append("plan")))
        self.scheduler.add_routine(Routine("comments", comments, AWAKE, after_slots=True))
        for _ in range(2):
            with self.assertLogs("astrbot", level="ERROR") as logs:
                await self.scheduler.tick()
            self.assertIn("mood", logs.output[0])
        self.assertEqual(calls, ["mood", "plan", "comments"] * 2)

    async def test_one_broken_branch_does_not_stop_the_others(self):
        broken = FakeBranch("dynamic", ["09:00"])
        broken.prepare = lambda day, now: (_ for _ in ()).throw(ValueError("日程文件坏了"))
        fine = FakeBranch("watch", ["09:00"])
        calls = []
        self.scheduler.register(broken)
        self.scheduler.register(fine)
        self.scheduler.add_routine(Routine("comments", lambda: calls.append("comments"), AWAKE, after_slots=True))
        with self.assertLogs("astrbot", level="ERROR") as logs:
            self.assertEqual(await self.scheduler.tick(), ["watch@09:00"])
        await self.settle()
        self.assertIn("dynamic", logs.output[0])
        self.assertEqual((broken.ran, fine.ran, calls), ([], ["09:00"], ["comments"]))

    async def test_cancelling_a_routine_still_stops_the_round(self):
        async def stop():
            raise asyncio.CancelledError

        self.scheduler.add_routine(Routine("stop", stop))
        with self.assertRaises(asyncio.CancelledError):
            await self.scheduler.tick()

    async def test_startup_interrupts_leftover_runs(self):
        self.runs.plan("2026-10-28", "watch", ["08:59"])
        self.runs.start("2026-10-28", "watch", "08:59")
        self.assertEqual(self.scheduler.startup(), 1)
        self.assertEqual(self.state("watch", "08:59"), ActivityState.INTERRUPTED)

    def test_registering_the_same_kind_twice_is_an_error(self):
        self.scheduler.register(FakeBranch("watch", []))
        with self.assertRaises(ValueError):
            self.scheduler.register(FakeBranch("watch", []))


if __name__ == "__main__":
    unittest.main()
