"""发动态的快照：哪些情况不发、发给模型什么、配什么图、发了什么、记了什么。

假的只有 B站、模型、搜索、生图服务和文件。

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_dynamic_post_snapshot.py
"""
import base64
import copy
import json
import os
import random
import re
import tempfile
import time
import types
import unittest

from bili_parts import BiliParts
from memory_parts import MemoryParts
from people_fakes import LegacyMood
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.config import (
    DAILY_SUMMARY_FILE, DYNAMIC_LOG_FILE, PERMANENT_MEMORY_FILE, WATCH_LOG_FILE, WEEKLY_SUMMARY_FILE,
)
from core.domain.dynamic_ideas import DynamicIdeas
from core.domain.dynamic_log import DynamicLog
from core.storage.activity_log import ActivityLog
from core.features.dynamic_post import DynamicPost
from parts import part
from schedule_parts import LegacyNowDoing, ScheduleParts
from core.settings import Settings
from itself import itself

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "dynamic_post.json"
# 生图和下封面发 HTTP 的模块：替换它里面的会话和临时目录。
IMAGE_MODULE = "core.infra.image_gen"
BASE = {"ENABLE_DYNAMIC": True, "DYNAMIC_FROM_VIDEO": True, "DYNAMIC_DAILY_COUNT": 3, "DYNAMIC_POST_CHANCE": 100,
        "DYNAMIC_MIN_INTERVAL_DAYS": 1, "DEDE_USER_ID": "9"}
IMAGE_CONFIG = {"IMAGE_GEN_API_KEY": "k", "IMAGE_GEN_API_BASE": "https://image.test", "IMAGE_GEN_MODEL": "m"}
SEARCH_CONFIG = {"ENABLE_WEB_SEARCH": True, "WEB_SEARCH_API_KEY": "k"}
PNG = b"\x89PNG-bytes"
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()
# 动态池子里的一条想法（看片时放进去的样子）；场景里 ``ideas`` 按顺序放进池子，编号从 1 开始。
IDEA = {"key": "BV1", "kind": "分享", "source": "proactive", "title": "测试视频", "up_name": "UP", "score": 8,
        "review": "好看", "summary": "讲了猫", "angle": "结尾反转", "pic": "https://i0.hdslb.com/a.jpg"}
OLD_IDEA = {**IDEA, "key": "BV_OLD", "kind": "吐槽", "title": "昨天的视频", "pic": "", "days_ago": 1}
REPOST_IDEA = {**IDEA, "key": "BV_R", "kind": "转发", "title": "值得一看"}
BANGUMI_IDEA = {**IDEA, "key": "ep:5", "kind": "段子", "source": "bangumi", "title": "《番》第3话", "up_name": "", "pic": ""}


def post(text="今天想说的话", image="none", prompt="", idea=None):
    value = {"decision": "post", "text": text, "search_query": "", "image": image, "image_prompt": prompt}
    if idea is not None:
        value["idea"] = idea
    return json.dumps(value, ensure_ascii=False)


SKIP = '{"decision":"skip","text":"","search_query":"","image":"none","image_prompt":""}'
SKIP_IDEA = '{"decision":"skip","text":"","search_query":"","image":"none","image_prompt":"","idea":0}'
SEARCH = '{"decision":"search","text":"","search_query":"查一下","image":"none","image_prompt":""}'


def stamp(days=0):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")


class FakeResponse:
    def __init__(self, status=200, body=b"", payload=None, content_type=""):
        self.status = status
        self.body = body
        self.payload = payload
        self.headers = {"Content-Type": content_type}

    async def read(self):
        return self.body

    async def json(self):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class FakeSession:
    def __init__(self, probe):
        self.probe = probe

    def _reply(self, method, url, **kwargs):
        self.probe.calls.append([method, url, {k: v for k, v in kwargs.items() if k in ("json", "headers")}])
        reply = self.probe.http
        if isinstance(reply, Exception):
            raise reply
        return reply

    def post(self, url, **kwargs):
        return self._reply("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self._reply("GET", url, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class FakeFile:
    """内存里的 JsonFile，和 ``_load_json`` 看到同一份内容。"""

    def __init__(self, probe, path):
        self.probe = probe
        self.path = path

    def read(self, default=None):
        return self.probe.json_store._load_json(self.path, [] if default is None else default)

    def update(self, change, default=None):
        data = self.read(default)
        result = change(data)
        result = data if result is None else result
        self.probe.json_store._save_json(self.path, result)
        return result


class Probe(BiliParts, MemoryParts):
    schedule_rules = ScheduleParts.schedule_rules
    daily_plan = ScheduleParts.daily_plan
    models = itself
    web_search = itself
    dynamic_post = part(DynamicPost)

    def __init__(self, config=None, replies=(), files=None, memory=None, plan=None, limit_max=0, outcome=None,
                 http=None, upload=True, image_sent=True, search_result="查到的资料", ideas=(), repost=True):
        self.config = {**BASE, **(config or {})}
        self.settings = Settings(self.config)
        self.replies = list(replies)
        self.files = copy.deepcopy(files or {})
        self._memory = list(memory or [])
        self.plan = plan or {}
        self.limit_max = limit_max
        self.outcome = outcome or {"success": True}
        self.http = http
        self.upload = upload
        self.image_sent = image_sent
        self.search_result = search_result
        self.repost = repost
        self.calls = []
        self.dynamic_ideas = DynamicIdeas.in_memory()
        now = time.time()
        for idea in ideas:
            idea = dict(idea)
            days_ago = idea.pop("days_ago", 0)
            self.dynamic_ideas.add(**idea, now=now - days_ago * 86400 - 60)
        self.event_runtime = types.SimpleNamespace(execute=self._execute)
        self._attach()

    def _attach(self):
        self.dynamic_log = DynamicLog(ActivityLog("dynamic", file=FakeFile(self, DYNAMIC_LOG_FILE)))

    async def _execute(self, request, handler):
        self.calls.append(["queue", request.key.rsplit(":", 1)[0], request.kind, request.event_key.rsplit(":", 1)[0],
                           request.target_id, request.metadata])
        if self.outcome.get("success"):
            await handler()
        return types.SimpleNamespace(success=self.outcome.get("success"), state=self.outcome.get("state", "committed"),
                                     reason=self.outcome.get("reason", ""))

    def _load_json(self, path, default=None):
        return copy.deepcopy(self.files.get(path, default))

    def _save_json(self, path, value):
        self.calls.append(["save", os.path.basename(path), len(value)])
        self.files[path] = copy.deepcopy(value)

    now_doing = part(LegacyNowDoing)


    def _set_cross_platform_activity(self, kind, stage, **details):
        self.calls.append(["doing", kind, stage, details])

    def _clear_cross_platform_activity(self, kind):
        self.calls.append(["done", kind])

    mood = part(LegacyMood)

    def _get_today_mood(self):
        return "开心", "天气好"

    def _autonomous_limit_max(self, kind):
        return self.limit_max

    def _autonomous_plan_for_today(self):
        return self.plan

    async def _get_system_prompt(self):
        return "系统提示词"

    async def _generation_llm_call(self, prompt, system_prompt=None, max_tokens=None):
        self.calls.append(["model", prompt, system_prompt, max_tokens])
        reply = self.replies.pop(0) if self.replies else SKIP
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def _llm_call(self, prompt, **_kwargs):
        raise AssertionError("有生成模型入口时不该走主模型")

    async def _web_search(self, query):
        self.calls.append(["search", query])
        return self.search_result

    async def _upload_image_to_bilibili(self, path):
        with open(path, "rb") as f:
            self.calls.append(["upload", re.sub(r"\d+", "N", os.path.basename(path)), f.read().decode("latin1")])
        return {"url": "uploaded"} if self.upload else None

    async def _post_dynamic_text(self, text):
        self.calls.append(["post_text", text])
        return True

    async def _get_video_oid(self, bvid):
        self.calls.append(["video_oid", bvid])
        return {"BV_R": 4321}.get(bvid)

    async def _repost_video(self, aid, text):
        self.calls.append(["repost", aid, text])
        return self.repost

    async def _post_dynamic_with_image(self, text, img_info, image_state=None):
        self.calls.append(["post_image", text, img_info])
        if image_state is not None:
            image_state["sent"] = self.image_sent
        return True

    async def _save_self_memory_record(self, key, text, source=None, memory_type=None, extra=None):
        self.calls.append(["memory", key, text, source, memory_type, *([extra] if extra else [])])


FAILS_TO_OPENAI = FakeResponse(status=500)
OPENAI_OK = FakeResponse(payload={"choices": [{"message": {"images": [{"image_url": {"url": PNG_URL}}]}}]})
NOVELAI_JSON = {"IMAGE_GEN_BACKEND": "novelai", "IMAGE_GEN_API_KEY": "t"}

SCENES = {
    "scheduled_skip": (dict(), {}),
    "scheduled_post": (dict(replies=[post()]), {}),
    "chance_zero_no_model": (dict(config={"DYNAMIC_POST_CHANCE": 0}), {}),
    "interval_not_due": (dict(config={"DYNAMIC_MIN_INTERVAL_DAYS": 3}, files={DYNAMIC_LOG_FILE: [{"time": stamp(2), "text": "旧"}]}), {}),
    "interval_passed": (dict(config={"DYNAMIC_MIN_INTERVAL_DAYS": 3}, replies=[post()], files={DYNAMIC_LOG_FILE: [{"time": stamp(3), "text": "旧"}]}), {}),
    "future_date_does_not_block": (dict(replies=[post()], files={DYNAMIC_LOG_FILE: [{"time": stamp(-5), "text": "未来"}]}), {}),
    "summaries_do_not_use_interval": (dict(replies=[post()], files={DYNAMIC_LOG_FILE: [{"time": stamp(), "text": "周报", "weekly": True}, {"time": stamp(), "text": "日报", "daily_summary": True}]}), {}),
    "daily_limit_reached": (dict(config={"DYNAMIC_DAILY_COUNT": 1}, files={DYNAMIC_LOG_FILE: [{"time": stamp(), "text": "今天"}]}), {"human_initiated": True}),
    "autonomous_limit_lower": (dict(limit_max=1, files={DYNAMIC_LOG_FILE: [{"time": stamp(), "text": "今天"}]}), {"human_initiated": True}),
    "plan_limit_only_scheduled": (dict(plan={"dynamic_times": ["10:00"]}, config={"DYNAMIC_MIN_INTERVAL_DAYS": 1}, files={DYNAMIC_LOG_FILE: [{"time": stamp(), "text": "今天"}]}), {}),
    "plan_limit_ignored_by_owner": (dict(plan={"dynamic_times": ["10:00"]}, replies=[post()], files={DYNAMIC_LOG_FILE: [{"time": stamp(), "text": "今天"}]}), {"human_initiated": True}),
    "limit_zero_means_no_limit": (dict(config={"DYNAMIC_DAILY_COUNT": 0}, replies=[post()], files={DYNAMIC_LOG_FILE: [{"time": stamp(), "text": "今天"}]}), {"human_initiated": True}),
    "owner_skips_interval_and_chance": (dict(config={"DYNAMIC_POST_CHANCE": 0, "DYNAMIC_MIN_INTERVAL_DAYS": 3}, replies=[post()], files={DYNAMIC_LOG_FILE: [{"time": stamp(1), "text": "旧"}]}), {"human_initiated": True}),
    # 看完一轮：池子里的想法交给模型挑（编号 1 是这一轮刚放进去的）
    "round_post": (dict(ideas=[IDEA], replies=[post(idea=1)]), {"fresh": (1,)}),
    "round_ignores_chance": (dict(config={"DYNAMIC_POST_CHANCE": 0}, ideas=[IDEA], replies=[post(idea=1)]), {"fresh": (1,)}),
    "round_already_posted": (dict(ideas=[IDEA], files={DYNAMIC_LOG_FILE: [{"time": stamp(5), "text": "旧", "bvid": "BV1"}]}), {"fresh": (1,)}),
    "round_skip_keeps_idea": (dict(ideas=[IDEA], replies=[SKIP_IDEA]), {"fresh": (1,)}),
    "round_daily_limit_keeps_idea": (dict(config={"DYNAMIC_DAILY_COUNT": 1}, ideas=[IDEA], files={DYNAMIC_LOG_FILE: [{"time": stamp(), "text": "今天"}]}), {"fresh": (1,)}),
    "round_writes_something_else": (dict(ideas=[IDEA], replies=[post(idea=0)]), {"fresh": (1,)}),
    "round_picks_older_idea": (dict(ideas=[OLD_IDEA, IDEA], replies=[post(idea=1)]), {"fresh": (2,)}),
    "round_unknown_idea_refused": (dict(ideas=[IDEA], replies=[post(idea=9)]), {"fresh": (1,)}),
    "round_missing_idea_field_refused": (dict(ideas=[IDEA], replies=[post()]), {"fresh": (1,)}),
    "round_bangumi_idea": (dict(ideas=[BANGUMI_IDEA], replies=[post(idea=1)]), {"fresh": (1,)}),
    "round_cover": (dict(ideas=[IDEA], replies=[post(image="video_cover", idea=1)], http=FakeResponse(body=b"cover")), {"fresh": (1,)}),
    "round_cover_without_idea_refused": (dict(ideas=[IDEA], replies=[post(image="video_cover", idea=0)]), {"fresh": (1,)}),
    "round_cover_http_fails": (dict(ideas=[IDEA], replies=[post(image="video_cover", idea=1)], http=FakeResponse(status=404)), {"fresh": (1,)}),
    "round_cover_raises": (dict(ideas=[IDEA], replies=[post(image="video_cover", idea=1)], http=RuntimeError("断了")), {"fresh": (1,)}),
    "round_cover_empty": (dict(ideas=[IDEA], replies=[post(image="video_cover", idea=1)], http=FakeResponse(body=b"")), {"fresh": (1,)}),
    "round_cover_not_bilibili": (dict(ideas=[{**IDEA, "pic": "https://evil.test/a.jpg"}], replies=[post(image="video_cover", idea=1)]), {"fresh": (1,)}),
    "round_cover_protocol_relative": (dict(ideas=[{**IDEA, "pic": "//i0.hdslb.com/a.jpg"}], replies=[post(image="video_cover", idea=1)], http=FakeResponse(body=b"cover")), {"fresh": (1,)}),
    "round_cover_refused_without_pic": (dict(ideas=[{**IDEA, "pic": ""}], replies=[post(image="video_cover", idea=1)]), {"fresh": (1,)}),
    # 转发：转发视频配一句话
    "repost": (dict(ideas=[REPOST_IDEA], replies=[post("一定要看到最后", idea=1)]), {"fresh": (1,)}),
    "repost_ignores_image": (dict(ideas=[REPOST_IDEA], replies=[post("一定要看到最后", image="video_cover", idea=1)]), {"fresh": (1,)}),
    # 这个假运行时不看处理函数的返回值；真的运行时把 False 记成 handler_returned_unsuccessful。
    "repost_rejected_drops_idea": (dict(ideas=[REPOST_IDEA], replies=[post("一定要看到最后", idea=1)], repost=False,
                                        outcome={"success": False, "state": "failed", "reason": "handler_returned_unsuccessful"}), {"fresh": (1,)}),
    "repost_budget_keeps_idea": (dict(ideas=[REPOST_IDEA], replies=[post("一定要看到最后", idea=1)], outcome={"success": False, "state": "skipped", "reason": "budget_exhausted:day"}), {"fresh": (1,)}),
    "repost_without_aid": (dict(ideas=[{**REPOST_IDEA, "key": "BV_NOAID"}], replies=[post("一定要看到最后", idea=1)]), {"fresh": (1,)}),
    # 定时时刻：池子里有想法就不掷发布意愿
    "scheduled_pool_skips_chance": (dict(config={"DYNAMIC_POST_CHANCE": 0}, ideas=[OLD_IDEA], replies=[post(idea=1)]), {}),
    "scheduled_pool_still_needs_interval": (dict(config={"DYNAMIC_MIN_INTERVAL_DAYS": 3}, ideas=[OLD_IDEA], files={DYNAMIC_LOG_FILE: [{"time": stamp(1), "text": "旧"}]}), {}),
    "owner_sees_pool": (dict(ideas=[OLD_IDEA], replies=[post(idea=0)]), {"human_initiated": True}),
    "bad_json": (dict(replies=["不是 JSON"]), {}),
    "blank_reply": (dict(replies=[""]), {}),
    "model_raises": (dict(replies=[RuntimeError("炸了")]), {}),
    "search_then_post": (dict(config=SEARCH_CONFIG, replies=[SEARCH, post()]), {}),
    "search_empty_result": (dict(config=SEARCH_CONFIG, replies=[SEARCH, post()], search_result=""), {}),
    "search_capped": (dict(config={**SEARCH_CONFIG, "DYNAMIC_WEB_SEARCH_MAX": 1}, replies=[SEARCH, SEARCH]), {}),
    "search_offered_only_when_on": (dict(replies=[SEARCH]), {}),
    "generate_without_key_means_no_image": (dict(replies=[post(image="generate", prompt="一只猫")]), {}),
    "generate_incomplete_config": (dict(config={"IMAGE_GEN_API_KEY": "k"}, replies=[post(image="generate", prompt="一只猫")]), {}),
    "generate_ok": (dict(config=IMAGE_CONFIG, replies=[post(image="generate", prompt="一只猫")], http=OPENAI_OK), {}),
    "generate_http_fails_posts_text": (dict(config=IMAGE_CONFIG, replies=[post(image="generate", prompt="一只猫")], http=FAILS_TO_OPENAI), {}),
    "generate_api_error": (dict(config=IMAGE_CONFIG, replies=[post(image="generate", prompt="一只猫")], http=FakeResponse(payload={"error": "no"})), {}),
    "generate_image_in_content": (dict(config=IMAGE_CONFIG, replies=[post(image="generate", prompt="一只猫")], http=FakeResponse(payload={"choices": [{"message": {"content": f"![]({PNG_URL})"}}]})), {}),
    "generate_returns_nothing": (dict(config=IMAGE_CONFIG, replies=[post(image="generate", prompt="一只猫")], http=FakeResponse(payload={"choices": [{"message": {}}]})), {}),
    "generate_raises": (dict(config=IMAGE_CONFIG, replies=[post(image="generate", prompt="一只猫")], http=RuntimeError("断了")), {}),
    "upload_fails_posts_text": (dict(config=IMAGE_CONFIG, replies=[post(image="generate", prompt="一只猫")], http=OPENAI_OK, upload=False), {}),
    "image_post_degraded_to_text": (dict(config=IMAGE_CONFIG, replies=[post(image="generate", prompt="一只猫")], http=OPENAI_OK, image_sent=False), {}),
    "novelai_scheduled_has_no_image": (dict(config=NOVELAI_JSON, replies=[post(image="generate", prompt="一只猫")]), {}),
    "novelai_owner_json": (dict(config=NOVELAI_JSON, replies=[post(image="generate", prompt="一只猫")], http=FakeResponse(body=json.dumps({"images": [base64.b64encode(PNG).decode()]}).encode(), content_type="application/json")), {"human_initiated": True}),
    "novelai_owner_http_error": (dict(config=NOVELAI_JSON, replies=[post(image="generate", prompt="一只猫")], http=FakeResponse(status=402, body=b"pay")), {"human_initiated": True}),
    "novelai_owner_empty": (dict(config=NOVELAI_JSON, replies=[post(image="generate", prompt="一只猫")], http=FakeResponse(body=b"?", content_type="text/plain")), {"human_initiated": True}),
    "budget_exhausted": (dict(replies=[post()], outcome={"success": False, "state": "skipped", "reason": "budget_exhausted:day"}), {}),
    "result_unknown_not_recorded": (dict(replies=[post()], outcome={"success": False, "state": "unknown"}), {}),
    "refused_not_recorded": (dict(replies=[post()], outcome={"success": False, "state": "failed"}), {}),
    "prompt_has_everything": (dict(
        config={"DYNAMIC_TOPICS": ["话题一", " "], "DYNAMIC_PROMPT": "短句", "CUSTOM_DYNAMIC_INSTRUCTION": "别太长"},
        replies=[post()],
        files={
            PERMANENT_MEMORY_FILE: [{"text": "我喜欢猫"}],
            DYNAMIC_LOG_FILE: [{"time": stamp(9), "text": "长" * 60}, {"time": stamp(9), "text": "短"}],
            WATCH_LOG_FILE: [
                {"time": stamp(1), "title": "木工", "review": "榫头没讲清楚", "content_summary": "做椅子"},
                {"time": stamp(1), "title": "只有标题", "review": "评价失败"},
                {"time": stamp(1), "title": "元数据", "review": "推测", "analysis_basis": "metadata"},
                {"time": stamp(30), "title": "太旧", "review": "旧的"},
            ],
            DAILY_SUMMARY_FILE: [{"date": stamp(1), "structured": {"video_highlights": [{"title": "日报视频", "review": "不错"}]}}],
            WEEKLY_SUMMARY_FILE: [{"time": stamp(2), "structured": {"bangumi_highlights": [{"title": "周报番", "summary": "好看"}]}}],
        },
        memory=[
            {"time": stamp(1), "user_id": "self", "memory_type": "video", "source": "bilibili", "text": "公开观点"},
            {"time": stamp(1), "user_id": "42", "memory_type": "chat", "text": "主人秘密"},
            {"time": stamp(1), "user_id": "self", "memory_type": "video", "source": "qq", "text": "QQ秘密"},
            {"time": stamp(1), "user_id": "self", "memory_type": "dynamic", "thread_id": "dynamic", "text": "发过的动态"},
            {"time": stamp(1), "user_id": "self", "memory_type": "dynamic", "rpid": "dynamic_1", "text": "发过的动态二"},
        ],
    ), {}),
    "log_is_capped": (dict(replies=[post()], files={DYNAMIC_LOG_FILE: [{"time": stamp(9), "text": str(i)} for i in range(100)]}), {}),
}

HANDOFFS = {
    "taken": (dict(ideas=[IDEA], replies=[post(idea=1)]), {}),
    "dynamics_off": (dict(config={"ENABLE_DYNAMIC": False}, ideas=[IDEA]), {}),
    "from_video_off": (dict(config={"DYNAMIC_FROM_VIDEO": False}, ideas=[IDEA]), {}),
}


def _normalize(value):
    text = json.dumps(value, ensure_ascii=False, default=lambda o: f"<{type(o).__name__}>")
    text = re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", "<TIME>", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}", "<DATE>", text)
    text = re.sub(r"\d{2}-\d{2}看的", "<MM-DD>看的", text)
    text = re.sub(r"现在是(深夜/凌晨|上午|下午|晚上)", "现在是<时段>", text)
    return json.loads(text)


class DynamicPostSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def _scene(self, script, call):
        probe = Probe(**copy.deepcopy({k: v for k, v in script.items() if k != "http"}), http=script.get("http"))
        with tempfile.TemporaryDirectory() as folder, \
                patch(f"{IMAGE_MODULE}.plugin_http_session", lambda _config: FakeSession(probe)), \
                patch(f"{IMAGE_MODULE}.TEMP_IMAGE_DIR", folder), \
                patch("core.features.dynamic_post.random", random.Random(1)):
            try:
                outcome = {"result": await call(probe)}
            except Exception as exc:
                outcome = {"raises": type(exc).__name__}
            leftovers = os.listdir(folder)
        log = probe.files.get(DYNAMIC_LOG_FILE, [])
        pool = [[item["id"], item["key"], item["state"], item["reason"]] for item in probe.dynamic_ideas.all()]
        return _normalize({**outcome, "calls": probe.calls, "log_tail": log[-2:], "log_size": len(log),
                           "leftover_images": leftovers, "pool": pool})

    async def test_matches_snapshot(self):
        results = {}
        for name, (script, kwargs) in SCENES.items():
            results[f"run/{name}"] = await self._scene(script, lambda p, k=kwargs: p.dynamic_post.run(**k))
        for name, (script, _) in HANDOFFS.items():
            handoff = types.SimpleNamespace(fresh=(1,))
            results[f"handoff/{name}"] = await self._scene(script, lambda p: p.dynamic_post.take_video_handoff(handoff))

        async def queued(probe):
            return (await probe.dynamic_post.queue_post("周报", probe.bili_dynamics._post_dynamic_text and (lambda: probe.bili_dynamics._post_dynamic_text("周报")), with_outcome=True)).success

        results["queue/with_outcome"] = await self._scene(dict(), queued)
        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS") or not SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(results, json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
