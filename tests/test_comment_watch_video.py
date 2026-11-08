"""评论区看别人推荐的视频：``watch_video`` 工具走后台队列，看完再回复。"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.test_comment_context import BV, Bot, response
from tool_fakes import WithToolFakes, call

OTHER = "BV1Ab411c7Xy"


def make_bot(script, watched=None):
    bot = Bot(script)
    bot.comment_context._COMMENT_INSPECT_INLINE_WAIT = 0.01
    bot.watch_on_request = SimpleNamespace(_watch_video_and_save_memory=AsyncMock(
        side_effect=watched or (lambda *_args, **_kw: {"ok": True, "message": "看完了：讲做饭的"})))
    return bot


def watch(bvid=OTHER):
    return call("watch_video", bvid=bvid)


class CommentWatchVideoTests(WithToolFakes, unittest.IsolatedAsyncioTestCase):
    async def test_quick_watch_replies_in_place_and_saves_as_comment_watch(self):
        bot = make_bot([watch(), response()])
        bot.comment_context._COMMENT_INSPECT_INLINE_WAIT = 1
        defer = AsyncMock()
        result = await bot.flow(defer=defer)
        self.assertEqual(result["decision"], "reply")
        self.assertEqual([(row["tool"], row["status"]) for row in result["_context_trace"]], [("watch_video", "ok")])
        bot.watch_on_request._watch_video_and_save_memory.assert_awaited_once_with(OTHER, memory_source="comment_tool")
        self.assertIn("讲做饭的", bot.answers()[0])
        self.assertIn('trust="untrusted"', bot.answers()[0])
        defer.assert_not_awaited()

    async def test_slow_watch_defers_then_resumes_without_another_watch(self):
        release = asyncio.Event()

        async def slow(*_args, **_kw):
            await release.wait()
            return {"ok": True, "message": "看完了：很好笑"}

        bot = make_bot([watch(), response("ignore"), response()], watched=slow)
        resumed = asyncio.get_running_loop().create_future()

        async def defer(resume):
            resumed.set_result(resume)

        self.assertEqual(await bot.flow(defer=defer), {"decision": "deferred"})
        self.assertIn("转到后台", bot.answers()[0])
        release.set()
        final = await bot.flow(resume=await asyncio.wait_for(resumed, 1))
        self.assertEqual(final["decision"], "reply")
        self.assertEqual([(row["tool"], row["status"]) for row in final["_context_trace"]],
                         [("watch_video", "deferred"), ("watch_video", "ok")])
        second = bot.tool_loop.calls[-1]
        self.assertNotIn("watch_video", second["tools"])
        self.assertNotIn("video_inspect", second["tools"])
        self.assertIn("很好笑", second["prompt"])

    async def test_current_video_or_bad_id_is_refused_without_watching(self):
        for bvid, hint in ((BV, "video_inspect"), ("看这个", "BV 号")):
            bot = make_bot([watch(bvid), response()])
            await bot.flow()
            bot.watch_on_request._watch_video_and_save_memory.assert_not_awaited()
            self.assertIn(hint, bot.answers()[0])

    async def test_daily_limit_stops_offering_the_tool(self):
        bot = make_bot([watch(), response(), response()])
        bot.config["COMMENT_WATCH_VIDEO_DAILY_LIMIT"] = 1
        bot.comment_context._COMMENT_INSPECT_INLINE_WAIT = 1
        await bot.flow()
        await bot.flow()
        self.assertIn("watch_video", bot.tool_loop.calls[0]["tools"])
        self.assertNotIn("watch_video", bot.tool_loop.calls[1]["tools"])
        self.assertEqual(bot.watch_on_request._watch_video_and_save_memory.await_count, 1)

    async def test_off_when_limit_is_zero_or_not_in_the_allowlist(self):
        bot = make_bot([response()])
        bot.config["COMMENT_WATCH_VIDEO_DAILY_LIMIT"] = 0
        await bot.flow()
        self.assertNotIn("watch_video", bot.tool_loop.calls[0]["tools"])
        self.assertNotIn("watch_video", bot.tool_loop.calls[0]["prompt"])
        bot = make_bot([response()])
        bot.config["BILI_TOOL_ALLOWLIST"] = ["bili_up_info"]
        await bot.flow()
        self.assertNotIn("watch_video", bot.tool_loop.calls[0]["tools"])

    async def test_failed_watch_tells_the_model(self):
        bot = make_bot([watch(), response()], watched=lambda *_a, **_k: {"ok": False, "message": f"找不到视频 {OTHER}"})
        bot.comment_context._COMMENT_INSPECT_INLINE_WAIT = 1
        result = await bot.flow()
        self.assertEqual(result["_context_trace"][0]["status"], "unavailable")
        self.assertIn("找不到视频", bot.answers()[0])


if __name__ == "__main__":
    unittest.main()
