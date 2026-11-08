"""主动看片各块的快照：视频池、口味报告、选片。整轮流程在 ``test_proactive_flow_snapshot``。

假的只有 B站、模型和文件；都从插件对象的方法调用，搬代码时这里不用改。

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_proactive_pieces_snapshot.py
"""
import copy
import json
import os
import random
import re
import types
import unittest
from parts import part
from schedule_parts import ScheduleParts
from people_fakes import LegacyMood
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from proactive_modules import patched_in_proactive_modules

from core.config import PREFERENCE_STATE_FILE, WATCH_LOG_FILE
from proactive_parts import ProactiveParts
from core.settings import Settings
from itself import itself

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "proactive_pieces.json"
NOW = datetime.now()


def stamp(days=0):
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")


def raw_video(bvid, view=20000, pubdate=2_000_000_000):
    return {"bvid": bvid, "title": f"视频{bvid}", "desc": "简介", "owner": {"name": "UP", "mid": 3}, "pubdate": pubdate,
            "pic": "p", "stat": {"view": view}, "tname": "动画", "play": view, "author": "UP", "mid": 3,
            "aid": 1, "duration": 60, "typename": "动画", "create": "2020-01-01 00:00", "goto": "av",
            "id": 1, "uri": "", "cid": 2, "rcmd_reason": {}}


def payload(url):
    if "popular/series" in url or "popular/precious" in url:
        return {"code": 0, "data": {"list": [raw_video("BVw"), raw_video("BVlow", view=1)]}}
    if "ranking" in url:
        return {"code": 0, "data": {"list": [raw_video("BVr")]}}
    if "popular" in url:
        return {"code": 0, "data": {"list": [raw_video("BVh"), raw_video("BVlow", view=1), raw_video("BVold", pubdate=1)]}}
    if "newlist" in url:
        return {"code": 0, "data": {"archives": [raw_video("BVn"), raw_video("BVlow", view=1)]}}
    if "rcmd" in url:
        return {"code": 0, "data": {"item": [raw_video("BVc")]}}
    return {"code": 0, "data": {}}


class Probe(ProactiveParts):
    activities = ScheduleParts.activities
    models = itself

    def __init__(self, config=None, files=None, reply=None, http="ok", search=None):
        # B5c 起默认只有约三成的日子按喜好搜；快照照原来天天按喜好录的，这里钉成 1
        self.config = {"PROACTIVE_PREFERENCE_DAY_CHANCE": 1, **(config or {})}
        self.settings = Settings(self.config)
        self.files = copy.deepcopy(files or {})
        self.reply = reply
        self.http = http
        self.search = search or {}
        self.calls = []

    def _load_json(self, path, default=None):
        return copy.deepcopy(self.files.get(path, default))

    def _save_json(self, path, value):
        self.calls.append(["save", os.path.basename(path)])
        self.files[path] = copy.deepcopy(value)

    mood = part(LegacyMood)

    def _get_today_mood(self):
        return "开心", "天气好"

    def _has_cookie(self):
        return True

    def _activity_busy(self, _kind):
        return False

    def _run_activity_now(self, kind, **kwargs):
        self.calls.append(["run_now", kind, kwargs])
        return object()

    async def _http_get(self, url, params=None, **_kwargs):
        self.calls.append(["GET", url, params])
        if self.http == "raises":
            raise RuntimeError("断网")
        if self.http == "fail":
            return {"code": -352, "message": "风控"}, None
        return payload(url), None

    async def search_bilibili_videos(self, keyword, ps=20, **_kwargs):
        self.calls.append(["search", keyword, ps])
        result = self.search.get(keyword, [])
        if isinstance(result, Exception):
            raise result
        return result

    async def _llm_call(self, prompt, **kwargs):
        self.calls.append(["model", prompt, kwargs.get("max_tokens")])
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    async def _generation_llm_call(self, prompt, **kwargs):
        return await self._llm_call(prompt, **kwargs)

    async def _summary_llm_call(self, prompt, **kwargs):
        return await self._llm_call(prompt, **kwargs)


TASTE_LOG = [
    {"time": stamp(1), "title": "木工抽屉", "score": 9, "up_name": "工匠", "up_mid": "42", "tname": "手工", "bvid": "BV1",
     "source": "search", "source_detail": "木工", "review": "榫头好看", "mood": "开心", "search_keywords": ["榫卯"],
     "preference_signals": [{"type": "theme", "value": "木工", "polarity": "like", "strength": 0.8, "evidence": "榫头"}]},
    {"time": stamp(2.5), "title": "无聊开箱", "score": 3, "up_name": "甲", "up_mid": "43", "tname": "数码", "bvid": "BV2",
     "source": "pool", "review": "一般", "mood": "无聊",
     "preference_signals": [{"type": "theme", "value": "开箱", "polarity": "dislike", "strength": 0.6, "evidence": "太长"}]},
    {"time": stamp(3.5), "title": "单机游戏", "score": 8, "up_name": "乙", "up_mid": "44", "tname": "单机游戏", "bvid": "BV3",
     "source": "follow", "review": "好玩", "mood": "开心"},
    {"time": stamp(90), "title": "太旧", "score": 10, "tname": "音乐", "bvid": "BV4"},
    {"time": stamp(1), "title": "评价失败", "score": 0, "evaluation_status": "failed", "bvid": "BV5"},
]
TASTE_FILES = {WATCH_LOG_FILE: TASTE_LOG}
GOOD_QUERIES = '{"queries":["猫咪 日常","木工 榫卯"]}'


def _normalize(value):
    text = json.dumps(value, ensure_ascii=False, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else f"<{type(o).__name__}>")
    text = re.sub(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?", "<TIME>", text)
    return json.loads(text)


PIECES = {
    # 视频池
    "pool/default": lambda: (dict(), lambda p: p.video_pools._get_pool_videos(1000)),
    "pool/all": lambda: (dict(config={"PROACTIVE_VIDEO_POOLS": ["热门", "每周必看", "入站必刷", "推荐", "排行榜:游戏", "排行榜", "最新:单机游戏", "newlist:17", "最新", "没这个"]}), lambda p: p.video_pools._get_pool_videos(1000)),
    "pool/api_fails": lambda: (dict(http="fail", config={"PROACTIVE_VIDEO_POOLS": ["热门", "每周必看", "入站必刷", "推荐", "排行榜", "newlist:17"]}), lambda p: p.video_pools._get_pool_videos()),
    "pool/api_raises": lambda: (dict(http="raises", config={"PROACTIVE_VIDEO_POOLS": ["热门", "每周必看", "入站必刷", "推荐", "排行榜", "newlist:17"]}), lambda p: p.video_pools._get_pool_videos()),
    # 搜索候选
    "search/empty": lambda: (dict(), lambda p: p.video_discovery._get_proactive_search_videos([], 3)),
    "search/round_robin": lambda: (dict(search={"甲": [{"bvid": f"A{i}", "title": "a"} for i in range(4)], "乙": [{"bvid": f"B{i}", "title": "b", "author": "作者", "mid": 9, "play": 7} for i in range(4)]}), lambda p: p.video_discovery._get_proactive_search_videos(["甲", "乙", "甲"], 5, watched_bvids={"A0"})),
    "search/one_fails": lambda: (dict(search={"甲": RuntimeError("坏"), "手工 木工制作": [{"bvid": "C"}, {"bvid": ""}]}), lambda p: p.video_discovery._get_proactive_search_videos(["甲", "手工 木工制作"], 2)),
    # 搜索词
    "queries/model_ok": lambda: (dict(reply=GOOD_QUERIES, files=TASTE_FILES), lambda p: p.video_discovery._decide_proactive_search_queries()),
    "queries/passed_history": lambda: (dict(reply=GOOD_QUERIES), lambda p: p.video_discovery._decide_proactive_search_queries(TASTE_LOG)),
    "queries/lines_reply": lambda: (dict(reply="猫咪\n木工", files=TASTE_FILES), lambda p: p.video_discovery._decide_proactive_search_queries()),
    "queries/bad_reply": lambda: (dict(reply="{坏", files=TASTE_FILES), lambda p: p.video_discovery._decide_proactive_search_queries()),
    "queries/no_reply": lambda: (dict(reply=None, files=TASTE_FILES), lambda p: p.video_discovery._decide_proactive_search_queries()),
    "queries/model_raises": lambda: (dict(reply=RuntimeError("炸"), files=TASTE_FILES), lambda p: p.video_discovery._decide_proactive_search_queries()),
    "queries/no_history": lambda: (dict(reply=None), lambda p: p.video_discovery._decide_proactive_search_queries([])),
    "queries/custom_prompt": lambda: (dict(reply=GOOD_QUERIES, config={"PROACTIVE_SEARCH_QUERY_PROMPT": "只搜猫"}), lambda p: p.video_discovery._decide_proactive_search_queries([])),
    # 聊天里顺口叫它去看
    "trigger/unrelated_text": lambda: (dict(reply="yes"), lambda p: p.proactive_browse._should_trigger_proactive_from_text("今天天气不错")),
    "trigger/direct": lambda: (dict(reply="no"), lambda p: p.proactive_browse._should_trigger_proactive_from_text("你去刷B站视频")),
    "trigger/model_yes": lambda: (dict(reply="yes"), lambda p: p.proactive_browse._should_trigger_proactive_from_text("B站有什么好看的")),
    "trigger/model_no": lambda: (dict(reply="no"), lambda p: p.proactive_browse._should_trigger_proactive_from_text("B站有什么好看的")),
    "trigger/off": lambda: (dict(reply="yes"), lambda p: p.proactive_browse._maybe_trigger_proactive_from_llm(types.SimpleNamespace(message_str="去B站看看视频吧", get_sender_name=lambda: "柠"), types.SimpleNamespace(system_prompt=""))),
    "trigger/starts": lambda: (dict(reply="yes", config={"ENABLE_PROACTIVE": True}), lambda p: p.proactive_browse._maybe_trigger_proactive_from_llm(types.SimpleNamespace(message_str="去B站看看视频吧", get_sender_name=lambda: "柠"), types.SimpleNamespace(system_prompt=""))),
    # 看前预筛
    "prefilter/off": lambda: (dict(reply='{"watch":false}'), lambda p: p.video_discovery._should_watch_video_before_download({"bvid": "B", "title": "t", "_source": "search"}, [17], 0, 3)),
    "prefilter/follow_passes": lambda: (dict(reply="NO", config={"ENABLE_PROACTIVE_LLM_PREFILTER": True}), lambda p: p.video_discovery._should_watch_video_before_download({"bvid": "B", "title": "t", "_source": "follow"}, [17], 0, 3)),
    "prefilter/preferred_zone_passes": lambda: (dict(reply="NO", config={"ENABLE_PROACTIVE_LLM_PREFILTER": True, "PROACTIVE_EXPLORATION_RATIO": 0}), lambda p: p.video_discovery._should_watch_video_before_download({"bvid": "B", "title": "t", "_source": "search", "tname": "单机游戏"}, [17], 0, 3)),
    "prefilter/exploration_passes": lambda: (dict(reply="NO", config={"ENABLE_PROACTIVE_LLM_PREFILTER": True, "PROACTIVE_EXPLORATION_RATIO": 1}), lambda p: p.video_discovery._should_watch_video_before_download({"bvid": "B", "title": "t", "_source": "search"}, [17], 0, 3)),
    "prefilter/rejects": lambda: (dict(reply="NO", config={"ENABLE_PROACTIVE_LLM_PREFILTER": True, "PROACTIVE_EXPLORATION_RATIO": 0}), lambda p: p.video_discovery._should_watch_video_before_download({"bvid": "B", "title": "t", "_source": "search", "tname": "数码"}, [17], 0, 3)),
    "prefilter/accepts": lambda: (dict(reply="YES", config={"ENABLE_PROACTIVE_LLM_PREFILTER": True, "PROACTIVE_EXPLORATION_RATIO": 0}), lambda p: p.video_discovery._should_watch_video_before_download({"bvid": "B", "title": "t", "_source": "search", "tname": "数码"}, [17], 0, 3)),
    "prefilter/reject_budget_used": lambda: (dict(reply="NO", config={"ENABLE_PROACTIVE_LLM_PREFILTER": True, "PROACTIVE_EXPLORATION_RATIO": 0}), lambda p: p.video_discovery._should_watch_video_before_download({"bvid": "B", "title": "t", "_source": "search", "tname": "数码"}, [17], 3, 3)),
    "prefilter/model_raises": lambda: (dict(reply=RuntimeError("炸"), config={"ENABLE_PROACTIVE_LLM_PREFILTER": True, "PROACTIVE_EXPLORATION_RATIO": 0}), lambda p: p.video_discovery._should_watch_video_before_download({"bvid": "B", "title": "t", "_source": "search", "tname": "数码"}, [17], 0, 3)),
}
SYNC_PIECES = {
    "zones/format_config": lambda: (dict(config={"PROACTIVE_VIDEO_POOLS": ["热门", "排行榜:游戏", "最新:单机游戏,17", "没这个:1"]}), lambda p: p.video_pools._format_video_pool_config()),
    "zones/format_default": lambda: (dict(), lambda p: p.video_pools._format_video_pool_config()),
    "taste/empty_summary": lambda: (dict(), lambda p: p.taste._format_recent_taste_summary()),
    "taste/summary": lambda: (dict(files=TASTE_FILES), lambda p: p.taste._format_recent_taste_summary()),
    "taste/preferences_empty": lambda: (dict(), lambda p: p.taste._format_recent_preference_summary()),
    "taste/preferences": lambda: (dict(files=TASTE_FILES), lambda p: p.taste._format_recent_preference_summary()),
    "taste/report_empty": lambda: (dict(), lambda p: p.taste._format_interest_report()),
    "taste/report": lambda: (dict(files=TASTE_FILES), lambda p: p.taste._format_interest_report()),
    "taste/tids_empty": lambda: (dict(), lambda p: p.taste._get_taste_tids()),
    "taste/tids": lambda: (dict(files=TASTE_FILES), lambda p: p.taste._get_taste_tids()),
    "taste/window": lambda: (dict(config={"PROACTIVE_TASTE_WINDOW_DAYS": 3}, files=TASTE_FILES), lambda p: [p.taste._taste_window_days(), p.taste._format_recent_taste_summary()]),
    "taste/lifecycle": lambda: (dict(files=TASTE_FILES), lambda p: p.preferences.cached()),
    "taste/lifecycle_state": lambda: (dict(files={**TASTE_FILES, PREFERENCE_STATE_FILE: {"current": [{"signal_type": "theme", "value": "木工", "polarity": "like", "stage": "stable", "evidence_count": 5, "active_weeks": 3, "score": 2.5}, {"signal_type": "up", "value": "工匠", "polarity": "dislike", "stage": "recent", "evidence_count": 2, "active_weeks": 1}]}}), lambda p: [p.preferences.cached(), p.taste._format_recent_preference_summary(), p.taste._format_interest_report(), p.video_discovery._fallback_proactive_search_queries(), p.taste._get_taste_tids()]),
    "taste/tids_qualified": lambda: (dict(files={WATCH_LOG_FILE: [{"time": stamp(1), "title": f"t{i}", "score": 9, "tname": "单机游戏", "up_name": "乙", "bvid": f"T{i}"} for i in range(3)] + [{"time": stamp(1), "title": f"u{i}", "score": 8, "tname": "手工", "bvid": f"U{i}"} for i in range(2)] + [{"time": stamp(1), "title": "低", "score": 2, "tname": "数码", "bvid": "L"}]}), lambda p: [p.taste._get_taste_tids(), p.taste._get_taste_tids(min_score=9, min_count=3), p.taste._format_interest_report()]),
    "queries/fallback": lambda: (dict(files=TASTE_FILES), lambda p: p.video_discovery._fallback_proactive_search_queries()),
    "comments/today_count": lambda: (dict(), lambda p: p.watch_video._today_proactive_comment_count([{"time": stamp(), "comment": "x"}, {"time": stamp(1), "comment": "y"}], [{"time": stamp(), "comment": "z"}])),
}


class ProactivePiecesSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def _scene(self, build, is_async):
        kwargs, call = build()
        probe = Probe(**kwargs)

        async def no_wait(_seconds):
            pass

        # 用单独的随机数：全局的那个可能被别的测试留下的线程推进。
        with patch("asyncio.sleep", no_wait), patched_in_proactive_modules(random=random.Random(1)):
            try:
                result = call(probe)
                outcome = {"result": (await result) if is_async else result}
            except Exception as exc:
                outcome = {"raises": type(exc).__name__}
        return _normalize({**outcome, "calls": probe.calls})

    async def test_matches_snapshot(self):
        results = {name: await self._scene(build, True) for name, build in PIECES.items()}
        results.update({name: await self._scene(build, False) for name, build in SYNC_PIECES.items()})
        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS") or not SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(results, json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()


class PreferenceDayTests(unittest.IsolatedAsyncioTestCase):
    """B5c：只有「喜好日」才拿口味和喜好信号去想搜索词。"""

    def test_roll_is_fixed_within_a_day_and_follows_the_chance(self):
        from datetime import date
        days = [date(2026, 11, 1) + timedelta(days=i) for i in range(200)]
        discovery = Probe({"PROACTIVE_PREFERENCE_DAY_CHANCE": 0.35}).video_discovery
        rolls = [discovery._preference_day(day) for day in days]
        self.assertEqual(rolls, [discovery._preference_day(day) for day in days])
        self.assertTrue(0.2 < sum(rolls) / len(rolls) < 0.5)
        self.assertFalse(any(Probe({"PROACTIVE_PREFERENCE_DAY_CHANCE": 0}).video_discovery._preference_day(d) for d in days))

    async def test_off_day_prompt_leaves_out_taste_and_signals(self):
        probe = Probe({"PROACTIVE_PREFERENCE_DAY_CHANCE": 0}, files=TASTE_FILES, reply=GOOD_QUERIES)
        await probe.video_discovery._decide_proactive_search_queries()
        prompt = probe.calls[-1][1]
        self.assertIn("今天不照着口味搜", prompt)
        self.assertNotIn("分区口味", prompt)
        self.assertNotIn("具体信号", prompt)
        on = Probe(files=TASTE_FILES, reply=GOOD_QUERIES)
        await on.video_discovery._decide_proactive_search_queries()
        self.assertIn("分区口味", on.calls[-1][1])
        self.assertIn("具体信号", on.calls[-1][1])

    def test_off_day_fallback_does_not_use_preferences(self):
        probe = Probe({"PROACTIVE_PREFERENCE_DAY_CHANCE": 0}, files=TASTE_FILES)
        _, _, id_name = probe.video_pools._zone_id_maps()
        zones = {str(name) for name in id_name.values()}
        keywords = probe.video_discovery._fallback_proactive_search_queries()
        self.assertTrue(keywords)
        self.assertTrue(set(keywords) <= zones)


class PrefilterScopeTests(unittest.IsolatedAsyncioTestCase):
    """tools-and-favorites-plan 第 6 条：只有特别关注不筛，普通关注和口味分区也要过标题筛选。"""

    async def test_only_special_follows_skip_the_title_check(self):
        probe = Probe({"ENABLE_PROACTIVE_LLM_PREFILTER": True, "PROACTIVE_EXPLORATION_RATIO": 0}, reply="no 没兴趣")
        check = probe.video_discovery._should_watch_video_before_download
        special = {"bvid": "B1", "title": "t", "_source": "follow", "_source_detail": "special_follow"}
        self.assertEqual(await check(special, [17], 0, 3), (True, "特别关注，直接看"))
        following = {"bvid": "B2", "title": "t", "_source": "follow", "_source_detail": "following"}
        self.assertFalse((await check(following, [17], 0, 3))[0])
        self.assertTrue((await check(following, [17], 3, 3))[0])  # 拒够 3 次必须看
