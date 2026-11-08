import asyncio
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.features.comment_reply import CommentTurn
from reply_parts import ReplyParts
from core.runtime import EventState
from tests.test_comment_context import Bot, response
from tool_fakes import WithToolFakes, call


def make_bot(script):
    bot = Bot(script)
    bot.comment_context._COMMENT_INSPECT_INLINE_WAIT = 0.01
    bot.video_files._video_download_timeout = lambda duration: 120
    return bot


def inspect(question="有猫吗"):
    return call("video_inspect", query=question)


class BackgroundInspectionTests(WithToolFakes, unittest.IsolatedAsyncioTestCase):
    async def test_fast_inspection_replies_in_place(self):
        bot = make_bot([inspect(), response()])
        bot.comment_context._COMMENT_INSPECT_INLINE_WAIT = 1
        defer = AsyncMock()
        result = await bot.flow(defer=defer)
        self.assertEqual(result["decision"], "reply")
        self.assertEqual(result["_context_trace"][0]["tool"], "video_inspect")
        self.assertEqual(result["_context_trace"][0]["status"], "ok")
        defer.assert_not_awaited()

    async def test_slow_inspection_defers_then_resumes_with_evidence(self):
        bot = make_bot([call("video_description"), inspect(), call("web_search", query="之后又想查"),
                        response("ignore"), response()])
        release = asyncio.Event()

        async def slow(*_args):
            await release.wait()
            return {"status": "ok", "segments": [{"finding": "found"}]}

        bot.video_watch._inspect_comment_video = AsyncMock(side_effect=slow)
        resumed = asyncio.get_running_loop().create_future()

        async def defer(resume):
            resumed.set_result(resume)

        result = await bot.flow(defer=defer)
        self.assertEqual(result, {"decision": "deferred"})
        self.assertFalse(resumed.done())
        self.assertEqual(bot.logs, [])
        bot._web_search.assert_not_awaited()  # 转后台以后这一轮不再查
        self.assertIn("转到后台", bot.answers()[1])

        release.set()
        resume = await asyncio.wait_for(resumed, 1)
        final = await bot.flow(resume=resume)
        self.assertEqual(final["decision"], "reply")
        self.assertEqual([(row["tool"], row["status"]) for row in final["_context_trace"]], [
            ("video_description", "ok"), ("video_inspect", "deferred"), ("web_search", "stopped"), ("video_inspect", "ok"),
        ])
        second = bot.tool_loop.calls[-1]
        self.assertNotIn("video_inspect", second["tools"])
        self.assertIn("刚刚看完视频", second["prompt"])
        self.assertIn("介绍", second["prompt"])
        self.assertIn("found", second["prompt"])
        self.assertNotIn("转到后台", second["prompt"])
        self.assertIn("最多还能查2次", second["prompt"])
        self.assertEqual(len(bot.logs), 1)

    async def test_resume_does_not_offer_another_inspection(self):
        bot = make_bot([inspect("别的问题"), response()])
        run = bot.comment_context._comment_tool_run()
        result = await bot.flow(resume={"run": run, "state": {}})
        self.assertEqual(result["decision"], "reply")
        bot.video_watch._inspect_comment_video.assert_not_awaited()
        self.assertIn("未找到工具", bot.answers()[0])

    async def test_only_one_runs_and_full_queue_counts_as_timeout(self):
        release = asyncio.Event()
        running = []

        async def slow(*_args):
            running.append(1)
            await release.wait()
            return {"status": "ok", "segments": []}

        script = []
        for i in range(6):
            script += [inspect(str(i)), response("ignore")]
        bot = make_bot(script + [inspect("6"), response()])
        bot.video_watch._inspect_comment_video = AsyncMock(side_effect=slow)
        defer = AsyncMock()
        for _ in range(6):
            self.assertEqual((await bot.flow(defer=defer))["decision"], "deferred")
        self.assertEqual(len(running), 1)

        result = await bot.flow(defer=defer)
        self.assertEqual(result["decision"], "reply")
        self.assertEqual(result["_context_trace"][0]["status"], "timeout")

        release.set()
        await asyncio.wait_for(bot.comment_context._comment_inspection_worker, 1)
        self.assertEqual(len(running), 6)
        self.assertEqual(defer.await_count, 6)

    async def test_failed_model_round_after_deferring_still_defers(self):
        bot = make_bot([inspect(), RuntimeError("model down")])

        async def slow(*_args):
            await asyncio.Event().wait()

        bot.video_watch._inspect_comment_video = AsyncMock(side_effect=slow)
        self.assertEqual(await bot.flow(defer=AsyncMock()), {"decision": "deferred"})
        bot.comment_context._cancel_comment_inspections()

    async def test_stop_cancels_background_inspection(self):
        bot = make_bot([inspect(), response("ignore")])
        cancelled = asyncio.Event()

        async def slow(*_args):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        bot.video_watch._inspect_comment_video = AsyncMock(side_effect=slow)
        defer = AsyncMock()
        await bot.flow(defer=defer)
        bot.comment_context._cancel_comment_inspections()
        await asyncio.wait_for(cancelled.wait(), 1)
        defer.assert_not_awaited()

    async def test_test_mode_still_waits_in_place(self):
        bot = make_bot([inspect(), response()])
        result = await bot.flow(test_mode=True)
        self.assertEqual(result["decision"], "reply")
        self.assertFalse(hasattr(bot.comment_context, "_comment_inspection_jobs"))


class ResumeReplyTests(unittest.IsolatedAsyncioTestCase):
    def comment(self):
        item = {"mid": "1", "username": "昵称", "content": "内容", "oid": 123, "rpid": 9, "comment_type": 1, "thread_id": "t"}
        return CommentTurn(item=item, runtime_event=None, claim=SimpleNamespace(event_key="k"))

    def bot(self):
        bot = ReplyParts()
        bot.config = {}
        bot.event_runtime = SimpleNamespace(transition=AsyncMock())
        bot.schedule_rules._in_sleep_hours = lambda: False
        bot.reply_gate._daily_reply_limit_reached = lambda _: False
        bot.security_log = types.SimpleNamespace(record=lambda *a: None)
        bot.reply_writing._generate_reply = AsyncMock(return_value={"decision": "reply", "reply": "看到了"})
        bot.reply_aftermath._apply_reply_result = AsyncMock(return_value=True)
        bot.abuse_alert._check_abuse_alert = AsyncMock()
        return bot

    async def test_sends_after_inspection(self):
        bot = self.bot()
        resume = {"next_round": 1}
        await bot.comment_reply._resume_comment_turn(self.comment(), resume)
        self.assertIs(bot.reply_writing._generate_reply.await_args.kwargs["comment_resume"], resume)
        bot.reply_aftermath._apply_reply_result.assert_awaited_once()

    async def test_daily_limit_rechecked_after_inspection(self):
        bot = self.bot()
        bot.reply_gate._daily_reply_limit_reached = lambda _: True
        await bot.comment_reply._resume_comment_turn(self.comment(), {})
        bot.reply_writing._generate_reply.assert_not_awaited()
        bot.event_runtime.transition.assert_awaited_once_with("k", EventState.IGNORED, "daily_reply_limit")

    async def test_generation_error_marks_event_failed(self):
        bot = self.bot()
        bot.reply_writing._generate_reply = AsyncMock(side_effect=RuntimeError("boom"))
        await bot.comment_reply._resume_comment_turn(self.comment(), {})
        bot.event_runtime.transition.assert_awaited_once_with("k", EventState.FAILED, "deferred_reply_failed")
        bot.reply_aftermath._apply_reply_result.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
