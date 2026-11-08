"""追番的快照：选了哪部、发给模型什么、做了哪些互动、记了什么。假的只有 B站、模型和文件。

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_bangumi_snapshot.py
"""
import copy
import json
import os
import random
import re
import types
import unittest

from bili_parts import BiliParts
from video_parts import VideoParts
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.config import BANGUMI_MEMORY_FILE, PROACTIVE_LOG_FILE, WATCH_LOG_FILE
from core.domain.bangumi_memory import BangumiMemory
from core.features.bangumi import Bangumi
from core.settings import Settings, SettingsAccessor
from itself import itself
from parts import part
from schedule_parts import LegacyNowDoing, ScheduleParts

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "bangumi.json"
BASE = {
    "ENABLE_BANGUMI": True, "BANGUMI_EPISODE_COUNT": 2, "BANGUMI_CONTINUE_SCORE": 6,
    "PROACTIVE_LIKE": True, "PROACTIVE_COIN": True, "PROACTIVE_FAV": True,
    "PROACTIVE_COMMENT_DAILY_LIMIT": 5, "PROACTIVE_COMMENT_COUNT": 1,
    "BANGUMI_COMMENT": True, "BANGUMI_AUTO_FOLLOW": True, "BANGUMI_POOLS": ["trending"],
}


def reply(score=7, want=True, comment="这集不错"):
    return json.dumps({"score": score, "comment": comment, "mood": "开心", "review": "挺好看", "want_continue": want}, ensure_ascii=False)


def ep(number, aid=None):
    return {"ep_id": 100 + number, "ep_index": str(number), "title": str(number), "long_title": f"第{number}个故事",
            "aid": number if aid is None else aid, "cid": 500 + number, "duration": 24}


def season(sid=7, count=3):
    return {"season_id": sid, "title": f"番{sid}", "evaluate": "一部番的简介", "cover": "cover.png", "styles": "日常",
            "areas": "日本", "score": 9.1, "episodes": [ep(i) for i in range(1, count + 1)]}


def watched(*numbers, sid=7, **extra):
    episodes = [{"ep_id": 100 + n, "ep_index": str(n), "title": str(n), "score": 7, "mood": "开心",
                 "review": f"感想{n}", "description": f"内容{n}"} for n in numbers]
    return {str(sid): {"title": f"番{sid}", "season_id": sid, "total_watched": len(episodes), "episodes": episodes, **extra}}


class FakeFile:
    """内存里的 JsonFile，和 ``_load_json`` 看到同一份内容。"""

    def __init__(self, probe, path):
        self.probe = probe
        self.path = path

    def read(self, default=None):
        return self.probe.json_store._load_json(self.path, {} if default is None else default)

    def write(self, data):
        self.probe.json_store._save_json(self.path, data)

    def update(self, change, default=None):
        data = self.read(default)
        result = change(data)
        result = data if result is None else result
        self.write(result)
        return result


class Probe(BiliParts, VideoParts):
    activities = ScheduleParts.activities
    models = itself
    bili_bangumi = itself
    outward_actions = itself
    watch_video = itself
    bangumi = part(Bangumi)
    settings = SettingsAccessor()

    def __init__(self, config=None, detail=None, trending=(), timeline=(), followed=(), replies=(), summary="前情提要",
                 analysis="这一集讲了一个很长的故事，主角终于和朋友和好了。", memory=None, watch_log=None, proactive_log=None,
                 refused=(), raises=(), cookie=True, cookie_valid=True, busy=False, lost_race=False):
        self.config = {**BASE, **(config or {})}
        self.settings = Settings(self.config)
        self.detail = detail
        self.trending = list(trending)
        self.timeline = list(timeline)
        self.followed = list(followed)
        self.replies = list(replies)
        self.summary = summary
        self.analysis = analysis
        self.refused = set(refused)
        self.raises = set(raises)
        self.cookie = cookie
        self.cookie_valid = cookie_valid
        self.busy = busy
        self.lost_race = lost_race
        self.calls = []
        self.files = {}
        if memory is not None:
            self.files[BANGUMI_MEMORY_FILE] = copy.deepcopy(memory)
        if watch_log is not None:
            self.files[WATCH_LOG_FILE] = copy.deepcopy(watch_log)
        if proactive_log is not None:
            self.files[PROACTIVE_LOG_FILE] = copy.deepcopy(proactive_log)
        self._attach_memory()

    def _attach_memory(self):
        self.bangumi_memory = BangumiMemory(FakeFile(self, BANGUMI_MEMORY_FILE))

    def _load_json(self, path, default=None):
        return copy.deepcopy(self.files.get(path, default))

    def _save_json(self, path, value):
        self.calls.append(["save", os.path.basename(path)])
        self.files[path] = copy.deepcopy(value)

    now_doing = part(LegacyNowDoing)


    def _set_cross_platform_activity(self, kind, stage, **details):
        self.calls.append(["doing", kind, stage, details])

    def _clear_cross_platform_activity(self, kind):
        self.calls.append(["done", kind])

    def _has_cookie(self):
        return self.cookie

    async def check_cookie(self):
        self.calls.append(["check_cookie"])
        return self.cookie_valid, "ok"

    def _boom(self, name):
        if name in self.raises:
            raise RuntimeError(f"{name} 炸了")

    async def get_bangumi_trending(self, season_type=1, day=3):
        self.calls.append(["trending", season_type])
        self._boom("trending")
        return [item for item in self.trending if item.get("season_type", 1) == season_type]

    async def get_bangumi_timeline(self, day_before=0, day_after=6):
        self.calls.append(["timeline", day_before, day_after])
        self._boom("timeline")
        return self.timeline

    async def get_bangumi_detail(self, season_id=None, ep_id=None):
        self.calls.append(["detail", season_id, ep_id])
        self._boom("detail")
        return self.detail

    async def _get_followed_bangumi(self, follow_status=0, page=1, page_size=30):
        self.calls.append(["followed", follow_status])
        self._boom("followed")
        return self.followed

    async def _follow_bangumi(self, season_id):
        self.calls.append(["follow", season_id])
        return True

    async def _oid_to_bvid(self, oid):
        self.calls.append(["oid_to_bvid", oid])
        self._boom("oid_to_bvid")
        return f"BV{oid}"

    async def _analyze_video_text(self, video_info, joint=None):
        self.calls.append(["analyze", video_info])
        self._boom("analyze")
        return self.analysis

    async def _get_system_prompt(self):
        return "系统提示词"

    async def _generation_llm_call(self, prompt, system_prompt=None, max_tokens=None):
        self.calls.append(["evaluate", prompt, system_prompt, max_tokens])
        answer = self.replies.pop(0) if self.replies else reply()
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def _summary_llm_call(self, prompt, max_tokens=None):
        self.calls.append(["summarize", prompt, max_tokens])
        self._boom("summarize")
        return self.summary

    async def _llm_call(self, prompt, **_kwargs):
        raise AssertionError("有专门的模型入口时不该走主模型")

    async def _save_self_memory_record(self, key, text, memory_type=None, extra=None):
        self.calls.append(["memory", key, text, memory_type, extra])

    @property
    def dynamic_post(self):
        return types.SimpleNamespace(
            collect_idea=self._collect_dynamic_idea,
            comment_syncs=lambda: bool(self.settings.get("COMMENT_SYNC_DYNAMIC")),
            comment_synced=lambda key, text, title="": self.calls.append(["comment_synced", key, text, title]),
        )

    def _collect_dynamic_idea(self, **idea):
        self.calls.append(["collect_dynamic_idea", idea])
        return 1

    async def _execute_proactive_action(self, key, kind, target, handler, metadata=None):
        self.calls.append(["action", key, kind, target, metadata])
        if kind in self.refused:
            return types.SimpleNamespace(success=False)
        if kind in self.raises:
            raise RuntimeError(f"{kind} 炸了")
        result = handler()
        if hasattr(result, "__await__"):
            await result
        return types.SimpleNamespace(success=True)

    async def _like_video(self, aid):
        self.calls.append(["like", aid])

    async def _coin_video(self, aid):
        self.calls.append(["coin", aid])

    async def _fav_video(self, aid):
        self.calls.append(["fav", aid])

    async def _send_comment(self, aid, text, oid_type=1, *, sync_to_dynamic=False):
        self.calls.append(["comment", aid, text, oid_type, *(["sync_to_dynamic"] if sync_to_dynamic else [])])

    @staticmethod
    def _today_proactive_comment_count(watch_log, proactive_log=None):
        return len(watch_log or []) + len(proactive_log or [])

    def _activity_busy(self, kind):
        return self.busy

    def _run_activity_now(self, kind, **kwargs):
        self.calls.append(["run_now", kind, kwargs])
        return None if self.lost_race else object()


RUN_SCENES = {
    "picks_from_trending": (dict(detail=season(), trending=[{"season_id": 7, "title": "番7", "score": 9}]), {}),
    "no_pools_means_trending": (dict(config={"BANGUMI_POOLS": []}, detail=season(), trending=[{"season_id": 7, "title": "番7"}]), {}),
    "timeline_pool_only_published": (dict(config={"BANGUMI_POOLS": ["timeline"]}, detail=season(), timeline=[{"season_id": 8, "title": "没播", "published": False}, {"season_id": 7, "title": "番7", "published": True}]), {}),
    "unknown_pool_falls_back": (dict(config={"BANGUMI_POOLS": ["nope"]}, detail=season(), trending=[{"season_id": 7, "title": "番7"}]), {}),
    "pool_error_falls_back": (dict(detail=season(), raises=["trending"]), {}),
    "no_candidates": (dict(), {}),
    "completed_is_not_picked": (dict(detail=season(9), trending=[{"season_id": 7, "title": "番7"}, {"season_id": 9, "title": "番9"}], memory=watched(1, completed=True)), {}),
    "only_completed_picks_nothing": (dict(trending=[{"season_id": 7, "title": "番7"}], memory=watched(1, completed=True)), {}),
    "only_followed_ones_are_candidates": (dict(detail=season(), trending=[{"season_id": 7, "title": "番7"}], memory=watched(1)), {}),
    "no_detail": (dict(detail=None), {"season_id": 7}),
    "no_episodes": (dict(detail={**season(), "episodes": []}), {"season_id": 7}),
    "detail_raises_is_swallowed": (dict(raises=["detail"]), {"season_id": 7}),
    "stops_at_episode_count": (dict(detail=season(count=5)), {"season_id": 7}),
    "max_episodes_argument": (dict(detail=season(count=5)), {"season_id": 7, "max_episodes": 1}),
    "continues_after_watched": (dict(detail=season(), memory=watched(1)), {"season_id": 7}),
    "low_score_stops": (dict(detail=season(), replies=[reply(score=4, want=False)]), {"season_id": 7}),
    "low_score_wants_more": (dict(detail=season(), replies=[reply(score=4, want=True), reply(score=4, want=False)]), {"season_id": 7}),
    "wants_dynamic_goes_to_pool": (dict(detail=season(count=1), replies=[json.dumps({
        "score": 7, "comment": "", "mood": "好笑", "review": "反派又输了", "want_continue": True,
        "want_dynamic": True, "dynamic_angle": "反派每集都输得很有创意", "dynamic_kind": "段子"}, ensure_ascii=False)]), {"season_id": 7}),
    "top_score_does_everything": (dict(detail=season(count=1), replies=[reply(score=9)]), {"season_id": 7}),
    "score_six_likes_only": (dict(detail=season(count=1), replies=[reply(score=6)]), {"season_id": 7}),
    "no_quota": (dict(detail=season(), refused=["bangumi_watch"]), {"season_id": 7}),
    "bad_json_retries_next_time": (dict(detail=season(), replies=[reply(), "不是 JSON"]), {"season_id": 7}),
    "blank_reply": (dict(detail=season(), replies=[""]), {"season_id": 7}),
    "model_raises": (dict(detail=season(), replies=[RuntimeError("炸了")]), {"season_id": 7}),
    "custom_instruction": (dict(config={"CUSTOM_PROACTIVE_INSTRUCTION": "多吐槽"}, detail=season(count=1)), {"season_id": 7}),
    "short_analysis_uses_introduction": (dict(detail=season(count=1), analysis="短"), {"season_id": 7}),
    "analysis_raises_uses_title": (dict(detail=season(count=1), raises=["analyze"]), {"season_id": 7}),
    "bvid_lookup_failure_ignored": (dict(detail=season(count=1), raises=["oid_to_bvid"]), {"season_id": 7}),
    "start_episode": (dict(detail=season()), {"season_id": 7, "start_ep_id": 102}),
    "start_episode_rewatch_skips_later_watched": (dict(detail=season(), memory=watched(2, 3)), {"season_id": 7, "start_ep_id": 102}),
    "start_episode_missing": (dict(detail=season()), {"season_id": 7, "start_ep_id": 999}),
    "all_watched_marks_completed": (dict(detail=season(count=2), memory=watched(1, 2)), {"season_id": 7}),
    "nothing_to_watch_and_no_record": (dict(detail={**season(), "episodes": [{"ep_index": "PV"}]}), {"season_id": 7}),
    "context_lists_few_episodes": (dict(detail=season(count=4), memory=watched(1, 3)), {"season_id": 7, "max_episodes": 1}),
    "context_summarizes_many": (dict(detail=season(count=8), memory=watched(1, 2, 3, 4, 5, 7)), {"season_id": 7, "max_episodes": 1}),
    "summary_fails": (dict(detail=season(count=8), memory=watched(1, 2, 3, 4, 5, 6), raises=["summarize"]), {"season_id": 7, "max_episodes": 1}),
    "non_numeric_index_sorted_last": (dict(detail={**season(count=1), "episodes": [{**ep(1), "ep_index": "PV"}, ep(2)]}), {"season_id": 7}),
    "cookie_expired_no_interactions": (dict(detail=season(count=1), cookie_valid=False, replies=[reply(score=9)]), {"season_id": 7}),
    "like_refused_stops_interactions": (dict(detail=season(count=1), refused=["like"], replies=[reply(score=9)]), {"season_id": 7}),
    "like_raises_stops_interactions": (dict(detail=season(count=1), raises=["like"], replies=[reply(score=9)]), {"season_id": 7}),
    "coin_and_favorite_refused_keep_going": (dict(detail=season(count=1), refused=["coin", "favorite"], replies=[reply(score=9)]), {"season_id": 7}),
    "comment_raises_is_swallowed": (dict(detail=season(count=1), raises=["proactive_comment"], replies=[reply(score=9)]), {"season_id": 7}),
    "follow_raises_is_swallowed": (dict(detail=season(count=1), raises=["follow_bangumi"], replies=[reply(score=9)]), {"season_id": 7}),
    "no_aid_no_interactions": (dict(detail={**season(count=1), "episodes": [ep(1, aid=0)]}, replies=[reply(score=9)]), {"season_id": 7}),
    "comment_limit_reached": (dict(config={"PROACTIVE_COMMENT_DAILY_LIMIT": 1}, detail=season(count=1), watch_log=[{"x": 1}]), {"season_id": 7}),
    "comment_switch_off": (dict(config={"BANGUMI_COMMENT": False}, detail=season(count=1)), {"season_id": 7}),
    "comment_count_zero": (dict(config={"PROACTIVE_COMMENT_COUNT": 0}, detail=season(count=1)), {"season_id": 7}),
    "empty_comment": (dict(detail=season(count=1), replies=[reply(comment="")]), {"season_id": 7}),
    "interaction_switches_off": (dict(config={"PROACTIVE_LIKE": False, "PROACTIVE_COIN": False, "PROACTIVE_FAV": False, "BANGUMI_AUTO_FOLLOW": False}, detail=season(count=1), replies=[reply(score=9)]), {"season_id": 7}),
    "memory_keeps_last_fifty": (dict(detail=season(count=52), memory=watched(*range(1, 51))), {"season_id": 7, "max_episodes": 1}),
    "watch_log_is_capped": (dict(detail=season(count=1)), {"season_id": 7}),
    "proactive_log_is_capped": (dict(config={"PROACTIVE_COMMENT_DAILY_LIMIT": 500}, detail=season(count=1), proactive_log=[{"i": i} for i in range(100)]), {"season_id": 7}),
}

UPDATE_SCENES = {
    "disabled": dict(config={"ENABLE_BANGUMI": False}),
    "logged_out": dict(cookie=False),
    "nothing_followed_watches_anyway": dict(detail=season(count=1), trending=[{"season_id": 7, "title": "番7"}]),
    "followed_never_watched": dict(detail=season(count=1), followed=[{"season_id": 7, "title": "番7"}]),
    "followed_has_new_episode": dict(detail=season(count=2), followed=[{"season_id": 7, "title": "番7"}], memory=watched(1)),
    "up_to_date_watches_anyway": dict(detail=season(count=1), followed=[{"season_id": 7, "title": "番7"}], trending=[{"season_id": 7, "title": "番7"}], memory=watched(1)),
    "followed_detail_missing": dict(detail=None, followed=[{"season_id": 7, "title": "番7"}], memory=watched(1)),
    "followed_raises": dict(raises=["followed"]),
}

TOOL_SCENES = {
    "disabled": (dict(config={"ENABLE_BANGUMI": False}), {"season_id": 7}),
    "logged_out": (dict(cookie=False), {"season_id": 7}),
    "busy": (dict(busy=True), {"season_id": 7}),
    "starts": (dict(), {"season_id": 7}),
    "looks_up_season_from_episode": (dict(detail=season()), {"ep_id": 101}),
    "episode_lookup_fails": (dict(raises=["detail"]), {"ep_id": 101}),
    "loses_the_race": (dict(lost_race=True), {"season_id": 7}),
}


def _normalize(value):
    text = json.dumps(value, ensure_ascii=False, default=lambda o: f"<{type(o).__name__}>")
    return json.loads(re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", "<TIME>", text))


class BangumiSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def _scene(self, script, call):
        probe = Probe(**copy.deepcopy(script))

        async def no_wait(seconds):
            probe.calls.append(["sleep", seconds])

        # 用单独的随机数：全局的那个可能被别的测试留下的线程推进。
        with patch("asyncio.sleep", no_wait), patch("core.features.bangumi.random", random.Random(1)):
            try:
                outcome = {"result": await call(probe)}
            except Exception as exc:
                outcome = {"raises": type(exc).__name__}
        files = {os.path.basename(path): value for path, value in probe.files.items()}
        for name, value in files.items():
            if isinstance(value, list) and len(value) > 3:
                files[name] = {"size": len(value), "tail": value[-2:]}
        memory = files.get(os.path.basename(BANGUMI_MEMORY_FILE))
        if memory:
            for record in memory.values():
                record["episode_ids"] = [e.get("ep_id") for e in record["episodes"]]
                record["episodes"] = record["episodes"][-1:]
        return _normalize({**outcome, "calls": probe.calls, "files": files})

    async def test_matches_snapshot(self):
        results = {}
        for name, (script, kwargs) in RUN_SCENES.items():
            results[f"run/{name}"] = await self._scene(script, lambda p, k=kwargs: p.bangumi.run(**k))
        for name, script in UPDATE_SCENES.items():
            results[f"update/{name}"] = await self._scene(script, lambda p: p.bangumi.check_updates())
        for name, (script, kwargs) in TOOL_SCENES.items():
            results[f"tool/{name}"] = await self._scene(script, lambda p, k=kwargs: p.bangumi.watch_on_request(**k))
        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS") or not SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(results, json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
