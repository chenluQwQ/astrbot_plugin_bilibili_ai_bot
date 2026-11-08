"""关注动态巡视的快照：哪些动态被跳过、发给模型什么、记了什么。

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_dynamic_watch_snapshot.py
"""
import copy
import json
import os
import re
import unittest

from parts import part
from schedule_parts import LegacyNowDoing
from datetime import datetime
from pathlib import Path

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.config import DYNAMIC_WATCH_LOG_FILE
from vision_fakes import vision_on
from proactive_parts import ProactiveParts
from core.settings import Settings
from itself import itself

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "dynamic_watch.json"
BASE = {"ENABLE_DYNAMIC_WATCH": True, "DYNAMIC_WATCH_DAILY_LIMIT": 5}
OK = '{"interested": true, "summary": "  说了件事  ", "reason": "有趣"}'
NO = '{"interested": false, "summary": "广告", "reason": "没意思"}'


def post(dynamic_id="1", **extra):
    return {"dynamic_id": dynamic_id, "up_mid": "42", "up_name": "UP", "type": "WORD", "text": "正文", **extra}


class Probe(ProactiveParts):
    models = itself
    vision = property(vision_on)

    def __init__(self, config=None, updates=(), replies=(), log=None, images=None):
        self.config = {**BASE, **(config or {})}
        self.settings = Settings(self.config)
        self.updates = list(updates)
        self.replies = list(replies)
        self.images = images
        self.files = {DYNAMIC_WATCH_LOG_FILE: copy.deepcopy(log)} if log is not None else {}
        self.calls = []

    def _load_json(self, path, default=None):
        return copy.deepcopy(self.files.get(path, default))

    def _save_json(self, path, value):
        self.calls.append(["save", os.path.basename(path), len(value)])
        self.files[path] = copy.deepcopy(value)

    now_doing = part(LegacyNowDoing)


    def _set_cross_platform_activity(self, kind, stage, **details):
        self.calls.append(["doing", kind, stage])

    def _clear_cross_platform_activity(self, kind):
        self.calls.append(["done", kind])

    async def get_following_updates(self, limit=None):
        self.calls.append(["updates", limit])
        return self.updates

    async def _recognize_images(self, urls):
        self.calls.append(["images", urls])
        if isinstance(self.images, Exception):
            raise self.images
        return self.images

    async def _generation_llm_call(self, prompt, max_tokens=None):
        self.calls.append(["model", prompt, max_tokens])
        reply = self.replies.pop(0) if self.replies else OK
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def _llm_call(self, prompt, **_kwargs):
        raise AssertionError("有生成模型入口时不该走主模型")


    async def _save_self_memory_record(self, key, text, memory_type=None, extra=None):
        self.calls.append(["memory", key, text, memory_type, extra])


def today(minutes=0):
    return datetime.now().strftime("%Y-%m-%d %H:%M")


SCENES = {
    "disabled": dict(config={"ENABLE_DYNAMIC_WATCH": False}),
    "limit_zero": dict(config={"DYNAMIC_WATCH_DAILY_LIMIT": 0}),
    "used_up": dict(config={"DYNAMIC_WATCH_DAILY_LIMIT": 1}, log=[{"dynamic_id": "9", "time": today()}]),
    "nothing_new": dict(updates=[]),
    "interested_is_remembered": dict(updates=[post()]),
    "not_interested_not_remembered": dict(updates=[post()], replies=[NO]),
    "bad_json_not_remembered": dict(updates=[post()], replies=["不是 JSON"]),
    "blank_summary_not_remembered": dict(updates=[post()], replies=['{"interested": true, "summary": "  "}']),
    "wrong_type_not_remembered": dict(updates=[post()], replies=['{"interested": "yes", "summary": "x"}']),
    "model_raises": dict(updates=[post()], replies=[RuntimeError("炸了")]),
    "no_id_and_seen_skipped": dict(updates=[post(""), post("9"), post("2")], log=[{"dynamic_id": "9", "time": "2020-01-01 00:00"}]),
    "stops_at_remaining": dict(config={"DYNAMIC_WATCH_DAILY_LIMIT": 2}, updates=[post("1"), post("2"), post("3")]),
    "special_only": dict(config={"DYNAMIC_WATCH_SPECIAL_ONLY": True, "PROACTIVE_FOLLOW_UIDS": [" 43 "]}, updates=[post("1"), post("2", up_mid="43")]),
    "video_posts_skipped": dict(config={"DYNAMIC_WATCH_INCLUDE_VIDEO_POSTS": False}, updates=[post("1", video_bvid="BV1")]),
    "video_posts_included": dict(config={"DYNAMIC_WATCH_INCLUDE_VIDEO_POSTS": True}, updates=[post("1", text="", video_bvid="BV1", video_title="视频标题")]),
    "live_title_as_text": dict(updates=[post("1", text="", live_title="直播标题")]),
    "no_text": dict(updates=[post("1", text="")]),
    "images_recognized": dict(updates=[post("1", image_urls=["a", "b", "c", "d", "e"])], images="图里有猫"),
    "images_fail_still_judged": dict(updates=[post("1", image_urls=["a"])], images=RuntimeError("坏了")),
    "prompt_addon": dict(config={"DYNAMIC_WATCH_INTEREST_PROMPT": "喜欢猫"}, updates=[post()]),
    "log_is_capped": dict(updates=[post("x")], log=[{"dynamic_id": str(i), "time": "2020-01-01 00:00"} for i in range(500)]),
}


def _normalize(value):
    text = json.dumps(value, ensure_ascii=False, default=lambda o: f"<{type(o).__name__}>")
    return json.loads(re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", "<TIME>", text))


class DynamicWatchSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def _scene(self, script):
        probe = Probe(**copy.deepcopy(script))
        try:
            outcome = {"result": await probe.dynamic_watch.run()}
        except Exception as exc:
            outcome = {"raises": type(exc).__name__}
        log = probe.files.get(DYNAMIC_WATCH_LOG_FILE, [])
        return _normalize({**outcome, "calls": probe.calls, "log_tail": log[-2:], "log_size": len(log)})

    async def test_matches_snapshot(self):
        results = {name: await self._scene(script) for name, script in SCENES.items()}
        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS") or not SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(results, json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
