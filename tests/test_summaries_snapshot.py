"""日总结、周总结的快照：什么时候跑、发给模型什么、发到哪、记了什么。

假的只有模型、QQ、B站和文件。

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_summaries_snapshot.py
"""
import copy
import json
import os
import re
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.config import (
    BANGUMI_WATCH_LOG_FILE, DAILY_SUMMARY_FILE, DYNAMIC_LOG_FILE, PROACTIVE_LOG_FILE, WATCH_LOG_FILE, WEEKLY_SUMMARY_FILE,
)
from core.domain.dynamic_log import DynamicLog
from core.storage.activity_log import ActivityLog
from core.settings import Settings
from summary_parts import SummaryParts
from itself import itself

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "summaries.json"
NOW = datetime.now()


def stamp(days=0):
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")


ACTIVITY = {
    WATCH_LOG_FILE: [
        {"time": stamp(0), "title": "木工抽屉", "score": 9, "up_name": "工匠", "up_mid": "42", "tname": "手工", "bvid": "BV1", "source": "search",
         "review": "榫头好看", "mood": "开心", "content_summary": "做抽屉", "actions": ["点赞"],
         "preference_signals": [{"type": "theme", "value": "木工", "polarity": "like", "strength": 0.8, "evidence": "榫头"}]},
        {"time": stamp(2.5), "title": "无聊开箱", "score": 3, "up_name": "甲", "tname": "数码", "bvid": "BV2", "review": "一般", "mood": "无聊"},
        {"time": stamp(0), "title": "失败", "evaluation_status": "failed", "bvid": "BV3"},
        {"time": stamp(30), "title": "太旧", "score": 10, "bvid": "BV4", "review": "旧"},
    ],
    BANGUMI_WATCH_LOG_FILE: [
        {"time": stamp(0), "title": "番", "ep_index": "1", "score": 8, "review": "好看", "mood": "开心", "comment": "好"},
        {"time": stamp(0), "title": "坏番", "ep_index": "2", "evaluation_status": "failed"},
    ],
    DYNAMIC_LOG_FILE: [{"time": stamp(0), "text": "今天的动态"}, {"time": stamp(1.5), "text": "周报", "weekly": True}],
    PROACTIVE_LOG_FILE: [{"time": stamp(0), "title": "木工抽屉", "comment": "榫头漂亮"}],
}


class Probe(SummaryParts):
    models = itself

    def __init__(self, config=None, files=None, reply="今天看了木工，榫头很好看。", outcome=None, upload=True, send_raises=False, memory=None):
        self.config = {"WEEKLY_SUMMARY_RENDER_IMAGE": False, "DAILY_SUMMARY_RENDER_IMAGE": False, **(config or {})}
        self.settings = Settings(self.config)
        self.files = copy.deepcopy(files or {})
        self.reply = reply
        self.outcome = outcome or {"success": True, "state": "committed"}
        self.upload = upload
        self.send_raises = send_raises
        self.calls = []
        self._memory = list(memory or [])
        self.context = types.SimpleNamespace(send_message=self._send_message)
        self.dynamic_log = DynamicLog(ActivityLog("dynamic", file=types.SimpleNamespace(
            read=lambda default=None: self.json_store._load_json(DYNAMIC_LOG_FILE, default),
            update=self._update_log,
        )))
        self._attach()

    def _attach(self):
        """重构后这里接上新模块。"""

    def _update_log(self, change, default=None):
        data = self.json_store._load_json(DYNAMIC_LOG_FILE, default)
        result = change(data)
        self.json_store._save_json(DYNAMIC_LOG_FILE, data if result is None else result)

    def _load_json(self, path, default=None):
        return copy.deepcopy(self.files.get(path, default))

    def _save_json(self, path, value):
        self.calls.append(["save", os.path.basename(path)])
        self.files[path] = copy.deepcopy(value)

    async def _send_message(self, umo, chain):
        self.calls.append(["qq", umo, chain.text, chain.chain])
        if self.send_raises:
            raise RuntimeError("发送结果未知")

    async def _get_system_prompt(self):
        return "系统提示词"

    async def _llm_call(self, prompt, **kwargs):
        self.calls.append(["model", prompt, kwargs.get("max_tokens")])
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    async def _generation_llm_call(self, prompt, **kwargs):
        return await self._llm_call(prompt, **kwargs)

    async def _summary_llm_call(self, prompt, **kwargs):
        return await self._llm_call(prompt, **kwargs)

    async def _upload_image_to_bilibili(self, path):
        self.calls.append(["upload", path])
        return {"url": "u"} if self.upload else None

    async def _post_dynamic_text(self, text):
        self.calls.append(["post_text", text])

    async def _post_dynamic_with_image(self, text, img, state=None):
        self.calls.append(["post_image", text, img])
        if state is not None:
            state["sent"] = True

    @property
    def dynamic_post(self):
        return types.SimpleNamespace(queue_post=self._queue_dynamic_post)

    async def _queue_dynamic_post(self, text, handler, *, with_outcome=False):
        self.calls.append(["queue", text])
        if self.outcome.get("success"):
            await handler()
        return types.SimpleNamespace(**self.outcome)

    def _load_preference_lifecycle(self):
        return {}


class FakeChain:
    def __init__(self):
        self.text = ""
        self.chain = []

    def message(self, text):
        self.text += text
        return self


class FakeImage:
    @staticmethod
    def fromFileSystem(path):
        if path == "broken.png":
            raise OSError("图片坏了")
        return f"[图片:{path}]"


QQ = {"WEEKLY_SUMMARY_MODE": "qq", "DAILY_SUMMARY_MODE": "qq", "WEEKLY_SUMMARY_QQ_UMO": "umo", "DAILY_SUMMARY_QQ_UMO": "umo"}
DYN = {"WEEKLY_SUMMARY_MODE": "dynamic", "DAILY_SUMMARY_MODE": "dynamic"}
BOTH = {**QQ, **DYN, "WEEKLY_SUMMARY_MODE": "both", "DAILY_SUMMARY_MODE": "both"}

SCENES = {}
for kind, run in (("daily", "run_daily_summary"), ("weekly", "run_weekly_summary")):
    records = {"daily": DAILY_SUMMARY_FILE, "weekly": WEEKLY_SUMMARY_FILE}[kind]
    deliver = f"_deliver_{kind}_summary"
    SCENES.update({
        f"{kind}/no_activity": (dict(reply=None), lambda p, r=run: getattr(p.summaries, r)()),
        f"{kind}/archive_only": (dict(files=ACTIVITY), lambda p, r=run: getattr(p.summaries, r)()),
        f"{kind}/qq": (dict(config=QQ, files=ACTIVITY), lambda p, r=run: getattr(p.summaries, r)()),
        f"{kind}/qq_no_umo": (dict(config={"WEEKLY_SUMMARY_MODE": "qq", "DAILY_SUMMARY_MODE": "qq"}, files=ACTIVITY), lambda p, r=run: getattr(p.summaries, r)()),
        f"{kind}/qq_send_unknown": (dict(config=QQ, files=ACTIVITY, send_raises=True), lambda p, r=run: getattr(p.summaries, r)()),
        f"{kind}/dynamic": (dict(config=DYN, files=ACTIVITY), lambda p, r=run: getattr(p.summaries, r)()),
        f"{kind}/both": (dict(config=BOTH, files=ACTIVITY), lambda p, r=run: getattr(p.summaries, r)()),
        f"{kind}/dynamic_unknown": (dict(config=DYN, files=ACTIVITY, outcome={"success": False, "state": "unknown"}), lambda p, r=run: getattr(p.summaries, r)()),
        f"{kind}/dynamic_refused": (dict(config=DYN, files=ACTIVITY, outcome={"success": False, "state": "failed"}), lambda p, r=run: getattr(p.summaries, r)()),
        f"{kind}/model_fails": (dict(files=ACTIVITY, reply=None), lambda p, r=run: getattr(p.summaries, r)()),
        f"{kind}/model_raises": (dict(files=ACTIVITY, reply=RuntimeError("炸")), lambda p, r=run: getattr(p.summaries, r)()),
        f"{kind}/deliver_image_dynamic": (dict(config=DYN, files=ACTIVITY), lambda p, d=deliver: getattr(p.summaries, d)("总结", image_path="a.png")),
        f"{kind}/deliver_image_upload_fails": (dict(config=DYN, files=ACTIVITY, upload=False), lambda p, d=deliver: getattr(p.summaries, d)("总结", image_path="a.png")),
        f"{kind}/deliver_image_qq": (dict(config=QQ), lambda p, d=deliver: getattr(p.summaries, d)("总结", image_path="a.png")),
        f"{kind}/deliver_image_qq_broken": (dict(config=QQ), lambda p, d=deliver: getattr(p.summaries, d)("总结", image_path="broken.png")),
    })

SCENES.update({
    "due/daily_off": (dict(), lambda p: p.summaries._maybe_daily_summary()),
    "due/daily_wrong_hour": (dict(config={"ENABLE_DAILY_SUMMARY": True, "DAILY_SUMMARY_HOUR": (NOW.hour + 2) % 24}, files=ACTIVITY), lambda p: p.summaries._maybe_daily_summary()),
    "due/daily_right_hour": (dict(config={"ENABLE_DAILY_SUMMARY": True, "DAILY_SUMMARY_HOUR": NOW.hour}, files=ACTIVITY), lambda p: p.summaries._maybe_daily_summary()),
    "due/daily_done_today": (dict(config={"ENABLE_DAILY_SUMMARY": True, "DAILY_SUMMARY_HOUR": NOW.hour}, files={DAILY_SUMMARY_FILE: [{"date": NOW.strftime("%Y-%m-%d")}]}), lambda p: p.summaries._maybe_daily_summary()),
    "due/weekly_off": (dict(), lambda p: p.summaries._maybe_weekly_summary()),
    "due/weekly_wrong_day": (dict(config={"ENABLE_WEEKLY_SUMMARY": True, "WEEKLY_SUMMARY_DAY": (NOW.weekday() + 2) % 7}, files=ACTIVITY), lambda p: p.summaries._maybe_weekly_summary()),
    "due/weekly_right_day": (dict(config={"ENABLE_WEEKLY_SUMMARY": True, "WEEKLY_SUMMARY_DAY": NOW.weekday()}, files=ACTIVITY), lambda p: p.summaries._maybe_weekly_summary()),
    "due/weekly_done": (dict(config={"ENABLE_WEEKLY_SUMMARY": True, "WEEKLY_SUMMARY_DAY": NOW.weekday()}, files={WEEKLY_SUMMARY_FILE: [{"week": NOW.strftime("%G-W%V")}]}), lambda p: p.summaries._maybe_weekly_summary()),
    "archive/daily_keeps_45": (dict(files={DAILY_SUMMARY_FILE: [{"date": "2000-01-01", "i": i} for i in range(45)]}), lambda p: p.summaries.run_daily_summary()),
    "archive/weekly_keeps_20": (dict(files={WEEKLY_SUMMARY_FILE: [{"week": "2000-W01", "i": i} for i in range(20)]}), lambda p: p.summaries.run_weekly_summary()),
})


def _normalize(value):
    text = json.dumps(value, ensure_ascii=False, default=lambda o: f"<{type(o).__name__}>")
    text = re.sub(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?", "<TIME>", text)
    text = re.sub(r"\d{4}-W\d{2}", "<WEEK>", text)
    text = re.sub(r"\d{2}月\d{2}日|\d{1,2}月\d{1,2}日|\d{2}-\d{2}", "<DAY>", text)
    text = re.sub(r"周[一二三四五六日]", "<WD>", text)
    # 周记标题里的日期范围是 MM.DD（按今天往前推 7 天）。
    text = re.sub(r"(?<![\d.])\d{2}\.\d{2}(?![\d.])", "<DAY>", text)
    return json.loads(text)


class SummariesSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def _scene(self, script, call):
        probe = Probe(**copy.deepcopy(script))
        event = types.ModuleType("astrbot.api.event")
        event.MessageChain = FakeChain
        components = types.ModuleType("astrbot.api.message_components")
        components.Image = FakeImage
        with patch.dict(sys.modules, {"astrbot.api.event": event, "astrbot.api.message_components": components}):
            try:
                outcome = {"result": await call(probe)}
            except Exception as exc:
                outcome = {"raises": type(exc).__name__}
        files = {os.path.basename(k): (v[-2:] if isinstance(v, list) else v) for k, v in probe.files.items()}
        sizes = {os.path.basename(k): len(v) for k, v in probe.files.items() if isinstance(v, list)}
        return _normalize({**outcome, "calls": probe.calls, "tails": files, "sizes": sizes})

    async def test_matches_snapshot(self):
        results = {name: await self._scene(script, call) for name, (script, call) in SCENES.items()}
        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS") or not SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(results, json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
