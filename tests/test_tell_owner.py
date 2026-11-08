"""找主人说：回复 JSON 里可选的 tell_owner、发到一个 QQ 窗口、每天几次 / 间隔 / 同一个人 / 心情低少吐槽、不回复也能说。"""
import json
import types
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.features import _tell_owner
from core.features._tell_owner import TellOwner
from core.output_protocol import ReplyProtocolError, parse_reply_envelope, reply_schema_instruction
from core.settings import Settings

NOW = datetime(2026, 11, 7, 12, 0).timestamp()


def envelope(**extra):
    value = {"decision": "ignore", "reply": "", "score_delta": 0, "impression": "", "user_facts": [],
             "signals": {"interaction_type": "normal", "feedback_type": "none", "attack_level": 0,
                         "feedback_topic": None, "reflection_candidate": None, "confidence": 0.9},
             "tool_request": {"name": "none", "query": ""}, **extra}
    return json.dumps(value, ensure_ascii=False)


class ProtocolTests(unittest.TestCase):
    def test_tell_owner_is_optional_and_allowed_without_replying(self):
        self.assertIsNone(parse_reply_envelope(envelope(), channel="comment")["tell_owner"])
        note = {"mood": "吐槽", "text": "这条评论好离谱"}
        self.assertEqual(parse_reply_envelope(envelope(tell_owner=note), channel="comment")["tell_owner"], note)
        for bad in ({"mood": "生气", "text": "x"}, {"mood": "分享"}, {"mood": "分享", "text": ""}, "说一句"):
            with self.assertRaises(ReplyProtocolError):
                parse_reply_envelope(envelope(tell_owner=bad), channel="comment")

    def test_prompt_only_mentions_it_when_on(self):
        self.assertNotIn("tell_owner", reply_schema_instruction())
        self.assertIn('"tell_owner":null', reply_schema_instruction(tell_owner=True))


class _File:
    def __init__(self):
        self.data = {}

    def read(self, default=None):
        return self.data

    def update(self, change, default=None):
        self.data = change(self.data)
        return self.data


def make(mood="🌙 平静如常", **config):
    context = types.SimpleNamespace(send_message=AsyncMock())
    host = types.SimpleNamespace(
        settings=Settings({"ENABLE_TELL_OWNER": True, "OWNER_QQ_UMO": "FriendMessage:owner", "OWNER_MID": "1", **config}),
        context=context, mood=types.SimpleNamespace(today=lambda: (mood, "")),
        roles=types.SimpleNamespace(is_owner=lambda uid: str(uid) == "1"),
    )
    return TellOwner(host, _File()), context


class LimitTests(unittest.IsolatedAsyncioTestCase):
    async def tell(self, owner, uid="42", mood="分享", at=0):
        with patch.object(_tell_owner, "logger"):
            return await owner.maybe_tell({"mood": mood, "text": "想跟你说"}, uid=uid, username="小明",
                                          content="原文" * 60, channel="comment", now=NOW + at)

    async def test_sends_to_the_window_with_a_snippet(self):
        owner, context = make(TELL_OWNER_QQ_UMO="GroupMessage:room")
        sent = []

        class Chain:
            def message(self, text):
                sent.append(text)
                return self

        with patch("astrbot.api.event.MessageChain", Chain):
            self.assertTrue(await self.tell(owner))
        self.assertEqual(context.send_message.await_args.args[0], "GroupMessage:room")
        self.assertTrue(sent[0].startswith("想跟你说\n——小明（UID 42）在B站评论里说：「原文"))
        self.assertTrue(sent[0].endswith("…」"))

    async def test_daily_limit_gap_and_same_person(self):
        owner, context = make()
        self.assertTrue(await self.tell(owner, uid="42"))
        self.assertFalse(await self.tell(owner, uid="42", at=4 * 3600))  # 同一个人
        self.assertFalse(await self.tell(owner, uid="43", at=3600))      # 不到 3 小时
        self.assertTrue(await self.tell(owner, uid="43", at=4 * 3600))
        self.assertFalse(await self.tell(owner, uid="44", at=8 * 3600))  # 一天 2 次
        self.assertEqual(context.send_message.await_count, 2)

    async def test_low_mood_complains_only_once(self):
        owner, _ = make(mood="🧊 懒得废话", TELL_OWNER_GAP_HOURS=0)
        self.assertTrue(await self.tell(owner, uid="42", mood="吐槽"))
        self.assertFalse(await self.tell(owner, uid="43", mood="吐槽"))
        self.assertTrue(await self.tell(owner, uid="44", mood="分享"))

    async def test_off_owner_or_no_window_sends_nothing(self):
        for config, uid in (({"ENABLE_TELL_OWNER": False}, "42"), ({}, "1"), ({"OWNER_QQ_UMO": ""}, "42")):
            owner, context = make(**config)
            self.assertFalse(await self.tell(owner, uid=uid))
            context.send_message.assert_not_awaited()


class CommentHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_ignored_comment_still_reaches_the_owner(self):
        from core.features.comment_reply import CommentReply
        tell = types.SimpleNamespace(maybe_tell=AsyncMock(return_value=True))
        reply = CommentReply(types.SimpleNamespace(tell_owner=tell))
        reply._ignore_comment = AsyncMock()
        turn = types.SimpleNamespace(mid="42", username="小明", content="离谱评论", path=[])
        note = {"mood": "吐槽", "text": "好离谱"}
        self.assertFalse(await reply._accept_generated_reply(turn, {"decision": "ignore", "tell_owner": note}))
        tell.maybe_tell.assert_awaited_once_with(note, uid="42", username="小明", content="离谱评论", channel="comment")


if __name__ == "__main__":
    unittest.main()
