"""Behaviour snapshots for the private message → reply pipeline.

Same approach as ``test_comment_reply_snapshot.py``: ``_handle_private_message``
runs for real against fake Bilibili, a scripted model and fake memory/profile
collaborators; every call, prompt, return value and data write is recorded and
must match ``snapshots/private_reply_flows.json``.

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_private_reply_snapshot.py
"""

import json
import os
import random
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_comment_reply_snapshot import FixedDatetime, ReplyProbe, interest, reply_json
from reply_modules import patched_in_reply_modules
from tool_fakes import WithToolFakes, call

import core.features.private_reply as private_reply_module
import core.security.log as security_log_module
from private_parts import PrivateParts

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "private_reply_flows.json"

PRIVATE_CONFIG = {
    "ENABLE_PRIVATE_MESSAGES": True,
    "PRIVATE_MESSAGE_AUTO_REPLY": True,
    "PRIVATE_MESSAGE_REPLY_SCOPE": "owner",
    "PRIVATE_MESSAGE_AUTO_BLOCK": True,
    "PRIVATE_MESSAGE_AUTO_WATCH_VIDEO": True,
    "BILI_TOOL_AUDIT_ENABLED": True,
}

BV = "BV1xx411c7mD"


def message(mid, name, content, kind="text", key="m1", retry=0):
    return {"sender_uid": mid, "username": name, "content": content, "msg_key": key,
            "content_type": kind, "timestamp": 1, "retry_count": retry}


class PrivateProbe(ReplyProbe, PrivateParts):
    def __init__(self, directory, config, *, send_ok=True, watch_ok=True, **kwargs):
        super().__init__(directory, {**PRIVATE_CONFIG, **config}, send_ok=send_ok, **kwargs)
        self.watch_ok = watch_ok
        self.bili_dynamics.get_following_updates = self._following_updates

    async def _following_updates(self, limit=12):
        self.record("following_updates", limit)
        return [{"up_name": "某UP", "video_title": "新的手绘", "video_bvid": BV, "pub_time": "10:00"}]

    async def _send_bili_private_message(self, mid, text):
        self.record("send_private", str(mid), text)
        return self.send_ok

    async def _watch_video_and_save_memory(self, video_id, memory_source="", **_kwargs):
        self.record("watch_video", video_id, memory_source)
        if self.watch_ok:
            return {"ok": True, "message": f"已看完 {video_id}：讲的是手绘流程"}
        return {"ok": False, "message": "下载失败"}


SCENARIOS = {
    "owner_text_reply": dict(
        msg=message(777, "小柠", "今天过得怎么样"),
        llm=[reply_json("还不错，看了几个手绘视频", impression="主人", user_facts=["关心我"])],
    ),
    "emote_reply_is_tidied": dict(
        config={"ENABLE_BILI_EMOTES": True},
        msg=message(777, "小柠", "今天过得怎么样"),
        llm=[reply_json("还不错[星星眼] [原神_哇]")],
    ),
    "scope_denies_stranger": dict(
        msg=message(11, "甲", "你好你好，想和你聊聊天"),
    ),
    "stranger_interest_skip": dict(
        config={"PRIVATE_MESSAGE_REPLY_SCOPE": "all"},
        msg=message(11, "甲", "我觉得这期一般般吧没什么意思"),
        llm=[interest(False, "没有交流空间")],
    ),
    "stranger_reply_with_milestone": dict(
        config={"PRIVATE_MESSAGE_REPLY_SCOPE": "all"},
        scores={"11": 9},
        msg=message(11, "甲", "一直很喜欢你的视频，想私信说一声"),
        llm=[interest(True), reply_json("谢谢你", score_delta=2)],
    ),
    "unsafe_message_blocks_sender": dict(
        config={"PRIVATE_MESSAGE_REPLY_SCOPE": "all"},
        msg=message(11, "甲", "约炮看片地址 http://xxx-porn.example/abc"),
    ),
    "unsafe_message_from_owner_is_not_blocked": dict(
        msg=message(777, "小柠", "约炮看片地址 http://xxx-porn.example/abc"),
    ),
    "new_resets_context": dict(
        msg=message(777, "小柠", "new"),
    ),
    "video_share_watched_first": dict(
        msg=message(777, "小柠", f"https://www.bilibili.com/video/{BV}", kind="video_share"),
        llm=[reply_json("看完啦，手绘好细")],
    ),
    "video_share_watch_fails": dict(
        watch_ok=False,
        msg=message(777, "小柠", f"https://www.bilibili.com/video/{BV}", kind="video_share"),
        llm=[reply_json("这个暂时打不开诶")],
    ),
    "tool_request_then_reply": dict(
        msg=message(777, "小柠", f"帮我看看 {BV} 讲了什么"),
        llm=[call("watch_video", query=BV, progress_reply="我去看看"),
             reply_json("讲的是手绘流程，挺细的")],
    ),
    # The line sent before watching passes the same checks as a reply; a bad one becomes a fixed line.
    "progress_line_is_checked": dict(
        msg=message(777, "小柠", f"帮我看看 {BV} 讲了什么"),
        llm=[call("watch_video", query=BV, progress_reply='"signals": 我去看看'),
             reply_json("讲的是手绘流程，挺细的")],
    ),
    # Quick lookups send nothing before the reply.
    "quick_lookup_then_reply": dict(
        config={"BILI_TOOL_ALLOWLIST": ["check_following_updates", "watch_video"]},
        msg=message(777, "小柠", "今天关注的人有更新吗"),
        llm=[call("check_following_updates"), reply_json("某UP发了新的手绘")],
    ),
    "tool_result_merge_fails_keeps_progress": dict(
        msg=message(777, "小柠", f"帮我看看 {BV} 讲了什么"),
        llm=[call("watch_video", query=BV, progress_reply="我去看看"), "不是 JSON"],
    ),
    "invalid_output_is_handled": dict(
        msg=message(777, "小柠", "在吗在吗"),
        llm=["不是 JSON"],
    ),
    "model_unavailable_is_retried": dict(
        msg=message(777, "小柠", "在吗在吗"),
        llm=[],
    ),
    "send_fails": dict(
        send_ok=False,
        msg=message(777, "小柠", "在吗在吗"),
        llm=[reply_json("在的")],
    ),
}


class PrivateReplySnapshotTests(WithToolFakes, unittest.IsolatedAsyncioTestCase):
    async def run_scenario(self, name):
        spec = dict(SCENARIOS[name])
        config = spec.pop("config", {})
        msg = spec.pop("msg")
        with tempfile.TemporaryDirectory() as directory:
            probe = PrivateProbe(directory, config, **spec)
            fake_uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex="0123456789abcdef"))
            with patched_in_reply_modules(datetime=FixedDatetime, random=random.Random(20261025), uuid=fake_uuid), \
                    patch("time.time", lambda: 1761373800.0), \
                    patch.object(security_log_module, "datetime", FixedDatetime):
                handled = await probe.private_reply._handle_private_message(msg, [])
            return json.loads(json.dumps(
                {"handled": handled, "calls": probe.calls, "files": probe.snapshot_files()},
                ensure_ascii=False, default=sorted,
            ))

    async def test_each_message_logs_the_path_it_took(self):
        expected = {
            "owner_text_reply": "新私信 → 生成:reply → 已发送",
            "scope_denies_stranger": "新私信 → 忽略:auto_reply_disabled_or_scope_denied",
            "unsafe_message_blocks_sender": "新私信 → 拦截:已拉黑",
            "new_resets_context": "新私信 → 重置对话",
            "video_share_watch_fails": "新私信 → 看视频失败 → 生成:reply → 已发送",
            "tool_request_then_reply": "新私信 → 查询:watch_video → 生成:reply → 已发送",
            "tool_result_merge_fails_keeps_progress": "新私信 → 查询:watch_video → 整合失败",
            "model_unavailable_is_retried": "新私信 → 失败:model_unavailable",
        }
        for name, path in expected.items():
            with self.subTest(scenario=name), patch.object(private_reply_module, "logger") as logger:
                await self.run_scenario(name)
                lines = [str(call.args[0]) for call in logger.info.call_args_list if "🧭" in str(call.args[0])]
                self.assertEqual(lines, [f"[BiliBot] 🧭 私信 m1：{path}"])

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
