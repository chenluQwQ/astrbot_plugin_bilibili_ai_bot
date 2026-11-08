"""Mood is settled once a day when the bot is awake; everything else only reads it."""

import random
import unittest
from datetime import datetime
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

import core.domain.mood as mood_module
from core.domain.mood import CALM, MOODS, Mood


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 28, 9, 0)


class MemoryFile:
    def __init__(self, data=None):
        self.data = data
        self.saves = 0

    def read(self, default=None):
        return dict(self.data) if self.data is not None else default

    def write(self, data):
        self.saves += 1
        self.data = dict(data)


class DailyMoodTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(mood_module, "datetime", FixedDatetime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, enabled=True, data=None):
        file = MemoryFile(data)
        return Mood(file, lambda: enabled), file

    def test_reading_before_settling_is_calm_and_writes_nothing(self):
        mood, file = self.make()
        self.assertEqual(mood.today(), CALM)
        self.assertEqual(file.saves, 0)

    def test_settled_once_per_day_and_then_read(self):
        mood, file = self.make()
        with patch.object(mood_module, "random", random.Random(1)):
            mood.settle()
            first = mood.today()
            mood.settle()
        self.assertEqual(file.saves, 1)
        self.assertIn(first, MOODS)
        self.assertEqual(mood.today(), first)

    def test_yesterday_is_replaced_on_waking(self):
        mood, file = self.make(data={"date": "2026-10-27", "mood": "旧", "mood_prompt": "旧"})
        self.assertEqual(mood.today(), CALM)
        mood.settle()
        self.assertEqual(file.data["date"], "2026-10-28")

    def test_disabled_mood_never_writes(self):
        mood, file = self.make(enabled=False)
        mood.settle()
        self.assertEqual(mood.today(), ("🌙 平静如常", ""))
        self.assertEqual(file.saves, 0)

    def test_server_sample_is_read_as_is(self):
        mood, _ = self.make(data={"date": "2026-10-28", "mood": "😏 有点皮", "mood_prompt": "偶尔多一点调侃。"})
        self.assertEqual(mood.today(), ("😏 有点皮", "偶尔多一点调侃。"))


if __name__ == "__main__":
    unittest.main()
