"""分享后问一句喜不喜欢：按概率、一天一次、用 QQ 会话的人设；主人回答后工具记进口味；工具只给管理员。"""
import types
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.features import _owner_feedback
from core.features._owner_feedback import OwnerFeedback
from core.settings import Settings

NOW = datetime(2026, 11, 7, 12, 0).timestamp()


class _File:
    def __init__(self):
        self.data = {}

    def read(self, default=None):
        return self.data

    def write(self, value):
        self.data = value


def make(reply='{"decision":"comment","text":"这个你喜欢吗"}', chance=0.2, roll=0.1, **config):
    qq = types.SimpleNamespace(
        _qq_session_provider_id=AsyncMock(return_value="p1"),
        _qq_session_persona_prompt=AsyncMock(return_value="会话人设"),
        _send_share_reaction=AsyncMock(),
    )
    host = types.SimpleNamespace(
        settings=Settings({"ASK_OWNER_FEEDBACK_CHANCE": chance, "OWNER_QQ_UMO": "FriendMessage:owner",
                           "OWNER_MID": "1", **config}),
        qq_chat=qq, models=types.SimpleNamespace(_llm_call=AsyncMock(return_value=reply)),
        user_profiles=MagicMock(),
    )
    return OwnerFeedback(host, _File(), rng=lambda: roll), host


class AskTests(unittest.IsolatedAsyncioTestCase):
    async def ask(self, feedback, at=0):
        with patch.object(_owner_feedback, "logger"):
            return await feedback.maybe_ask("BV1", "猫猫视频", "这个猫好可爱", now=NOW + at)

    async def test_asks_once_a_day_in_the_session_persona(self):
        feedback, host = make()
        self.assertTrue(await self.ask(feedback))
        call = host.models._llm_call.await_args
        self.assertEqual((call.kwargs["system_prompt"], call.kwargs["provider_id"]), ("会话人设", "p1"))
        host.qq_chat._send_share_reaction.assert_awaited_once_with("FriendMessage:owner", "这个你喜欢吗", None)
        self.assertFalse(await self.ask(feedback, at=3600))
        self.assertTrue(await self.ask(feedback, at=86400))

    async def test_roll_chance_and_skip(self):
        for kwargs in ({"roll": 0.5}, {"chance": 0}, {"reply": '{"decision":"skip","text":""}'}, {"reply": "坏的"},
                       {"OWNER_QQ_UMO": ""}):
            feedback, host = make(**kwargs)
            self.assertFalse(await self.ask(feedback))
            host.qq_chat._send_share_reaction.assert_not_awaited()


class RecordTests(unittest.IsolatedAsyncioTestCase):
    async def asked(self):
        feedback, host = make()
        with patch.object(_owner_feedback, "logger"):
            await feedback.maybe_ask("BV1", "猫猫视频", "这个猫好可爱", now=NOW)
        return feedback, host

    async def test_liked_points_become_taste_tags(self):
        feedback, host = await self.asked()
        with patch.object(_owner_feedback, "logger"):
            text = feedback.record(True, ["猫", "治愈"], now=NOW + 60)
        self.assertIn("喜欢", text)
        host.user_profiles.update.assert_called_once_with("1", new_tags=["猫", "治愈"], source_scope="bili_comment")
        self.assertIn("没有在等", feedback.record(True, ["猫"], now=NOW + 120))  # 记过一次就不再等了

    async def test_disliked_drops_tags_and_notes_it(self):
        feedback, host = await self.asked()
        with patch.object(_owner_feedback, "logger"):
            feedback.record(False, [], now=NOW + 60)
        host.user_profiles.update.assert_called_once_with("1", new_facts=["不太喜欢《猫猫视频》这类视频"],
                                                          source_scope="bili_comment")
        feedback, host = await self.asked()
        with patch.object(_owner_feedback, "logger"):
            feedback.record(False, ["吵闹"], now=NOW + 60)
        host.user_profiles.drop_tags.assert_called_once_with("1", ["吵闹"])

    async def test_old_question_expires_but_bvid_still_works(self):
        feedback, host = await self.asked()
        self.assertIn("没有在等", feedback.record(True, ["猫"], now=NOW + 3 * 86400))
        with patch.object(_owner_feedback, "logger"):
            self.assertIn("记下了", feedback.record(True, ["猫"], bvid="BV9", now=NOW + 3 * 86400))


class TierTests(unittest.TestCase):
    def test_feedback_tool_is_persona_admin(self):
        import tempfile
        from pathlib import Path
        from core.layered_runtime import LayeredRuntime
        from core.security.toolgate import Tier
        with tempfile.TemporaryDirectory() as tmp:
            runtime = LayeredRuntime({}, Path(tmp) / "x.db")
            runtime.register_tools([types.SimpleNamespace(name="bili_owner_feedback", description="", parameters={},
                                                          call=None)])
            self.assertIs(runtime.tool_gate.get("bili_owner_feedback").tier, Tier.PERSONA_ADMIN)


if __name__ == "__main__":
    unittest.main()
