import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.reply import ReplyMixin
from core.runtime import EventState
from tests.test_comment_context import Bot, response


def make_bot(responses):
    bot = Bot(responses)
    bot._COMMENT_INSPECT_INLINE_WAIT = 0.01
    bot._video_download_timeout = lambda duration: 120
    return bot


class BackgroundInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_inspection_replies_in_place(self):
        bot = make_bot([response("tool", "video_inspect", "有猫吗"), response()])
        defer = AsyncMock()
        result = await bot.flow(defer=defer)
        self.assertEqual(result["decision"], "reply")
        self.assertEqual(result["_context_trace"][0]["tool"], "video_inspect")
        defer.assert_not_awaited()

    async def test_slow_inspection_defers_then_resumes_with_evidence(self):
        bot = make_bot([response("tool", "video_inspect", "有猫吗"), response()])
        release = asyncio.Event()

        async def slow(*_args):
            await release.wait()
            return {"status": "ok", "segments": [{"finding": "found"}]}

        bot._inspect_comment_video = AsyncMock(side_effect=slow)
        resumed = asyncio.get_running_loop().create_future()

        async def defer(resume):
            resumed.set_result(resume)

        result = await bot.flow(defer=defer)
        self.assertEqual(result, {"decision": "deferred"})
        self.assertFalse(resumed.done())
        self.assertEqual(bot.logs, [])

        release.set()
        resume = await asyncio.wait_for(resumed, 1)
        final = await bot.flow(resume=resume)
        self.assertEqual(final["decision"], "reply")
        self.assertEqual(final["_context_trace"][0]["status"], "ok")
        last_prompt = bot._llm_call.await_args_list[-1].args[0]
        self.assertIn("found", last_prompt)
        self.assertIn("刚刚看完视频", last_prompt)
        self.assertEqual(len(bot.logs), 1)

    async def test_resume_does_not_start_another_inspection(self):
        bot = make_bot([response("tool", "video_inspect", "别的问题"), response()])
        resume = {"state": {}, "requested": set(), "evidence": [], "trace": [], "next_round": 1}
        result = await bot.flow(resume=resume)
        self.assertEqual(result["decision"], "reply")
        bot._inspect_comment_video.assert_not_awaited()
        self.assertEqual(result["_context_trace"][0]["reason"], "request_limit")

    async def test_only_one_runs_and_full_queue_counts_as_timeout(self):
        release = asyncio.Event()
        running = []

        async def slow(*_args):
            running.append(1)
            await release.wait()
            return {"status": "ok", "segments": []}

        bot = make_bot([response("tool", "video_inspect", str(i)) for i in range(6)]
                       + [response("tool", "video_inspect", "6"), response()])
        bot._inspect_comment_video = AsyncMock(side_effect=slow)
        defer = AsyncMock()
        for _ in range(6):
            self.assertEqual((await bot.flow(defer=defer))["decision"], "deferred")
        self.assertEqual(len(running), 1)

        result = await bot.flow(defer=defer)
        self.assertEqual(result["decision"], "reply")
        self.assertEqual(result["_context_trace"][0]["reason"], "timeout")

        release.set()
        await asyncio.wait_for(bot._comment_inspection_worker, 1)
        self.assertEqual(len(running), 6)
        self.assertEqual(defer.await_count, 6)

    async def test_stop_cancels_background_inspection(self):
        bot = make_bot([response("tool", "video_inspect", "有猫吗")])
        cancelled = asyncio.Event()

        async def slow(*_args):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        bot._inspect_comment_video = AsyncMock(side_effect=slow)
        defer = AsyncMock()
        await bot.flow(defer=defer)
        bot._cancel_comment_inspections()
        await asyncio.wait_for(cancelled.wait(), 1)
        defer.assert_not_awaited()

    async def test_test_mode_still_waits_in_place(self):
        bot = make_bot([response("tool", "video_inspect", "有猫吗"), response()])
        result = await bot.flow(test_mode=True)
        self.assertEqual(result["decision"], "reply")
        self.assertFalse(hasattr(bot, "_comment_inspection_jobs"))


class ResumeReplyTests(unittest.IsolatedAsyncioTestCase):
    def comment(self):
        return {"event_key": "k", "mid": "1", "username": "昵称", "content": "内容", "oid": 123, "rpid": 9,
                "comment_type": 1, "thread_id": "t", "image_desc": "", "content_emb": None}

    def bot(self):
        bot = ReplyMixin()
        bot.config = {}
        bot.event_runtime = SimpleNamespace(transition=AsyncMock())
        bot._in_sleep_hours = lambda: False
        bot._daily_reply_limit_reached = lambda _: False
        bot._log_security_event = lambda *a: None
        bot._generate_reply = AsyncMock(return_value={"decision": "reply", "reply": "看到了"})
        bot._apply_reply_result = AsyncMock(return_value=True)
        bot._check_abuse_alert = AsyncMock()
        return bot

    async def test_sends_after_inspection(self):
        bot = self.bot()
        resume = {"next_round": 1}
        await bot._resume_comment_reply(self.comment(), resume)
        self.assertIs(bot._generate_reply.await_args.kwargs["comment_resume"], resume)
        bot._apply_reply_result.assert_awaited_once()

    async def test_daily_limit_rechecked_after_inspection(self):
        bot = self.bot()
        bot._daily_reply_limit_reached = lambda _: True
        await bot._resume_comment_reply(self.comment(), {})
        bot._generate_reply.assert_not_awaited()
        bot.event_runtime.transition.assert_awaited_once_with("k", EventState.IGNORED, "daily_reply_limit")

    async def test_generation_error_marks_event_failed(self):
        bot = self.bot()
        bot._generate_reply = AsyncMock(side_effect=RuntimeError("boom"))
        await bot._resume_comment_reply(self.comment(), {})
        bot.event_runtime.transition.assert_awaited_once_with("k", EventState.FAILED, "deferred_reply_failed")
        bot._apply_reply_result.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
