"""Behaviour snapshots for the full proactive watch and special-follow rounds.

Each scenario runs one complete round against fake Bilibili, model and storage
helpers, recording every external call, prompt and JSON write in order.  The
recording must match ``snapshots/proactive_flows.json`` exactly, so splitting
the flow into smaller functions cannot silently change what the bot does.

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_proactive_flow_snapshot.py
"""

import copy
import json
import os
import random
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

import core.proactive as proactive_module
from core.proactive import ProactiveMixin
from core.utils import UtilsMixin
from core.video import VideoMixin


SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "proactive_flows.json"
NOW = datetime(2026, 10, 25, 14, 30)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(NOW.year, NOW.month, NOW.day, NOW.hour, NOW.minute)


TODAY_TS = int(FixedDatetime(2026, 10, 25, 9, 0).timestamp())
YESTERDAY_TS = int(FixedDatetime(2026, 10, 24, 9, 0).timestamp())


def video(bvid, mid, up_name, title, **extra):
    return {"bvid": bvid, "up_mid": mid, "up_name": up_name, "title": title,
            "desc": f"{title} 的简介", "pic": f"https://i0.hdslb.com/{bvid}.jpg",
            "tname": "动画", **extra}


def evaluation(score, **extra):
    return {
        "score": score,
        "score_reason": f"评分理由{score}",
        "preference_signals": [{"polarity": "like", "value": "手绘"}],
        "search_keywords": ["手绘动画"],
        "comment": f"短评{score}",
        "mood": "开心",
        "review": f"感想{score}",
        "want_follow": False,
        "recommend_owner": False,
        "recommend_reason": "",
        "want_dynamic": False,
        "dynamic_angle": "",
        "partition": "动画",
        **extra,
    }


class FlowProbe(ProactiveMixin):
    """Fakes every collaborator of the two flows and records what they receive."""

    _append_json_list = UtilsMixin._append_json_list
    _clip_media_text = staticmethod(VideoMixin._clip_media_text)

    def __init__(self, config, *, files=None, latest=None, followings=(), pool=(),
                 search=(), evaluations=None, analysis_basis=None, oids=None,
                 cookies=None, failing_actions=(), watched=()):
        self.config = config
        self.calls = []
        self.files = copy.deepcopy(files or {})
        self.latest = latest or {}
        self.followings = list(followings)
        self.pool = list(pool)
        self.search = list(search)
        self.evaluations = evaluations or {}
        self.analysis_basis = analysis_basis or {}
        self.oids = oids or {}
        self.cookies = list(cookies or [])
        self.failing_actions = set(failing_actions)
        self.watched = set(watched)

    def record(self, *items):
        self.calls.append(list(items))

    # ── storage ──
    def _load_json(self, path, default=None):
        name = os.path.basename(path)
        if name in self.files:
            return copy.deepcopy(self.files[name])
        return {} if default is None else default

    def _save_json(self, path, data):
        name = os.path.basename(path)
        self.record("save_json", name)
        self.files[name] = copy.deepcopy(data)

    # ── runtime ──
    def _set_cross_platform_activity(self, kind, text, **extra):
        self.record("activity_set", kind, text, extra)

    def _clear_cross_platform_activity(self, kind):
        self.record("activity_clear", kind)

    async def _execute_proactive_action(self, key, kind, target_id, handler, *, metadata=None):
        self.record("action", key, kind, str(target_id), metadata or {})
        if key in self.failing_actions:
            return types.SimpleNamespace(success=False, state="failed", reason="test_failure")
        result = handler()
        if hasattr(result, "__await__"):
            result = await result
        return types.SimpleNamespace(success=bool(result), state="done", reason="")

    def _get_environment_status(self):
        return {"features": {"proactive_video_media": True}}

    # ── discovery ──
    async def _seen_video_bvids(self):
        return set(self.watched)

    async def _get_up_latest_video(self, mid):
        self.record("get_up_latest_video", str(mid))
        found = self.latest.get(str(mid))
        return dict(found) if found else None

    async def get_followings(self):
        self.record("get_followings")
        return list(self.followings)

    async def _get_pool_videos(self, min_pubdate=0):
        self.record("get_pool_videos", min_pubdate)
        return [dict(item) for item in self.pool]

    async def _decide_proactive_search_queries(self, watch_log=None):
        self.record("decide_search_queries", len(watch_log or []))
        return ["手绘动画"]

    async def _get_proactive_search_videos(self, keywords, limit, watched_bvids=None):
        self.record("get_search_videos", keywords, limit, sorted(watched_bvids or []))
        return [dict(item, _search_keyword=keywords[0]) for item in self.search]

    def _get_taste_tids(self, min_score=7, min_count=2):
        return []

    # ── watching ──
    async def _get_video_oid(self, bvid):
        self.record("get_video_oid", bvid)
        return self.oids.get(bvid, 0)

    async def _get_video_info(self, oid):
        self.record("get_video_info", oid)
        return {"bvid": f"BV_INFO_{oid}", "title": f"接口标题{oid}", "desc": f"接口简介{oid}",
                "owner_name": f"接口UP{oid}", "owner_mid": str(oid), "tname": "接口分区",
                "duration": 120, "pic": f"https://i0.hdslb.com/info{oid}.jpg", "cid": oid * 10}

    async def _analyze_video_with_vision(self, info):
        bvid = info.get("bvid")
        self.record("analyze_video", bvid, info.get("title"), info.get("cid", 0))
        for source_bvid, basis in self.analysis_basis.items():
            if bvid in {source_bvid, f"BV_INFO_{self.oids.get(source_bvid)}"}:
                info["analysis_basis"] = basis
                break
        else:
            info["analysis_basis"] = "video_media"
        return f"{info.get('title')}的画面分析：手绘过渡和配乐。" + "细节" * 80

    async def _mark_video_seen(self, bvid, info, source="proactive"):
        self.record("mark_video_seen", bvid, source, info.get("analysis_basis"))

    async def _evaluate_video(self, info, description):
        bvid = info.get("bvid")
        self.record("evaluate_video", bvid)
        result = self.evaluations.get(bvid)
        if result is None:
            info["_evaluation_diagnostic"] = {"stage": "test", "reason": "评价失败"}
            return None
        return copy.deepcopy(result)

    async def check_cookie(self):
        ok = self.cookies.pop(0) if self.cookies else True
        self.record("check_cookie", ok)
        return ok, ""

    # ── model ──
    async def _get_system_prompt(self):
        return "persona"

    async def _owner_recommendation_context(self, query_text):
        self.record("owner_recommendation_context", query_text)
        return "喜欢手绘动画"

    async def _generation_llm_call(self, prompt, system_prompt=None, max_tokens=None):
        self.record("llm", prompt, system_prompt, max_tokens)
        if "评论区留一条评论" in prompt:
            return '{"decision":"comment","text":"转场那一下太舒服了"}'
        if "随手分享" in prompt:
            return '{"decision":"share","text":"主人，这段手绘过渡真的好看"}'
        return "yes"

    async def _llm_call(self, *args, **kwargs):
        raise AssertionError("the flows should use the generation model")

    # ── writes ──
    async def _like_video(self, oid):
        self.record("like_video", oid)
        return True

    async def _coin_video(self, oid):
        self.record("coin_video", oid)
        return True

    async def _fav_video(self, oid):
        self.record("fav_video", oid)
        return True

    async def _send_comment(self, oid, text, sync_to_dynamic=False):
        self.record("send_comment", oid, text)
        return True

    async def _follow_user(self, mid):
        self.record("follow_user", str(mid))
        return True

    async def _send_bili_private_message(self, mid, text):
        self.record("send_bili_private_message", str(mid), text)
        return True

    async def _send_owner_recommend_qq(self, text, bvid):
        self.record("send_owner_recommend_qq", text, bvid)
        return True

    async def _save_self_memory_record(self, key, text, memory_type=None, extra=None):
        self.record("save_self_memory", key, text, memory_type, extra)

    async def _run_dynamic(self, video=None):
        self.record("run_dynamic", video)


BASE_CONFIG = {
    "DEDE_USER_ID": "999",
    "OWNER_MID": "777",
    "OWNER_NAME": "小柠",
    "OWNER_BILI_NAME": "柠弥",
    "PROACTIVE_EXPLORATION_RATIO": 0,
    "RECOMMEND_OWNER_MIN_GAP_MINUTES": 0,
}


def regular_round():
    """Three sources, every interaction, owner share on all channels and a dynamic."""
    probe = FlowProbe(
        {**BASE_CONFIG, "PROACTIVE_VIDEO_COUNT": 3, "PROACTIVE_COMMENT_DAILY_LIMIT": 2,
         "PROACTIVE_FOLLOW_UIDS": ["100"], "PROACTIVE_COIN": True,
         "RECOMMEND_OWNER_DELIVERY": "all", "ENABLE_DYNAMIC": True},
        latest={
            "100": video("BV_FOLLOW", "100", "特关UP", "手绘短片", pubdate=YESTERDAY_TS),
            "200": video("BV_SELF", "999", "自己", "自己的视频", pubdate=TODAY_TS, oid=21),
            "300": video("BV_OLD", "300", "旧UP", "昨天的视频", pubdate=YESTERDAY_TS),
            "400": video("BV_TODAY", "400", "今日UP", "今天的视频", pubdate=str(TODAY_TS)),
        },
        followings=[100, 200, 300, 400],
        pool=[video("BV_POOL", "500", "池UP", "热门视频", oid=51),
              video("BV_WATCHED", "501", "看过UP", "看过的视频")],
        search=[video("BV_SEARCH", "600", "搜UP", "搜索视频")],
        evaluations={
            "BV_INFO_11": evaluation(9.6, recommend_owner=True, recommend_reason="画面很细",
                                     want_dynamic=True, dynamic_angle="手绘过渡"),
            "BV_POOL": evaluation(6.4),
            "BV_INFO_41": evaluation(8.2, partition=""),
        },
        analysis_basis={"BV_SEARCH": "metadata"},
        oids={"BV_FOLLOW": 11, "BV_TODAY": 41},
        watched={"BV_WATCHED"},
        files={"watch_log.json": [
            {"time": "2026-10-25 08:00", "bvid": "BV_EARLY", "title": "早上", "source": "pool",
             "actions": ["💬评论"]},
        ]},
    )
    return probe, probe._run_proactive_inner()


def degraded_round():
    """Manual run: rejected reservation, expired cookie, failed like and a video without oid."""
    probe = FlowProbe(
        {**BASE_CONFIG, "PROACTIVE_DAILY_LIMIT": 5, "PROACTIVE_COMMENT_DAILY_LIMIT": 1,
         "RECOMMEND_OWNER_DELIVERY": "private_message", "ENABLE_PROACTIVE_LLM_PREFILTER": True,
         "PROACTIVE_LLM_PREFILTER_MAX_REJECTS": 1},
        pool=[video("BV_P1", "700", "UP7", "预约失败", oid=70),
              video("BV_P2", "701", "UP8", "点赞失败", oid=71),
              video("BV_P3", "702", "UP9", "Cookie失效", oid=72),
              video("BV_P4", "703", "UP10", "没有oid")],
        evaluations={
            "BV_INFO_71": evaluation(9.5, recommend_owner=True, recommend_reason="想分享"),
            "BV_INFO_72": evaluation(9.5, recommend_owner=True, recommend_reason="想分享"),
            "BV_P4": evaluation(9.5, want_dynamic=True, dynamic_angle=""),
        },
        oids={"BV_P1": 70, "BV_P2": 71, "BV_P3": 72},
        cookies=[False, True],
        failing_actions={"proactive_watch:BV_P1", "proactive_like:BV_P2"},
        files={
            "watch_log.json": [
                {"time": "2026-10-25 07:00", "bvid": "BV_A", "source": "follow", "actions": []},
                {"time": "2026-10-25 07:10", "bvid": "BV_B", "source": "taste", "actions": []},
                {"time": "2026-10-24 07:10", "bvid": "BV_C", "source": "pool",
                 "actions": ["✉️私信推荐给主人"], "title": "昨天推荐"},
            ],
        },
    )
    return probe, probe._run_proactive_inner(max_watch=3)


def daily_limit_round():
    probe = FlowProbe(
        {**BASE_CONFIG, "PROACTIVE_DAILY_LIMIT": 1},
        files={"watch_log.json": [{"time": "2026-10-25 07:00", "bvid": "BV_A", "actions": []}]},
    )
    return probe, probe._run_proactive_inner()


def special_follow_round():
    probe = FlowProbe(
        {**BASE_CONFIG, "PROACTIVE_FOLLOW_UIDS": ["100", "200", "300", "400", "999"],
         "PROACTIVE_COMMENT_DAILY_LIMIT": 2, "PROACTIVE_COIN": True,
         "RECOMMEND_OWNER_DELIVERY": "all"},
        latest={
            "100": video("BV_SP1", "100", "特关1", "特关视频", oid=81),
            "300": video("BV_SEEN", "300", "特关3", "看过了"),
            "400": video("BV_SP4", "400", "特关4", "评价失败的视频"),
            "999": video("BV_MINE", "999", "自己", "自己的视频"),
        },
        evaluations={"BV_INFO_81": evaluation(9.4, recommend_owner=True, recommend_reason="很好")},
        analysis_basis={"BV_SP4": "metadata"},
        oids={"BV_SP1": 81},
        watched={"BV_SEEN"},
        files={"proactive_log.json": [
            {"time": "2026-10-25 06:00", "bvid": "BV_EARLY", "comment": "早"},
        ]},
    )
    return probe, probe._run_special_follow_inner()


def special_follow_cookie_round():
    probe = FlowProbe(
        {**BASE_CONFIG, "PROACTIVE_FOLLOW_UIDS": ["100", "200"]},
        latest={"100": video("BV_SP1", "100", "特关1", "特关视频", oid=81),
                "200": video("BV_SP2", "200", "特关2", "点赞失败", oid=82)},
        evaluations={"BV_INFO_81": evaluation(9.4), "BV_INFO_82": evaluation(9.4)},
        cookies=[False, True],
        failing_actions={"special_follow_like:BV_SP2"},
    )
    return probe, probe._run_special_follow_inner()


SCENARIOS = {
    "regular_round": regular_round,
    "degraded_round": degraded_round,
    "daily_limit_round": daily_limit_round,
    "special_follow_round": special_follow_round,
    "special_follow_cookie_round": special_follow_cookie_round,
    "special_follow_empty": lambda: (lambda p: (p, p._run_special_follow_inner()))(FlowProbe({})),
}


class ProactiveFlowSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def run_scenario(self, name):
        probe, coroutine = SCENARIOS[name]()
        rng = random.Random(20261025)

        async def sleep(seconds):
            probe.record("sleep", seconds)

        fake_asyncio = types.SimpleNamespace(
            sleep=sleep, CancelledError=proactive_module.asyncio.CancelledError
        )
        with patch.object(proactive_module, "datetime", FixedDatetime), \
                patch.object(proactive_module, "random", rng), \
                patch.object(proactive_module, "asyncio", fake_asyncio):
            await coroutine
        return json.loads(json.dumps(
            {"calls": probe.calls, "files": probe.files}, ensure_ascii=False, default=sorted
        ))

    async def test_flows_match_snapshot(self):
        recorded = {name: await self.run_scenario(name) for name in SCENARIOS}
        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS"):
            SNAPSHOT_FILE.parent.mkdir(exist_ok=True)
            SNAPSHOT_FILE.write_text(
                json.dumps(recorded, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
            )
        expected = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        self.maxDiff = None
        self.assertEqual(sorted(recorded), sorted(expected))
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                self.assertEqual(recorded[name], expected[name])


if __name__ == "__main__":
    unittest.main()
