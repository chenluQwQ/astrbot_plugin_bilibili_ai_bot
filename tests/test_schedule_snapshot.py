"""调度快照：五种定时活动在主循环里现在怎么触发。

收成「活动表 + 统一触发」之前先把现有行为钉住，包括各家不一样的地方
（动态不写触发日志、关注动态巡视不会从文件恢复日程、追番第一个时刻先查更新）。
改调度时这些断言要么照样过，要么在 CHANGELOG 里写明为什么变。

跑真的 ``Runner.main_loop``（``core/runner.py``），时间是假的，每个用例只跑一轮。
"""
import asyncio
import types
from operator import attrgetter
import unittest
from datetime import datetime as real_datetime
from unittest.mock import AsyncMock, MagicMock

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from core import config
from test_main_loop_schedule import _Clock, _bot, _load_main_loop, seed, triggered as _triggered

TODAY = real_datetime(2026, 10, 29, 10, 1)
DAY = "2026-10-29"


def triggered(bot, kind):
    return _triggered(bot, kind, DAY)


# 每种活动：开关、日程文件、属性前缀、生成/恢复/保存（schedule_files 上的）/入口函数、触发日志类型（None = 不写）。
ACTIVITIES = {
    "proactive": dict(
        kind="proactive",
        switches={"ENABLE_PROACTIVE": True}, file="SCHEDULE_FILE", attr="_proactive",
        generate="_generate_daily_schedule", restore="_load_or_generate_schedule",
        save="_save_schedule_state", entry="proactive_browse._run_proactive", log_type="proactive_video",
    ),
    "dynamic": dict(
        kind="dynamic",
        switches={"ENABLE_DYNAMIC": True}, file="DYNAMIC_SCHEDULE_FILE", attr="_dynamic",
        generate="_generate_dynamic_schedule", restore="_load_or_generate_dynamic_schedule",
        save="_save_dynamic_schedule_state", entry="dynamic_post.run", log_type=None,
    ),
    "dynamic_watch": dict(
        kind="dynamic_watch",
        switches={"ENABLE_DYNAMIC_WATCH": True}, file="DYNAMIC_WATCH_SCHEDULE_FILE",
        attr="_dynamic_watch", generate="_generate_dynamic_watch_schedule", restore=None,
        save="_save_dynamic_watch_schedule_state", entry="dynamic_watch.run",
        log_type="dynamic_watch",
    ),
    "special_follow": dict(
        kind="special_follow",
        switches={"SPECIAL_FOLLOW_ENABLED": True}, file="SPECIAL_FOLLOW_SCHEDULE_FILE",
        attr="_special_follow", generate="_generate_special_follow_schedule",
        restore="_load_or_generate_special_follow_schedule",
        save="_save_special_follow_schedule_state", entry="special_follow.run",
        log_type="special_follow",
    ),
}
FILES = [spec["file"] for spec in ACTIVITIES.values()] + ["BANGUMI_SCHEDULE_FILE"]


def _run_once(bot, now_dt):
    clock = _Clock(bot, start=0, step=20, rounds=1)
    namespace = {
        "asyncio": types.SimpleNamespace(
            sleep=clock.sleep,
            create_task=lambda coro: asyncio.ensure_future(coro),
            CancelledError=asyncio.CancelledError,
        ),
        "time": types.SimpleNamespace(time=lambda: clock.now),
        "datetime": types.SimpleNamespace(now=lambda: now_dt),
        "logger": bot.loop_logger,
        "traceback": MagicMock(),
        "PROACTIVE_TRIGGER_LOG_FILE": config.PROACTIVE_TRIGGER_LOG_FILE,
        **{name: getattr(config, name) for name in FILES},
    }
    return _load_main_loop(namespace)(bot)


def _set_entry(bot, entry, value):
    """``dynamic_post.run`` 这种带点的入口挂在同名的假对象上。"""
    owner, _, name = entry.rpartition(".")
    if owner:
        setattr(bot, owner, types.SimpleNamespace())
    setattr(getattr(bot, owner) if owner else bot, name, value)


def _activity_bot(spec, *, file_date, times, triggered=(), generated=None, busy=False, enabled=True):
    files = {getattr(config, spec["file"]): {"date": file_date}}
    switches = {key: enabled for key in spec["switches"]}
    bot = _bot({**switches, "COOKIE_CHECK_INTERVAL": 10_000}, files=files)
    attr = spec["attr"]
    seed(bot, spec["kind"], file_date, times, triggered)
    task = MagicMock()
    task.done.return_value = not busy
    setattr(bot, f"{attr}_task", task if busy else None)
    setattr(bot.schedule_files, spec["generate"], MagicMock(return_value=generated or (list(times), set())))
    if spec["restore"]:
        setattr(bot.schedule_files, spec["restore"], MagicMock(return_value=(list(times), set())))
    setattr(bot.schedule_files, spec["save"], MagicMock())
    _set_entry(bot, spec["entry"], AsyncMock())
    return bot


def _log_types(bot):
    return [entry["type"] for entry in bot.json_store._load_json(config.PROACTIVE_TRIGGER_LOG_FILE, [])]


class ScheduledActivityTests(unittest.IsolatedAsyncioTestCase):
    async def _check(self, bot):
        await asyncio.sleep(0)
        bot.loop_logger.error.assert_not_called()

    async def test_due_slot_triggers_once_and_is_recorded(self):
        for name, spec in ACTIVITIES.items():
            with self.subTest(name):
                bot = _activity_bot(spec, file_date="2026-10-29", times=[(10, 0)])
                await _run_once(bot, TODAY)
                await self._check(bot)
                attrgetter(spec["entry"])(bot).assert_awaited_once()
                self.assertEqual(triggered(bot, name), {"10:00"})
                getattr(bot.schedule_files, spec["save"]).assert_called_once_with([(10, 0)], {"10:00"})
                expected = [spec["log_type"]] if spec["log_type"] else []
                self.assertEqual(_log_types(bot), expected)

    async def test_new_day_regenerates_before_checking_slots(self):
        for name, spec in ACTIVITIES.items():
            with self.subTest(name):
                bot = _activity_bot(
                    spec, file_date="2026-10-28", times=[(10, 0)], triggered={"10:00"},
                    generated=([(10, 0)], set()),
                )
                await _run_once(bot, TODAY)
                await self._check(bot)
                getattr(bot.schedule_files, spec["generate"]).assert_called_once()
                attrgetter(spec["entry"])(bot).assert_awaited_once()

    async def test_triggered_slot_is_not_run_again(self):
        for name, spec in ACTIVITIES.items():
            with self.subTest(name):
                bot = _activity_bot(spec, file_date="2026-10-29", times=[(10, 0)], triggered={"10:00"})
                await _run_once(bot, TODAY)
                await self._check(bot)
                attrgetter(spec["entry"])(bot).assert_not_awaited()

    async def test_busy_activity_waits_without_marking_the_slot(self):
        for name, spec in ACTIVITIES.items():
            with self.subTest(name):
                bot = _activity_bot(spec, file_date="2026-10-29", times=[(10, 0)], busy=True)
                await _run_once(bot, TODAY)
                await self._check(bot)
                attrgetter(spec["entry"])(bot).assert_not_awaited()
                self.assertEqual(triggered(bot, name), set())

    async def test_only_one_slot_starts_per_round(self):
        for name, spec in ACTIVITIES.items():
            with self.subTest(name):
                bot = _activity_bot(spec, file_date="2026-10-29", times=[(9, 58), (10, 0)])
                await _run_once(bot, TODAY)
                await self._check(bot)
                attrgetter(spec["entry"])(bot).assert_awaited_once()
                self.assertEqual(triggered(bot, name), {"9:58"})

    async def test_stale_slot_is_never_replayed(self):
        for name, spec in ACTIVITIES.items():
            with self.subTest(name):
                # 过了 5 分钟的宽限就不补跑。以前悄悄留着不跑，挂到主干后明确记成跳过（CHANGELOG）。
                bot = _activity_bot(spec, file_date="2026-10-29", times=[(9, 0)])
                await _run_once(bot, TODAY)
                await self._check(bot)
                attrgetter(spec["entry"])(bot).assert_not_awaited()
                run = bot.day_schedules._runs().runs(DAY, name)[0]
                self.assertEqual((run.state.value, run.reason), ("skipped", "错过了时间"))

    async def test_same_day_with_empty_times_restores_from_file(self):
        for name, spec in ACTIVITIES.items():
            with self.subTest(name):
                bot = _activity_bot(spec, file_date="2026-10-29", times=[])
                await _run_once(bot, TODAY)
                await self._check(bot)
                if spec["restore"]:
                    getattr(bot.schedule_files, spec["restore"]).assert_called_once()
                else:
                    # 关注动态巡视没有这一步：同一天里日程丢了就一直空着。
                    attrgetter(spec["entry"])(bot).assert_not_awaited()

    async def test_switch_off_does_nothing(self):
        for name, spec in ACTIVITIES.items():
            with self.subTest(name):
                bot = _activity_bot(spec, file_date="2026-10-28", times=[(10, 0)], enabled=False)
                await _run_once(bot, TODAY)
                await self._check(bot)
                getattr(bot.schedule_files, spec["generate"]).assert_not_called()
                attrgetter(spec["entry"])(bot).assert_not_awaited()


def _bangumi_bot(*, times, triggered=(), update_checked=False, busy=False, proactive=True):
    files = {config.BANGUMI_SCHEDULE_FILE: {"date": "2026-10-29"}}
    bot = _bot({
        "ENABLE_BANGUMI": True, "BANGUMI_PROACTIVE": proactive, "COOKIE_CHECK_INTERVAL": 10_000,
    }, files=files)
    seed(bot, "bangumi", DAY, times, triggered)
    bot.day_schedules._bangumi_update_checked = update_checked
    task = MagicMock()
    task.done.return_value = False
    bot._bangumi_task = task if busy else None
    bot.schedule_files._generate_bangumi_schedule = MagicMock(return_value=(list(times), set(), False))
    bot.schedule_files._load_or_generate_bangumi_schedule = MagicMock(return_value=(list(times), set(), False))
    bot.schedule_files._save_bangumi_schedule_state = MagicMock()
    bot.bangumi = MagicMock(check_updates=AsyncMock(), run=AsyncMock())
    return bot


class BangumiScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_due_slot_checks_updates(self):
        bot = _bangumi_bot(times=[(10, 0)])
        await _run_once(bot, TODAY)
        await asyncio.sleep(0)
        bot.loop_logger.error.assert_not_called()
        bot.bangumi.check_updates.assert_awaited_once()
        bot.bangumi.run.assert_not_awaited()
        self.assertTrue(bot.day_schedules._bangumi_update_checked)
        bot.schedule_files._save_bangumi_schedule_state.assert_called_once_with([(10, 0)], {"10:00"}, True)
        self.assertEqual(_log_types(bot), ["bangumi_update_check"])

    async def test_later_slots_watch(self):
        bot = _bangumi_bot(times=[(10, 0)], update_checked=True)
        await _run_once(bot, TODAY)
        await asyncio.sleep(0)
        bot.bangumi.run.assert_awaited_once()
        bot.bangumi.check_updates.assert_not_awaited()
        self.assertEqual(_log_types(bot), ["bangumi_watch"])

    async def test_busy_waits(self):
        bot = _bangumi_bot(times=[(10, 0)], update_checked=True, busy=True)
        await _run_once(bot, TODAY)
        await asyncio.sleep(0)
        bot.bangumi.run.assert_not_awaited()
        self.assertEqual(triggered(bot, "bangumi"), set())

    async def test_needs_proactive_switch(self):
        bot = _bangumi_bot(times=[(10, 0)], update_checked=True, proactive=False)
        await _run_once(bot, TODAY)
        await asyncio.sleep(0)
        bot.bangumi.run.assert_not_awaited()
        bot.schedule_files._generate_bangumi_schedule.assert_not_called()

    async def test_same_day_with_empty_times_restores_from_file(self):
        bot = _bangumi_bot(times=[])
        await _run_once(bot, TODAY)
        await asyncio.sleep(0)
        bot.schedule_files._load_or_generate_bangumi_schedule.assert_called_once()


if __name__ == "__main__":
    unittest.main()
