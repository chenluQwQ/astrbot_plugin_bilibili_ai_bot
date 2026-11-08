"""每日计划里「这段看什么」（B2b）：重点的规矩，以及整份计划生成时备忘被排上。"""
import json
import time
import types
import unittest
from activity_parts import ListLog
from datetime import datetime

from test_autonomous_ranges import ScheduleProbe  # noqa: F401  先装上假的 astrbot
from base_parts import BaseParts
from parts import part
from people_fakes import LegacyMood
from schedule_parts import schedule_clock
from core.domain.day_focus import check_bangumi_memo, check_window_focus, leave_one_unfocused
from core.domain.memos import Memos

MEMOS = {1: {"kind": "video"}, 2: {"kind": "bangumi"}, 3: {"kind": "up"}, 4: {"kind": "other"}}
POOLS = ["热门", "排行榜:游戏"]


def check(raw, used=None, count=None, cap=3):
    return check_window_focus(raw, memos=MEMOS, pools=POOLS, used=set() if used is None else used,
                              memo_count=[0] if count is None else count, memo_cap=cap)


class FocusRuleTests(unittest.TestCase):
    def test_each_kind_when_valid(self):
        self.assertEqual(check({"focus": "memo", "detail": 1}), {"focus": "memo", "detail": 1})
        self.assertEqual(check({"focus": "memo", "detail": 3}), {"focus": "memo", "detail": 3})
        self.assertEqual(check({"focus": "follow", "detail": "随便"}), {"focus": "follow", "detail": ""})
        self.assertEqual(check({"focus": "topic", "detail": " 手绘动画 "}), {"focus": "topic", "detail": "手绘动画"})
        self.assertEqual(check({"focus": "pool", "detail": "排行榜:游戏"}), {"focus": "pool", "detail": "排行榜:游戏"})

    def test_invalid_focus_is_dropped(self):
        for raw in ("10:00-11:00", {"focus": "memo", "detail": 2}, {"focus": "memo", "detail": 4},
                    {"focus": "memo", "detail": 9}, {"focus": "memo", "detail": "1"}, {"focus": "memo", "detail": True},
                    {"focus": "topic", "detail": ""}, {"focus": "topic", "detail": "长" * 21},
                    {"focus": "pool", "detail": "我编的池子"}, {"focus": "music"}):
            with self.subTest(raw=raw):
                self.assertIsNone(check(raw))

    def test_same_focus_only_once_a_day(self):
        used, count = set(), [0]
        self.assertIsNotNone(check({"focus": "follow"}, used, count))
        self.assertIsNone(check({"focus": "follow"}, used, count))
        self.assertIsNotNone(check({"focus": "topic", "detail": "手绘"}, used, count))
        self.assertIsNone(check({"focus": "topic", "detail": "手 绘"}, used, count))
        self.assertIsNotNone(check({"focus": "topic", "detail": "猫"}, used, count))  # 别的话题可以
        self.assertIsNotNone(check({"focus": "memo", "detail": 1}, used, count))
        self.assertIsNone(check({"focus": "memo", "detail": 1}, used, count))

    def test_memo_cap_counts_windows_and_bangumi(self):
        used, count = set(), [0]
        self.assertIsNotNone(check({"focus": "memo", "detail": 1}, used, count, cap=2))
        self.assertEqual(check_bangumi_memo(2, memos=MEMOS, used=used, memo_count=count, memo_cap=2), 2)
        self.assertIsNone(check({"focus": "memo", "detail": 3}, used, count, cap=2))
        self.assertIsNone(check_bangumi_memo(1, memos=MEMOS, used=set(), memo_count=[0], memo_cap=3))  # 不是番剧

    def test_leaves_one_window_unfocused(self):
        memo, follow, topic = {"focus": "memo", "detail": 1}, {"focus": "follow"}, {"focus": "topic", "detail": "猫"}
        self.assertEqual(leave_one_unfocused([memo, follow, topic]), [memo, follow, None])
        self.assertEqual(leave_one_unfocused([topic, memo]), [None, memo])
        self.assertEqual(leave_one_unfocused([memo, {"focus": "memo", "detail": 3}]), [memo, None])
        self.assertEqual(leave_one_unfocused([memo, None]), [memo, None])
        self.assertEqual(leave_one_unfocused([topic]), [topic])  # 只有一段就随它


PLAN = {
    "proactive_windows": [
        {"window": "10:00-11:30", "focus": "memo", "detail": 1},
        {"window": "14:00-15:30", "focus": "follow", "detail": ""},
        {"window": "19:00-20:30", "focus": "topic", "detail": "手绘"},
    ],
    "bangumi_times": [{"time": "21:30", "memo_id": 2}],
    "rationale": "测试",
}


class PlanProbe(ScheduleProbe, BaseParts):
    mood = part(LegacyMood)

    def __init__(self, reply, **config):
        super().__init__({
            "ENABLE_AUTONOMOUS_DAILY_PLAN": True, "AUTONOMOUS_ACTIVITY_LEVEL": 100,
            "AUTONOMOUS_PROACTIVE_DAILY_MIN": 0, "AUTONOMOUS_PROACTIVE_DAILY_MAX": 4,
            "AUTONOMOUS_PROACTIVE_DAILY_LIMIT": 4, "PROACTIVE_TIMES_COUNT": 3, "ENABLE_PROACTIVE": True,
            "ENABLE_DYNAMIC": False, "ENABLE_BANGUMI": True, "BANGUMI_PROACTIVE": True, "BANGUMI_DAILY_LIMIT": 1,
            "SPECIAL_FOLLOW_ENABLED": False, "ENABLE_DYNAMIC_WATCH": False, "ENABLE_REPLY": False,
            "ENABLE_PRIVATE_MESSAGES": False, "SLEEP_START": 2, "SLEEP_END": 8,
            "AUTONOMOUS_MIN_ACTION_GAP_MINUTES": 45, "AUTONOMOUS_PROACTIVE_WINDOW_MINUTES": 90,
            "PROACTIVE_VIDEO_POOLS": POOLS, **config,
        })
        self.saved, self.prompts, self.reply = {}, [], reply
        self.store = Memos.in_memory()
        self.memo_tool = types.SimpleNamespace(enabled=lambda: True, _memos=lambda: self.store)

    def _load_json(self, _path, default=None):
        return self.saved or default

    def _save_json(self, _path, value):
        self.saved = value

    def _get_today_mood(self):
        return "平静", ""

    async def _get_system_prompt(self):
        return "测试人设"

    async def _llm_call(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        return self.reply


class Morning(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 8, 20, 8, 5, 0)


class PlanWithFocusTests(unittest.IsolatedAsyncioTestCase):
    async def generate(self, probe):
        with schedule_clock(Morning):
            return await probe.daily_plan._ensure_autonomous_daily_plan(force=True)

    def memos(self, probe):
        now = time.time()  # 过期按真实时间算，计划的假时钟不影响
        video, _ = probe.store.add(kind="video", target="BV1Ab411c7Xy", note="很好笑", source="comment",
                                   from_uid="11", from_name="甲", now=now)
        bangumi, _ = probe.store.add(kind="bangumi", target="葬送的芙莉莲", source="owner", from_uid="777",
                                     unlimited=True, now=now)
        return video, bangumi

    async def test_windows_get_focus_and_memos_are_scheduled(self):
        probe = PlanProbe(json.dumps(PLAN, ensure_ascii=False))
        video, bangumi = self.memos(probe)
        plan = await self.generate(probe)
        focuses = [window.get("focus") for window in plan["proactive_windows"]]
        self.assertEqual(focuses, [{"focus": "memo", "detail": video}, {"focus": "follow", "detail": ""}, None])
        self.assertEqual(plan["bangumi_memos"], {plan["bangumi_times"][0]: bangumi})
        self.assertEqual({item["state"] for item in probe.store.all()}, {"scheduled"})
        prompt = probe.prompts[0]
        self.assertIn("葬送的芙莉莲", prompt)
        self.assertIn('trust="untrusted"', prompt)
        self.assertIn("热门、排行榜:游戏", prompt)

    async def test_regenerating_today_puts_unused_memos_back(self):
        probe = PlanProbe(json.dumps(PLAN, ensure_ascii=False))
        self.memos(probe)
        await self.generate(probe)
        probe.reply = '{"proactive_windows":["10:00-11:30"],"rationale":"改主意了"}'
        plan = await self.generate(probe)
        self.assertEqual(plan["bangumi_memos"], {})
        self.assertEqual({item["state"] for item in probe.store.all()}, {"pending"})

    async def test_plain_windows_and_no_memos_still_work(self):
        probe = PlanProbe('{"proactive_windows":["10:00-11:30","19:00-20:30"],"rationale":"测试"}')
        plan = await self.generate(probe)
        self.assertEqual(len(plan["proactive_windows"]), 2)
        self.assertFalse(any("focus" in window for window in plan["proactive_windows"]))
        self.assertNotIn("memo_list", probe.prompts[0])

    async def test_memo_cap_zero_schedules_none(self):
        probe = PlanProbe(json.dumps(PLAN, ensure_ascii=False), MEMO_DAILY_TASKS=0)
        self.memos(probe)
        plan = await self.generate(probe)
        self.assertEqual(plan["bangumi_memos"], {})
        self.assertEqual({item["state"] for item in probe.store.all()}, {"pending"})


if __name__ == "__main__":
    unittest.main()


# ── B2c：到点按重点去看 ──
import asyncio  # noqa: E402
import random  # noqa: E402

import test_proactive_flow_snapshot as flow  # noqa: E402
from proactive_modules import patched_in_proactive_modules  # noqa: E402
from core.features.memo_watch import MemoWatch  # noqa: E402


async def run_round(focus, setup=None):
    probe, coroutine = flow.regular_round()
    coroutine.close()
    probe.store = Memos.in_memory()
    probe.memo_tool = types.SimpleNamespace(enabled=lambda: True, _memos=lambda: probe.store)
    probe.memo_watch = MemoWatch(probe)
    if setup:
        setup(probe)

    async def sleep(_seconds):
        pass

    fake_asyncio = types.SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError)
    with patched_in_proactive_modules(datetime=flow.FixedDatetime, random=random.Random(1), asyncio=fake_asyncio):
        await probe.proactive_browse._run_proactive_inner(focus=focus(probe) if callable(focus) else focus)
    watched = [call[1] for call in probe.calls if call[0] == "action" and call[2] == "proactive_watch"]
    return probe, watched


def calls(probe, name):
    return [call[1:] for call in probe.calls if call[0] == name]


class FocusRoundTests(unittest.IsolatedAsyncioTestCase):
    async def test_topic_focus_searches_that_topic_first(self):
        probe, watched = await run_round({"focus": "topic", "detail": "猫"})
        self.assertEqual(calls(probe, "get_search_videos")[0][0], ["猫"])
        self.assertEqual(calls(probe, "decide_search_queries"), [])  # 不再让模型另想搜索词
        self.assertEqual(watched[0], "proactive_watch:BV_SEARCH")

    async def test_pool_focus_starts_with_the_pool(self):
        _probe, watched = await run_round({"focus": "pool", "detail": "热门"})
        self.assertEqual(watched[0], "proactive_watch:BV_POOL")

    async def test_follow_focus_starts_with_followed_ups(self):
        _probe, watched = await run_round({"focus": "follow", "detail": ""})
        self.assertEqual(watched[0], "proactive_watch:BV_FOLLOW")

    async def test_memo_video_goes_first_and_the_memo_is_done(self):
        def setup(probe):
            probe.oids["BV1Ab411c7Xy"] = 77
            probe.evaluations["BV_INFO_77"] = flow.evaluation(7.5)
            probe.memo_id, _ = probe.store.add(kind="video", target="BV1Ab411c7Xy", source="comment",
                                               from_uid="11", now=time.time())

        probe, watched = await run_round(lambda p: {"focus": "memo", "detail": p.memo_id}, setup)
        self.assertEqual(watched[0], "proactive_watch:BV_INFO_77")
        memo = probe.store.get(probe.memo_id)
        self.assertEqual(memo["state"], "done")
        self.assertIn("接口标题77", memo["result"])

    async def test_memo_stays_open_when_the_evaluation_fails(self):
        def setup(probe):
            probe.oids["BV1Ab411c7Xy"] = 77  # 没配评价：评价失败，这个视频不算看完
            probe.memo_id, _ = probe.store.add(kind="video", target="BV1Ab411c7Xy", source="comment",
                                               from_uid="11", now=time.time())

        probe, _watched = await run_round(lambda p: {"focus": "memo", "detail": p.memo_id}, setup)
        self.assertEqual(probe.store.get(probe.memo_id)["state"], "pending")

    async def test_memo_already_watched_is_done_without_watching(self):
        def setup(probe):
            probe.memo_id, _ = probe.store.add(kind="video", target="BV1Ab411c7Xy", source="comment",
                                               from_uid="11", now=time.time())
            probe.watched.add("BV1Ab411c7Xy")

        probe, watched = await run_round(lambda p: {"focus": "memo", "detail": p.memo_id}, setup)
        self.assertNotIn("proactive_watch:BV_INFO_77", watched)
        self.assertEqual(probe.store.get(probe.memo_id)["state"], "done")

    async def test_no_focus_is_the_old_round(self):
        _probe, watched = await run_round(None)
        _old, old_watched = await run_round({})
        self.assertEqual(watched, old_watched)


class SlotFocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_slot_passes_the_focus_to_the_entry(self):
        from core.activities import Activities
        received = []

        async def run_proactive(**kwargs):
            received.append(kwargs)

        host = types.SimpleNamespace(
            proactive_browse=types.SimpleNamespace(_run_proactive=run_proactive),
            day_schedules=types.SimpleNamespace(_save_day_schedule=lambda *a, **k: None),
            activity_logs=types.SimpleNamespace(trigger=ListLog()),
            daily_plan=types.SimpleNamespace(slot_focus=lambda kind, slot: {"focus": {"focus": "follow", "detail": ""}}
                                             if slot == "10:45" else {}),
        )
        activities = Activities(host)
        await activities._run_scheduled_slot("proactive", "2026-11-06", "10:45")
        await activities._run_scheduled_slot("proactive", "2026-11-06", "15:00")
        host.daily_plan.slot_focus = lambda kind, slot: 1 / 0  # 读重点出错也照常跑
        await activities._run_scheduled_slot("proactive", "2026-11-06", "18:00")
        self.assertEqual(received, [{"focus": {"focus": "follow", "detail": ""}}, {}, {}])


class BangumiMemoTests(unittest.IsolatedAsyncioTestCase):
    def make(self, found):
        from unittest.mock import AsyncMock
        from core.features.bangumi import Bangumi
        store = Memos.in_memory()
        memo_id, _ = store.add(kind="bangumi", target="葬送的芙莉莲", source="owner", from_uid="777",
                               unlimited=True, now=time.time())
        host = types.SimpleNamespace(
            now_doing=types.SimpleNamespace(report=lambda *a, **k: None, finish=lambda *a: None),
            memo_tool=types.SimpleNamespace(enabled=lambda: True, _memos=lambda: store),
            bili_bangumi=types.SimpleNamespace(search_bilibili_bangumi=AsyncMock(
                return_value=[{"season_id": 4321}] if found else [])),
        )
        host.memo_watch = MemoWatch(host)
        bangumi = Bangumi(host)
        bangumi._watch_season = AsyncMock()
        return bangumi, store, memo_id

    async def test_found_season_is_watched_and_done(self):
        bangumi, store, memo_id = self.make(found=True)
        await bangumi.run(memo_id=memo_id)
        self.assertEqual(bangumi._watch_season.await_args.kwargs["season_id"], 4321)
        self.assertEqual(store.get(memo_id)["state"], "done")

    async def test_not_found_picks_as_usual_and_keeps_the_memo(self):
        bangumi, store, memo_id = self.make(found=False)
        await bangumi.run(memo_id=memo_id)
        self.assertIsNone(bangumi._watch_season.await_args.kwargs["season_id"])
        self.assertEqual(store.get(memo_id)["state"], "pending")
