"""私信伪流：拆句、停顿、发到一半失败怎么算。"""
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  先装上假的 astrbot
import core.features.private_reply as private_reply_module
from core.domain.typing_pace import split_for_typing, typing_pause
from core.features.private_reply import PrivateReply
from core.runtime import WriteResultUnknownError


class SplitTests(unittest.TestCase):
    def test_splits_on_sentence_ends(self):
        self.assertEqual(split_for_typing("今天看了一个手绘动画。配色特别舒服！你也去看看吧？"),
                         ["今天看了一个手绘动画。", "配色特别舒服！", "你也去看看吧？"])

    def test_short_or_linked_replies_stay_whole(self):
        self.assertEqual(split_for_typing("好呀。明天见！"), ["好呀。明天见！"])
        text = "这个视频在这里。https://www.bilibili.com/video/BV1xx411c7mD?p=2 你看看！"
        self.assertEqual(split_for_typing(text), [text])

    def test_short_pieces_and_lone_emotes_join_the_previous_sentence(self):
        self.assertEqual(split_for_typing("哈哈哈！今天看了一个特别好笑的视频！[doge]"),
                         ["哈哈哈！今天看了一个特别好笑的视频！[doge]"])
        self.assertEqual(split_for_typing("今天看了一个手绘动画！[doge]配色特别舒服呢。"),
                         ["今天看了一个手绘动画！[doge]", "配色特别舒服呢。"])

    def test_runs_of_punctuation_and_closing_quotes_stay_together(self):
        self.assertEqual(split_for_typing("真的假的？！我还以为是剪辑的。“原来是真的啊！”"),
                         ["真的假的？！我还以为是剪辑的。", "“原来是真的啊！”"])

    def test_at_most_three_parts(self):
        parts = split_for_typing("第一句话有点长呢。第二句话也挺长的。第三句话还是很长。第四句话也不短哦。")
        self.assertEqual(parts, ["第一句话有点长呢。", "第二句话也挺长的。", "第三句话还是很长。第四句话也不短哦。"])

    def test_newlines_split_too(self):
        self.assertEqual(split_for_typing("今天看了一个手绘动画\n配色特别舒服呢"), ["今天看了一个手绘动画", "配色特别舒服呢"])

    def test_pause_grows_with_length_within_bounds(self):
        self.assertEqual(typing_pause("嗯"), 1.0)
        self.assertEqual(typing_pause("字" * 100), 4.0)
        self.assertLess(typing_pause("短一点的话"), typing_pause("这是一句稍微长一些的话呀"))


def make_reply(results, enabled=True):
    sent = []

    async def send(mid, text):
        sent.append(text)
        result = results[len(sent) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    host = types.SimpleNamespace(
        settings={"ENABLE_PRIVATE_PSEUDO_STREAM": enabled},
        bili_messages=types.SimpleNamespace(_send_bili_private_message=send),
    )
    return PrivateReply(host), sent


TEXT = "今天看了一个手绘动画。配色特别舒服！你也去看看吧？"


class SendPartsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sleep = AsyncMock()
        patcher = patch.object(private_reply_module.asyncio, "sleep", self.sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_off_sends_one_message(self):
        reply, sent = make_reply([True], enabled=False)
        self.assertTrue(await reply._send_private_reply_parts("1", TEXT))
        self.assertEqual(sent, [TEXT])
        self.sleep.assert_not_awaited()

    async def test_on_sends_each_part_with_a_pause(self):
        reply, sent = make_reply([True, True, True])
        self.assertTrue(await reply._send_private_reply_parts("1", TEXT))
        self.assertEqual(sent, ["今天看了一个手绘动画。", "配色特别舒服！", "你也去看看吧？"])
        self.assertEqual(self.sleep.await_count, 2)

    async def test_first_part_failing_fails_the_whole_reply(self):
        reply, sent = make_reply([False])
        self.assertFalse(await reply._send_private_reply_parts("1", TEXT))
        self.assertEqual(len(sent), 1)

    async def test_first_part_unknown_stays_unknown(self):
        reply, _sent = make_reply([WriteResultUnknownError("timeout")])
        with self.assertRaises(WriteResultUnknownError):
            await reply._send_private_reply_parts("1", TEXT)

    async def test_later_part_failing_stops_without_resending(self):
        for later in (False, WriteResultUnknownError("timeout"), RuntimeError("boom")):
            reply, sent = make_reply([True, later, True])
            self.assertTrue(await reply._send_private_reply_parts("1", TEXT))
            self.assertEqual(sent, ["今天看了一个手绘动画。", "配色特别舒服！"])


if __name__ == "__main__":
    unittest.main()
