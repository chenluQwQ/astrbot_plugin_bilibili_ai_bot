"""番剧从第一季看起（B3）：该补哪季、看完觉得好看就记备忘、到点直接按 season_id 去看。"""
import time
import types
import unittest
from unittest.mock import AsyncMock

from test_autonomous_ranges import ScheduleProbe  # noqa: F401  先装上假的 astrbot
from core.domain.bangumi_series import main_seasons, season_to_backfill
from core.domain.memos import Memos
from core.features.memo_watch import MemoWatch

SERIES = [
    {"season_id": 1, "title": "第一季"},
    {"season_id": 9, "title": "剧场版 无限列车篇"},
    {"season_id": 2, "title": "第二季"},
    {"season_id": 7, "title": "OVA"},
    {"season_id": 3, "title": "第三季"},
]


def backfill(current, started=(), completed=()):
    found = season_to_backfill(SERIES, current, started=set(started), completed=set(completed))
    return found and found["season_id"]


class SeriesRuleTests(unittest.TestCase):
    def test_side_stories_are_not_in_the_order(self):
        self.assertEqual([s["season_id"] for s in main_seasons(SERIES)], [1, 2, 3])
        self.assertEqual([s["season_id"] for s in main_seasons([{"season_id": 5, "title": "SPY×FAMILY"}])], [5])

    def test_starting_late_points_back_to_the_first_season(self):
        self.assertEqual(backfill(3), 1)

    def test_walks_forward_until_caught_up(self):
        self.assertEqual(backfill(3, started={1, 3}, completed={1}), 2)
        self.assertIsNone(backfill(3, started={1, 2, 3}, completed={1, 2}))

    def test_current_unfinished_earlier_season_just_continues(self):
        self.assertIsNone(backfill(1, started={1, 3}))

    def test_first_season_or_unknown_needs_nothing(self):
        self.assertIsNone(backfill(1))
        self.assertIsNone(backfill(9))  # 剧场版不在正篇顺序里
        self.assertIsNone(season_to_backfill([], 3, started=set(), completed=set()))


class RememberEarlierSeasonTests(unittest.TestCase):
    def make(self, memory, enabled=True):
        from core.features.bangumi import Bangumi
        store = Memos.in_memory()
        host = types.SimpleNamespace(
            memo_tool=types.SimpleNamespace(enabled=lambda: enabled, _memos=lambda: store),
            bangumi_memory=types.SimpleNamespace(all=lambda: memory),
        )
        return Bangumi(host), store

    def test_liked_third_season_records_the_first(self):
        bangumi, store = self.make({"3": {"episodes": [{"ep_id": 30}]}})
        bangumi._remember_earlier_season({"title": "第三季", "seasons": SERIES}, 3)
        bangumi._remember_earlier_season({"title": "第三季", "seasons": SERIES}, 3)  # 同一季只记一条
        memos = store.all()
        self.assertEqual(len(memos), 1)
        self.assertEqual((memos[0]["kind"], memos[0]["source"], memos[0]["target"]), ("bangumi", "self", "第一季 ss1"))
        self.assertTrue(memos[0]["due_date"])

    def test_off_or_nothing_to_backfill_records_nothing(self):
        bangumi, store = self.make({}, enabled=False)
        bangumi._remember_earlier_season({"title": "第三季", "seasons": SERIES}, 3)
        bangumi2, store2 = self.make({})
        bangumi2._remember_earlier_season({"title": "第一季", "seasons": SERIES}, 1)
        self.assertEqual(store.all() + store2.all(), [])


class SeasonIdInMemoTests(unittest.IsolatedAsyncioTestCase):
    async def test_self_recorded_memo_skips_the_title_search(self):
        store = Memos.in_memory()
        memo_id, _ = store.add(kind="bangumi", target="第一季 ss1", source="self", now=time.time())
        search = AsyncMock(return_value=[{"season_id": 999}])
        host = types.SimpleNamespace(memo_tool=types.SimpleNamespace(enabled=lambda: True, _memos=lambda: store),
                                     bili_bangumi=types.SimpleNamespace(search_bilibili_bangumi=search))
        self.assertEqual(await MemoWatch(host).bangumi_season(memo_id), 1)
        search.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
