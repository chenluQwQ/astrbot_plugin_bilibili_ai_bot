"""日程状态快照：主循环以外那几处怎么改「今天的时刻 / 已起过的时刻」。

日程状态换成 activity_runs 之后照样要过（改了行为的几条写在 CHANGELOG，用例里也注明了）：
启动时记过期、取快照时换天或恢复、WebUI 改日程 / 重新生成 / 保存日程配置。
用固定计划模式，时刻都是写死的，结果可以逐个对。
"""
import json
import tempfile
import types
import unittest
from datetime import datetime as real_datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

# 先导入它：它会装上假的 astrbot 模块（含 astrbot.api.web）。
import test_webui_interest  # noqa: F401
from core import activities, day_schedules, schedule_files
from core import config as core_config
from core.features import daily_plan
from core.lifecycle import schedule_rules
from core.web import _common as web_common, config_page, schedule as web_schedule
from schedule_parts import ScheduleParts

NOW = real_datetime(2026, 10, 29, 10, 1)
FILE_NAMES = (
    "SCHEDULE_FILE", "DYNAMIC_SCHEDULE_FILE", "DYNAMIC_WATCH_SCHEDULE_FILE",
    "BANGUMI_SCHEDULE_FILE", "SPECIAL_FOLLOW_SCHEDULE_FILE", "AUTONOMOUS_PLAN_FILE",
)
# 每种活动：任务属性前缀、日程 json、json 里「已触发」的键。
KINDS = {
    "proactive": ("_proactive", "SCHEDULE_FILE", "proactive_triggered"),
    "dynamic": ("_dynamic", "DYNAMIC_SCHEDULE_FILE", "dynamic_triggered"),
    "dynamic_watch": ("_dynamic_watch", "DYNAMIC_WATCH_SCHEDULE_FILE", "dynamic_watch_triggered"),
    "bangumi": ("_bangumi", "BANGUMI_SCHEDULE_FILE", "bangumi_triggered"),
    "special_follow": ("_special_follow", "SPECIAL_FOLLOW_SCHEDULE_FILE", "special_follow_triggered"),
}


class _FixedDatetime(real_datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class _Config(dict):
    def save_config(self):
        self.saved = True


class Probe(ScheduleParts):
    def __init__(self, runs=None):
        # 传 runs = 同一个 sqlite 状态表重启；不传 = 新的空表（像第一次升级，只能从 json 导入）。
        self.day_schedules._activity_runs = runs
        self.config = _Config({
            "ENABLE_AUTONOMOUS_DAILY_PLAN": False, "SLEEP_START": 2, "SLEEP_END": 7,
            "ENABLE_PROACTIVE": True, "PROACTIVE_TIMES_COUNT": 5,
            "FIXED_PROACTIVE_WINDOWS": [], "FIXED_PROACTIVE_TIMES": ["9:00", "11:00"],
            "AUTONOMOUS_PROACTIVE_WINDOW_MINUTES": 30,
            "ENABLE_DYNAMIC": True, "FIXED_DYNAMIC_TIMES": ["9:10", "11:10"],
            "ENABLE_DYNAMIC_WATCH": True, "FIXED_DYNAMIC_WATCH_TIMES": ["9:20", "11:20"],
            "ENABLE_BANGUMI": True, "BANGUMI_PROACTIVE": True, "FIXED_BANGUMI_TIMES": ["9:30", "11:30"],
            "SPECIAL_FOLLOW_ENABLED": True, "FIXED_SPECIAL_FOLLOW_TIMES": ["9:40", "11:40"],
        })
        self.day_schedules._bangumi_update_checked = False
        self.schedule_files._proactive_windows = []

    def _load_json(self, path, default=None):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _save_json(self, path, value):
        Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = {name: str(Path(tmp.name) / f"{name.lower()}.json") for name in FILE_NAMES}
        # 日程现在经 bot.states 读写，假插件那份按 core.config 的常量找路径
        for name, path in self.paths.items():
            self._patch(core_config, name, path)
        for module in (day_schedules, schedule_files, daily_plan, activities, schedule_rules, web_common, web_schedule, config_page):
            for name, path in self.paths.items():
                if hasattr(module, name):
                    self._patch(module, name, path)
            if hasattr(module, "datetime"):
                self._patch(module, "datetime", _FixedDatetime)
        self.bot = Probe()

    def _patch(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def file(self, kind):
        return self.bot.json_store._load_json(self.paths[KINDS[kind][1]], {})

    def times(self, kind):
        return sorted(f"{h}:{m:02d}" for h, m in self.bot.day_schedules._day_schedule(kind)[0])

    def triggered(self, kind):
        return sorted(self.bot.day_schedules._day_schedule(kind)[1])

    def file_triggered(self, kind):
        return sorted(self.file(kind).get(KINDS[kind][2], []))

    def write_yesterday_files(self):
        """昨天的日程：9 点那一格都做过了。"""
        for kind, (_attr, name, key) in KINDS.items():
            self.bot.json_store._save_json(self.paths[name], {
                "date": "2026-10-28", key.replace("_triggered", "_times"): ["9:00"], key: ["9:00"],
            })

    async def request(self, body):
        for module in (web_schedule, config_page):
            self._patch(module, "request", types.SimpleNamespace(json=AsyncMock(return_value=body)))


class StartupTests(_Base):
    async def test_startup_builds_today_and_marks_past_slots_triggered(self):
        self.bot.day_schedules._mark_overdue_schedule_as_triggered_on_startup()
        expected = {
            "proactive": ["9:00"], "dynamic": ["9:10"], "dynamic_watch": ["9:20"],
            "bangumi": ["9:30"], "special_follow": ["9:40"],
        }
        for kind, slots in expected.items():
            with self.subTest(kind):
                self.assertEqual(len(self.times(kind)), 2)
                self.assertEqual(self.triggered(kind), slots)
                self.assertEqual(self.file_triggered(kind), slots)
                self.assertEqual(self.file(kind)["date"], "2026-10-29")

    async def test_startup_keeps_what_today_already_did(self):
        self.bot.day_schedules._mark_overdue_schedule_as_triggered_on_startup()
        self.bot.day_schedules._skip_slot("special_follow", "11:40", "测试")
        restarted = Probe(self.bot.day_schedules._activity_runs)
        self.bot = restarted
        restarted.day_schedules._mark_overdue_schedule_as_triggered_on_startup()
        self.assertEqual(self.triggered("special_follow"), ["11:40", "9:40"])

    async def test_first_start_after_upgrade_imports_triggered_from_json(self):
        # 旧版本今天已经起过 9:40 和 11:40，记在 json 里；新版本的表是空的。
        self.bot.json_store._save_json(self.paths["SPECIAL_FOLLOW_SCHEDULE_FILE"], {
            "date": "2026-10-29", "special_follow_times": ["9:40", "11:40"],
            "special_follow_triggered": ["9:40", "11:40"],
            "config_fingerprint": self.bot.schedule_files._get_special_follow_config_fingerprint(),
        })
        self.bot.day_schedules._mark_overdue_schedule_as_triggered_on_startup()
        self.assertEqual(self.triggered("special_follow"), ["11:40", "9:40"])
        # 导入只在表里还没有这一天时做一次：之后 json 的已触发不再读。
        self.bot.day_schedules._override_day_schedule("special_follow", [(9, 40), (11, 40)], set())
        self.bot.day_schedules._restore_day_schedule("special_follow")
        self.assertEqual(self.triggered("special_follow"), [])


class SnapshotTests(_Base):
    async def test_snapshot_rolls_yesterday_over(self):
        self.write_yesterday_files()
        snapshot = self.bot.day_schedules._get_schedule_snapshot()
        self.assertEqual(snapshot["date"], "2026-10-29")
        for kind in KINDS:
            with self.subTest(kind):
                # 读快照也会写：换天后日程重新生成并存盘，已触发清空。
                self.assertEqual(self.triggered(kind), [])
                self.assertEqual(self.file(kind)["date"], "2026-10-29")
        self.assertEqual(snapshot["special_follow_times"], ["9:40", "11:40"])
        self.assertEqual(snapshot["special_follow_triggered"], [])

    async def test_snapshot_restores_today_from_file(self):
        self.bot.day_schedules._mark_overdue_schedule_as_triggered_on_startup()
        self.bot = Probe()
        snapshot = self.bot.day_schedules._get_schedule_snapshot()
        self.assertEqual(snapshot["dynamic_triggered"], ["9:10"])
        self.assertEqual(snapshot["bangumi_triggered"], ["9:30"])

    async def test_snapshot_hides_disabled_activities(self):
        self.bot.config["ENABLE_DYNAMIC"] = False
        snapshot = self.bot.day_schedules._get_schedule_snapshot()
        self.assertEqual(snapshot["dynamic_times"], [])
        self.assertEqual(self.times("dynamic"), [])


class WebUIScheduleTests(_Base):
    async def test_override_replaces_times_and_takes_triggered_from_the_page(self):
        self.bot.day_schedules._mark_overdue_schedule_as_triggered_on_startup()
        self.bot.day_schedules._bangumi_update_checked = True
        await self.request({"events": [
            {"kind": "proactive", "time": "9:00", "start_time": "8:45", "end_time": "9:15", "triggered": True},
            {"kind": "dynamic", "time": "12:00"},
            {"kind": "dynamic_watch", "time": "13:00"},
            {"kind": "bangumi", "time": "14:00"},
            {"kind": "follow", "time": "15:00", "triggered": True},
        ]})
        result = await web_schedule.handle_schedule_override(self.bot)
        self.assertNotEqual(result.get("status"), "error", result)
        self.assertEqual(self.times("dynamic"), ["12:00"])
        self.assertEqual(self.triggered("dynamic"), [])
        self.assertEqual(self.triggered("proactive"), ["9:00"])
        # 页面说 15:00 做过了，就记成做过了，哪怕实际没跑。
        self.assertEqual(self.triggered("special_follow"), ["15:00"])
        self.assertEqual(self.file_triggered("special_follow"), ["15:00"])
        # 改一次日程，追番「今天查过更新」就重置；以前只改文件，现在内存也一起（CHANGELOG）。
        self.assertFalse(self.bot.json_store._load_json(self.paths["BANGUMI_SCHEDULE_FILE"])["update_checked"])
        self.assertFalse(self.bot.day_schedules._bangumi_update_checked)
        # 固定计划模式：改动写回配置。
        self.assertEqual(self.bot.config["FIXED_SPECIAL_FOLLOW_TIMES"], ["15:00"])
        self.assertTrue(self.bot.config.saved)

    async def test_regenerate_drops_slots_that_never_ran(self):
        self.bot.day_schedules._mark_overdue_schedule_as_triggered_on_startup()
        self.bot.daily_plan._ensure_autonomous_daily_plan = AsyncMock()
        result = await web_schedule.handle_schedule_regenerate(self.bot)
        self.assertNotEqual(result.get("status"), "error", result)
        self.bot.daily_plan._ensure_autonomous_daily_plan.assert_awaited_once_with(force=True)
        for kind in KINDS:
            with self.subTest(kind):
                self.assertEqual(len(self.times(kind)), 2)
                self.assertEqual(self.triggered(kind), [])

    async def test_regenerate_keeps_what_really_ran(self):
        # 以前重新生成会把今天真跑过的也清掉，还在 5 分钟宽限内的会再跑一次（CHANGELOG）。
        self.bot.day_schedules._mark_overdue_schedule_as_triggered_on_startup()
        runs = self.bot.day_schedules._runs()
        runs.plan("2026-10-29", "dynamic", ["10:00"])
        runs.start("2026-10-29", "dynamic", "10:00")
        self.bot.daily_plan._ensure_autonomous_daily_plan = AsyncMock()
        await web_schedule.handle_schedule_regenerate(self.bot)
        self.assertIn("10:00", self.triggered("dynamic"))
        self.assertIn("10:00", self.times("dynamic"))

    async def test_saving_schedule_config_clears_without_regenerating(self):
        self.bot.day_schedules._mark_overdue_schedule_as_triggered_on_startup()
        await self.request({"FIXED_DYNAMIC_TIMES": ["12:00"]})
        result = await config_page.handle_save_config(self.bot)
        self.assertNotEqual(result.get("status"), "error", result)
        for kind in KINDS:
            with self.subTest(kind):
                self.assertEqual(self.times(kind), [])
                self.assertEqual(self.triggered(kind), [])
                self.assertFalse(Path(self.paths[KINDS[kind][1]]).exists())
        self.assertFalse(self.bot.day_schedules._bangumi_update_checked)

    async def test_saving_unrelated_config_keeps_schedule(self):
        self.bot.day_schedules._mark_overdue_schedule_as_triggered_on_startup()
        await self.request({"POLL_INTERVAL": 30})
        await config_page.handle_save_config(self.bot)
        self.assertEqual(self.triggered("dynamic"), ["9:10"])


if __name__ == "__main__":
    unittest.main()
