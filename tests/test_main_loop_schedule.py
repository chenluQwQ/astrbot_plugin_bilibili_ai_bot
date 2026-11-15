"""主循环的定时检查：Cookie 定期检查、关注动态巡视换天。

跑真的 ``Runner.main_loop``（``core/runner.py``），时间和睡眠都是假的，不需要 AstrBot。
"""
from base_parts import attach_base
import asyncio
import types
import unittest
from datetime import datetime as real_datetime
from unittest.mock import AsyncMock, MagicMock, patch

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from core import config
from core import runner as runner_module
from schedule_parts import attach_schedule
from core.settings import Settings


def _load_main_loop(namespace):
    """``bot.runner`` 的主循环，跑的时候 ``core/runner.py`` 里的时间、睡眠、日志换成 ``namespace`` 里的。"""
    fakes = {name: namespace[name] for name in ("asyncio", "time", "datetime", "logger", "traceback")}

    async def run(bot):
        with patch.multiple(runner_module, **fakes):
            await bot.runner.main_loop()
    return run


class _Clock:
    """假的时间：每次主循环睡觉就往前拨 ``step`` 秒，跑够 ``rounds`` 轮就停。"""

    def __init__(self, bot, start, step, rounds):
        self.bot, self.now, self.step, self.rounds = bot, start, step, rounds

    async def sleep(self, _seconds):
        self.now += self.step
        self.rounds -= 1
        if self.rounds <= 0:
            self.bot._running = False


def _run(bot, clock, now_dt):
    fake_datetime = types.SimpleNamespace(now=lambda: now_dt)
    namespace = {
        "asyncio": types.SimpleNamespace(
            sleep=clock.sleep,
            create_task=lambda coro: asyncio.ensure_future(coro),
            CancelledError=asyncio.CancelledError,
        ),
        "time": types.SimpleNamespace(time=lambda: clock.now),
        "datetime": fake_datetime,
        "logger": bot.loop_logger,
        "traceback": MagicMock(),
        "DYNAMIC_WATCH_SCHEDULE_FILE": config.DYNAMIC_WATCH_SCHEDULE_FILE,
        "PROACTIVE_TRIGGER_LOG_FILE": config.PROACTIVE_TRIGGER_LOG_FILE,
    }
    return _load_main_loop(namespace)(bot)


def _bot(config, files=None):
    files = dict(files or {})
    # 只开要测的那一项，其余定时任务和轮询全关。
    quiet = {
        "ENABLE_PROACTIVE": False, "ENABLE_DYNAMIC": False, "ENABLE_PRIVATE_MESSAGES": False,
        "ENABLE_REPLY": False, "ENABLE_BANGUMI": False, "ENABLE_DYNAMIC_WATCH": False,
        "SPECIAL_FOLLOW_ENABLED": False,
    }
    bot = types.SimpleNamespace(
        settings=Settings({"SLEEP_START": 0, "SLEEP_END": 0, "POLL_INTERVAL": 20, **quiet, **config}),
        mood=types.SimpleNamespace(settle=MagicMock()),
        loop_logger=MagicMock(),
        _running=True,
        summaries=types.SimpleNamespace(),
        dynamic_watch=types.SimpleNamespace(),
        personality_evolution=types.SimpleNamespace(maybe_evolve=AsyncMock()),
        memo_review=types.SimpleNamespace(maybe_review=AsyncMock()),
        favorite_digest=types.SimpleNamespace(maybe_run=AsyncMock()),
        dynamic_sources=types.SimpleNamespace(maybe_refresh=AsyncMock()),
        daily_plan=types.SimpleNamespace(_ensure_autonomous_daily_plan=AsyncMock()),
        private_poll=types.SimpleNamespace(_poll_private_messages=AsyncMock()),
        comment_reply=types.SimpleNamespace(_poll_unified=AsyncMock()),
        _load_json=lambda path, default=None: files.get(path, default),
        _save_json=lambda path, value: files.__setitem__(path, value),
        _dynamic_watch_task=None,
    )
    attach_base(bot)
    # 定时活动的触发和日程状态都由日程那几个对象管，用真的（状态表在内存里）。
    attach_schedule(bot)
    bot.schedule_files._save_dynamic_watch_schedule_state = MagicMock()
    # 主循环和夜里、Cookie 两项例行检查用真的 Runner；真去查 Cookie、更新 yt-dlp 的换成假的。
    bot.runner = runner_module.Runner(bot)
    bot.runner.check_and_refresh_cookie = AsyncMock()
    bot.runner.maybe_update_yt_dlp = lambda: None
    return bot


def seed(bot, kind, day, times, triggered=()):
    """往状态表里排好这一天的时刻，``triggered`` 里的记成已经起过。"""
    bot.day_schedules._runs().plan(day, kind, [f"{h}:{m:02d}" for h, m in times])
    for slot in triggered:
        bot.day_schedules._runs().mark_skipped(day, kind, slot, "测试")


def triggered(bot, kind, day):
    return bot.day_schedules._day_schedule(kind, day)[1]


class CookieCheckIntervalTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_is_checked_again_after_the_interval(self):
        bot = _bot({"COOKIE_CHECK_INTERVAL": 6})
        # 每轮之间隔 4 小时，第 1 轮检查后，第 3 轮（距上次 8 小时）应该再检查一次。
        clock = _Clock(bot, start=10 * 3600, step=4 * 3600, rounds=3)
        await _run(bot, clock, real_datetime(2026, 10, 29, 12, 0))
        bot.loop_logger.error.assert_not_called()
        self.assertEqual(bot.runner.check_and_refresh_cookie.await_count, 2)


class SleepPeriodTests(unittest.IsolatedAsyncioTestCase):
    async def test_asleep_runs_night_work_only(self):
        bot = _bot({"SLEEP_START": 1, "SLEEP_END": 7, "ENABLE_REPLY": True, "COOKIE_CHECK_INTERVAL": 6})
        bot._consolidation = MagicMock()
        bot._consolidation.should_run_today.return_value = False
        bot.summaries._maybe_daily_summary = AsyncMock()
        bot.summaries._maybe_weekly_summary = AsyncMock()
        clock = _Clock(bot, start=10 * 3600, step=60, rounds=1)
        await _run(bot, clock, real_datetime(2026, 10, 29, 3, 0))
        bot.loop_logger.error.assert_not_called()
        bot.summaries._maybe_daily_summary.assert_awaited_once()
        bot.summaries._maybe_weekly_summary.assert_awaited_once()
        bot.daily_plan._ensure_autonomous_daily_plan.assert_awaited_once()
        bot.mood.settle.assert_not_called()
        bot.comment_reply._poll_unified.assert_not_awaited()
        bot.runner.check_and_refresh_cookie.assert_not_awaited()


class DynamicWatchDayRolloverTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_day_regenerates_schedule_and_triggers(self):
        bot = _bot(
            {"ENABLE_DYNAMIC_WATCH": True},
            files={config.DYNAMIC_WATCH_SCHEDULE_FILE: {
                "date": "2026-10-28", "dynamic_watch_times": ["10:00"],
                "dynamic_watch_triggered": ["10:00"],
            }},
        )
        # 昨天 10:00 已经跑过；今天同一时刻应该照样触发。
        seed(bot, "dynamic_watch", "2026-10-28", [(10, 0)], {"10:00"})
        bot.schedule_files._generate_dynamic_watch_schedule = MagicMock(return_value=([(10, 0)], set()))
        bot.dynamic_watch.run = AsyncMock()
        clock = _Clock(bot, start=0, step=20, rounds=1)
        await _run(bot, clock, real_datetime(2026, 10, 29, 10, 1))
        await asyncio.sleep(0)
        bot.loop_logger.error.assert_not_called()
        bot.schedule_files._generate_dynamic_watch_schedule.assert_called_once()
        bot.dynamic_watch.run.assert_awaited_once()

    async def test_same_day_keeps_triggered_slots(self):
        bot = _bot(
            {"ENABLE_DYNAMIC_WATCH": True},
            files={config.DYNAMIC_WATCH_SCHEDULE_FILE: {
                "date": "2026-10-29", "dynamic_watch_times": ["10:00"],
                "dynamic_watch_triggered": ["10:00"],
            }},
        )
        seed(bot, "dynamic_watch", "2026-10-29", [(10, 0)], {"10:00"})
        bot.schedule_files._generate_dynamic_watch_schedule = MagicMock()
        bot.dynamic_watch.run = AsyncMock()
        clock = _Clock(bot, start=0, step=20, rounds=1)
        await _run(bot, clock, real_datetime(2026, 10, 29, 10, 1))
        await asyncio.sleep(0)
        bot.loop_logger.error.assert_not_called()
        bot.schedule_files._generate_dynamic_watch_schedule.assert_not_called()
        bot.dynamic_watch.run.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
