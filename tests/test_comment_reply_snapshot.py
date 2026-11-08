"""Behaviour snapshots for the comment → reply pipeline.

Each scenario runs ``_poll_unified`` against fake Bilibili notifications, a
scripted model and fake memory/profile collaborators, while the pipeline code
in between (filters, the tool lookups, the strict reply protocol,
affection, milestones, block list, security log, the event runtime) is real.
Every external call, every prompt and every data file written is recorded in
order and must match ``snapshots/comment_reply_flows.json`` exactly, so the
pipeline can be restructured without silently changing what the bot does.

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_comment_reply_snapshot.py
"""

import copy
import json
import os
import random
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

import core.features.comment_reply as comment_reply_module
import core.security.log as security_log_module
from core.infra.bili.comments import BiliComments
from core.config import BILI_AT_NOTIFY_URL, BILI_NOTIFY_URL
from core.domain.affection import Affection, Milestones
from core.domain.blocklist import BlockList
from core.features.comment_context import CommentContext
from vision_fakes import vision_on
from itself import itself
from parts import part
from people_fakes import LegacyMood, LegacyProfiles, LegacyUsers
from people_parts import PeopleParts
from reply_parts import ReplyParts
from tool_fakes import WithToolFakes, call
from core.runtime import EventRuntime
from core.security.log import SecurityLog
from core.storage.activity_log import ActivityLog
from core.storage.json_file import JsonFile

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "comment_reply_flows.json"
NOW = datetime(2026, 10, 25, 14, 30)
DOMAIN_FILES = ("affection.json", "milestones.json", "block_log.json", "block_count.json", "security_log.json")

BASE_CONFIG = {
    "OWNER_MID": "777",
    "OWNER_NAME": "小柠",
    "DEDE_USER_ID": "999",
    "ENABLE_AFFECTION": True,
    "ENABLE_AUTO_BLOCK": True,
    "AUTO_BLOCK_SCORE": -10,
    "AUTO_BLOCK_NEGATIVE_TIMES": 3,
    "ENABLE_INTEREST_BASED_REPLY": True,
    "REPLY_PROBABILITY_PERCENT": 100,
    "ENABLE_SIMILAR_SKIP": False,
    "COMMENT_CONTEXT_MAX_CALLS": 2,
    "ENABLE_WEB_SEARCH": False,
    "REPLY_COOLDOWN": 5,
}


from reply_modules import patched_in_reply_modules
from reply_owners import attach_reply_owners

class _CannotWrite:
    def append(self, entry):
        raise AttributeError("_append_json_list")


def _SnapshotActivityLogs(bot):
    from activity_parts import HostActivityLogs
    logs = HostActivityLogs(bot)
    logs.comment_context = _CannotWrite()
    return logs


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(NOW.year, NOW.month, NOW.day, NOW.hour, NOW.minute)


def notice(rpid, mid, name, content, oid=100, root=None):
    return {
        "user": {"mid": mid, "nickname": name},
        "item": {"source_id": rpid, "source_content": content, "subject_id": oid,
                 "business_id": 1, "root_id": root or rpid},
    }


def at_notice(at_id, rpid, mid, name, content, oid=100):
    return {
        "id": at_id,
        "user": {"mid": mid, "nickname": name},
        "item": {"source_id": rpid, "source_content": content, "subject_id": oid,
                 "business_id": 1, "root_id": rpid},
    }


def reply_json(reply="收到啦", decision="reply", score_delta=1, tool=None, **extra):
    payload = {
        "decision": decision,
        "reply": reply if decision == "reply" else "",
        "score_delta": score_delta if decision == "reply" else 0,
        "impression": extra.pop("impression", ""),
        "user_facts": extra.pop("user_facts", []),
        "signals": {
            "interaction_type": "normal", "feedback_type": "none", "attack_level": 0,
            "feedback_topic": None, "reflection_candidate": None, "confidence": 0.9,
        },
        "tool_request": tool or {"name": "none", "query": ""},
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def interest(value, reason="有交流空间"):
    return json.dumps({"reply": value, "reason": reason}, ensure_ascii=False)


class ReplyProbe(PeopleParts, ReplyParts):
    """Real pipeline; fake Bilibili, model and the memory/profile modules it calls."""

    _strip_at_prefix = BiliComments._strip_at_prefix
    vision = property(vision_on)
    models = itself
    abuse_alert = itself
    # 录快照时这个假插件只有 _load_json / _save_json，没有 _append_json_list（评论资料日志写失败被吞掉），保持原样
    json_store = itself
    # 活动日志照旧读写上面的 json；评论资料日志照录快照时那样写不进去
    activity_logs = part(lambda bot: _SnapshotActivityLogs(bot))
    mood = part(LegacyMood)
    user_profiles = part(LegacyProfiles)
    bili_users = part(LegacyUsers)

    def __init__(self, directory, config, *, replies=(), ats=(), llm=(), first_poll=False,
                 scores=None, blocked=(), send_ok=True, images=()):
        self.config = {**BASE_CONFIG, **config}
        self.calls = []
        self.files = {}
        self.directory = directory
        self.comment_context = CommentContext(self)
        for name, content in (("affection.json", scores or {}),
                              ("block_log.json", {uid: {"reason": "旧账"} for uid in blocked})):
            JsonFile(Path(directory) / name).write(content)
        self.affection = Affection(JsonFile(Path(directory) / "affection.json"), self.roles.is_owner)
        self.affection.pin_owner(self.config["OWNER_MID"])  # as main.py does at startup
        self.milestones = Milestones(JsonFile(Path(directory) / "milestones.json"))
        self.blocklist = BlockList(JsonFile(Path(directory) / "block_log.json"),
                                   JsonFile(Path(directory) / "block_count.json"))
        self.security_log = SecurityLog(ActivityLog("security", file=JsonFile(Path(directory) / "security_log.json")))
        self.event_runtime = EventRuntime()
        attach_reply_owners(self)
        self.comment_reply._first_poll = first_poll
        self.notifications = {BILI_NOTIFY_URL: list(replies), BILI_AT_NOTIFY_URL: list(ats)}
        self.llm_queue = list(llm)
        self.send_ok = send_ok
        self.images = list(images)

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

    def snapshot_files(self):
        files = dict(self.files)
        # The pipeline saves these from sets, so their order is not meaningful.
        for name in ("replied.json", "replied_at.json"):
            if name in files:
                files[name] = sorted(files[name])
        for name in DOMAIN_FILES:
            path = Path(self.directory) / name
            if path.exists():
                files[name] = json.loads(path.read_text(encoding="utf-8"))
        return files

    # ── Bilibili ──
    async def _http_get(self, url, params=None, **_kwargs):
        self.record("http_get", url.rsplit("/", 1)[-1])
        return {"code": 0, "data": {"items": self.notifications.get(url, [])}}, None

    async def _get_comment_images(self, oid, rpid, comment_type):
        self.record("get_comment_images", str(rpid))
        return list(self.images)

    async def _recognize_images(self, image_urls):
        self.record("recognize_images", list(image_urls))
        return "一张猫猫图"

    async def _oid_to_bvid(self, oid):
        return f"BV{oid}"

    async def _send_reply(self, oid, rpid, reply_type, content):
        self.record("send_reply", str(rpid), content)
        return self.send_ok

    async def _block_user(self, mid):
        self.record("block_user", mid)
        return True

    # ── model ──
    async def _llm_call(self, prompt, system_prompt="", max_tokens=None, **_kwargs):
        self.record("llm", prompt)
        return self.llm_queue.pop(0) if self.llm_queue else None

    async def tool_loop(self, prompt, system_prompt="", *, event=None, tools=(), max_steps=None, **_kwargs):
        """AstrBot 的工具循环：队列里的 ``call(...)`` 是模型先调的工具（真的跑），字符串是最后的回复。"""
        by_name = {tool.name: tool for tool in tools}
        self.record("tool_loop", prompt, sorted(by_name), max_steps)
        while self.llm_queue:
            step = self.llm_queue.pop(0)
            if not isinstance(step, dict):
                return step
            text = await by_name[step["call"]].handler(event, **step["args"])
            self.record("tool_result", step["call"], text)
        return None

    async def _get_system_prompt(self):
        return "【人设】"

    async def _get_embedding(self, text):
        return None

    # ── other modules the pipeline reads from or commits to ──
    def _get_today_mood(self):
        return "🌙 平静如常", "按正常性格回复。"

    def _get_festival_prompt(self):
        return ""

    personality = types.SimpleNamespace(prompt=lambda context="": "")

    async def _build_memory_context(self, thread_id, user_id, query_text, oid=0, comment_type=1, channel="comment"):
        self.record("build_memory_context", str(thread_id), str(user_id), channel)
        return "【记忆】上次聊过手绘"

    def _record_relationship_interaction(self, mid, username, score_delta, source):
        self.record("relationship", str(mid), score_delta, source)

    def _update_user_profile(self, mid, **kwargs):
        self.record("update_profile", str(mid), sorted(k for k, v in kwargs.items() if v))

    def _note_profile_candidate(self, uid, scope, impression, facts):
        self.record("profile_candidate", str(uid), scope, impression, facts)
        return 0

    async def _save_memory_record(self, rpid, thread_id, user_id, username, content, reply_text, **kwargs):
        self.record("save_memory", str(rpid), reply_text)

    async def _compress_thread_memory(self, thread_id):
        self.record("compress_thread", str(thread_id))

    async def _compress_oid_memory(self, oid):
        self.record("compress_oid", str(oid))

    async def _compress_user_memory(self, user_id, username, memory_scope="bili_comment"):
        self.record("compress_user", str(user_id))

    async def _check_abuse_alert(self, **kwargs):
        self.record("abuse_alert_check", kwargs.get("mid"), kwargs.get("score_delta"))


SCENARIOS = {
    # Startup: everything pending is marked read and nothing is answered.
    "first_poll_marks_read": dict(
        first_poll=True,
        replies=[notice("r1", 11, "甲", "你好呀"), notice("r2", 12, "乙", "在吗")],
    ),
    # Owner bypasses filters; normal reply with all commits.
    "owner_reply": dict(
        replies=[notice("r1", 777, "小柠", "今天看了什么呀")],
        llm=[reply_json("看了一个手绘动画！", impression="主人", user_facts=["喜欢手绘"])],
    ),
    # Emotes on: the prompt lists them; only the first listed code survives sending.
    "emote_reply_is_tidied": dict(
        config={"ENABLE_BILI_EMOTES": True},
        replies=[notice("r1", 777, "小柠", "今天看了什么呀")],
        llm=[reply_json("看了一个手绘动画[doge][鸣潮_好呀][吃瓜]")],
    ),
    # Only the highest-priority comment is handled per poll.
    "one_comment_per_poll": dict(
        replies=[notice("r1", 11, "甲", "这个动画的配色好舒服啊"), notice("r2", 777, "小柠", "在吗")],
        llm=[reply_json("嗯嗯在的")],
    ),
    # @ mentions are always answered.
    "at_mention": dict(
        ats=[at_notice("a1", "r9", 13, "丙", "@Bot 你觉得这集怎么样")],
        llm=[reply_json("挺好看的")],
    ),
    # Normal user: interest filter says no.
    "interest_skip": dict(
        replies=[notice("r1", 11, "甲", "我觉得这期一般般吧没什么意思")],
        llm=[interest(False, "没有交流空间")],
    ),
    # Normal user: probability roll fails.
    "probability_skip": dict(
        config={"REPLY_PROBABILITY_PERCENT": 1},
        replies=[notice("r1", 11, "甲", "这个转场是怎么做的呀")],
        llm=[interest(True)],
    ),
    # Low-value filter (too short) before the model is asked anything.
    "low_value_filtered": dict(
        replies=[notice("r1", 11, "甲", "1")],
    ),
    # Model decides to stay silent.
    "model_ignores": dict(
        replies=[notice("r1", 11, "甲", "这个转场是怎么做的呀")],
        llm=[interest(True), reply_json(decision="ignore")],
    ),
    # Model output fails the strict protocol: nothing is sent.
    "invalid_model_output": dict(
        replies=[notice("r1", 11, "甲", "这个转场是怎么做的呀")],
        llm=[interest(True), "不是 JSON"],
    ),
    # Model calls the video description tool first, then replies.
    "context_request_then_reply": dict(
        replies=[notice("r1", 11, "甲", "这期讲的是什么呀")],
        llm=[interest(True), call("video_description"), reply_json("讲的是手绘流程")],
    ),
    # Crossing 10 points: the model's reply is replaced by the milestone line.
    "milestone_replaces_reply": dict(
        scores={"11": 9},
        replies=[notice("r1", 11, "甲", "一直在看你的视频！")],
        llm=[interest(True), reply_json("谢谢支持", score_delta=2)],
    ),
    # Score falls under the auto-block line: notice, block, no normal commits.
    "auto_block": dict(
        scores={"11": -8},
        replies=[notice("r1", 11, "甲", "这期做得真的不太行啊")],
        llm=[interest(True), reply_json("……", score_delta=-5)],
    ),
    # Sending fails: no affection, memory or profile changes.
    "send_fails": dict(
        send_ok=False,
        replies=[notice("r1", 777, "小柠", "在吗在吗")],
        llm=[reply_json("在的")],
    ),
    # Blocked user and blocked keyword are ignored before the model.
    "blocked_user": dict(
        blocked=["11"],
        replies=[notice("r1", 11, "甲", "我又来了哦哦哦")],
    ),
    # Comment with an image: recognised and passed into the prompt.
    "comment_with_image": dict(
        images=["https://i0.hdslb.com/cat.jpg"],
        replies=[notice("r1", 777, "小柠", "看我家猫")],
        llm=[reply_json("好可爱！")],
    ),
}


class CommentReplySnapshotTests(WithToolFakes, unittest.IsolatedAsyncioTestCase):
    async def run_scenario(self, name):
        spec = dict(SCENARIOS[name])
        config = spec.pop("config", {})
        with tempfile.TemporaryDirectory() as directory:
            probe = ReplyProbe(directory, config, **spec)
            probe.bili_videos._get_video_info = self._fake_video_info(probe)
            with patched_in_reply_modules(datetime=FixedDatetime, random=random.Random(20261025)), \
                    patch.object(security_log_module, "datetime", FixedDatetime):
                await probe.comment_reply._poll_unified()
            return json.loads(json.dumps(
                {"calls": probe.calls, "files": probe.snapshot_files()}, ensure_ascii=False, default=sorted
            ))

    @staticmethod
    def _fake_video_info(probe):
        async def get_video_info(oid):
            probe.record("get_video_info", str(oid))
            return {"bvid": f"BV{oid}", "title": "手绘动画", "desc": "一段手绘过程", "duration": 300, "cid": 1}
        return get_video_info

    async def test_each_turn_logs_the_path_it_took(self):
        expected = {
            "owner_reply": "新评论 → 必回 → 生成:reply → 已发送",
            "interest_skip": "新评论 → 普通 → 忽略:interest_skip",
            "invalid_model_output": "新评论 → 普通 → 失败:invalid_model_output",
            "send_fails": "新评论 → 必回 → 生成:reply → 未发送",
            "comment_with_image": "新评论 → 必回 → 看图 → 生成:reply → 已发送",
        }
        for name, path in expected.items():
            with self.subTest(scenario=name), patch.object(comment_reply_module, "logger") as logger:
                await self.run_scenario(name)
                lines = [str(call.args[0]) for call in logger.info.call_args_list if "🧭" in str(call.args[0])]
                self.assertEqual(lines, [f"[BiliBot] 🧭 评论 r1：{path}"])

    async def test_flows_match_snapshot(self):
        recorded = {name: await self.run_scenario(name) for name in SCENARIOS}
        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS"):
            SNAPSHOT_FILE.parent.mkdir(exist_ok=True)
            SNAPSHOT_FILE.write_text(json.dumps(recorded, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        expected = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        self.maxDiff = None
        self.assertEqual(sorted(recorded), sorted(expected))
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                self.assertEqual(recorded[name], expected[name])


if __name__ == "__main__":
    unittest.main()
