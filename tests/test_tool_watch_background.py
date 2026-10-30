import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from core.tools import WATCH_PENDING_MESSAGE, create_tools


def tool_context(event=None):
    return types.SimpleNamespace(context=types.SimpleNamespace(event=event))


EVENT = types.SimpleNamespace(unified_msg_origin="aiocqhttp:GroupMessage:1", message_str="看看这个")


class ToolWatchBackgroundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        wait_patch = patch("core.tools.WATCH_INLINE_WAIT", 0.01)
        wait_patch.start()
        self.addCleanup(wait_patch.stop)
        self.release = asyncio.Event()
        self.plugin = types.SimpleNamespace(
            config={"OWNER_MID": "42", "DEDE_USER_ID": "900", "BILI_PRIVATE_SHARE_COOLDOWN": 0, "BILI_PRIVATE_SHARE_TOOL_ENABLED": True},
            event_runtime=types.SimpleNamespace(execute=self._execute_write),
            _load_json=lambda *_: {},
            _send_bili_private_video_share=AsyncMock(return_value=True),
            _send_tool_watch_followup=AsyncMock(),
            _share_scene=lambda _event: ("group_share", "群聊"),
        )
        self.plugin._watch_video_and_save_memory = AsyncMock(side_effect=self._slow_watch)
        tools = {tool.name: tool for tool in create_tools(self.plugin)}
        # 绕过授权层，这里只测看片本身；授权由 test_tool_execution_security 守。
        self.watch = tools["watch_video"].__class__.call.__get__(tools["watch_video"])
        self.share = tools["watch_and_share_video_private"].__class__.call.__get__(
            tools["watch_and_share_video_private"]
        )

    async def asyncTearDown(self):
        for task in list(self.plugin._tool_watch_jobs.values()):
            task.cancel()
        await asyncio.sleep(0)

    async def _execute_write(self, _request, handler):
        return types.SimpleNamespace(success=True, result=await handler())

    async def _slow_watch(self, bvid, **_kwargs):
        await self.release.wait()
        return {"ok": True, "message": f"看完了 {bvid}", "bvid": bvid, "video_info": {"title": "t"}}

    async def _finish_jobs(self):
        self.release.set()
        await asyncio.gather(*self.plugin._tool_watch_jobs.values())
        await asyncio.sleep(0)

    async def test_fast_watch_returns_result_in_place(self):
        self.release.set()
        result = await self.watch(tool_context(), bvid="BV1fast")
        self.assertIn("看完了 BV1fast", result)
        await asyncio.sleep(0)
        self.assertEqual(self.plugin._tool_watch_jobs, {})

    async def test_slow_watch_returns_pending_and_keeps_going(self):
        result = await self.watch(tool_context(), bvid="BV1slow")
        self.assertEqual(result, WATCH_PENDING_MESSAGE)
        task = self.plugin._tool_watch_jobs["watch:bv1slow"]
        self.assertFalse(task.done())
        self.release.set()
        self.assertIn("看完了", await task)

    async def test_cancelled_tool_call_does_not_stop_the_watch(self):
        with patch("core.tools.WATCH_INLINE_WAIT", 30):
            call = asyncio.create_task(self.watch(tool_context(), bvid="BV1slow"))
            await asyncio.sleep(0.01)
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call
        self.assertFalse(self.plugin._tool_watch_jobs["watch:bv1slow"].done())

    async def test_repeat_call_joins_the_running_watch(self):
        await self.watch(tool_context(), bvid="BV1slow")
        await self.watch(tool_context(), bvid="bv1SLOW")
        self.plugin._watch_video_and_save_memory.assert_awaited_once()

    async def test_new_video_is_refused_when_jobs_are_full(self):
        for index in range(3):
            await self.watch(tool_context(), bvid=f"BV{index}")
        result = await self.watch(tool_context(), bvid="BV9")
        self.assertIn("晚点再试", result)
        self.assertEqual(self.plugin._watch_video_and_save_memory.await_count, 3)

    async def test_failed_background_watch_frees_its_slot(self):
        self.plugin._watch_video_and_save_memory.side_effect = None
        release = self.release

        async def fail(*_args, **_kwargs):
            await release.wait()
            raise RuntimeError("boom")

        self.plugin._watch_video_and_save_memory.side_effect = fail
        await self.watch(tool_context(), bvid="BV1bad")
        release.set()
        await asyncio.wait(set(self.plugin._tool_watch_jobs.values()))
        await asyncio.sleep(0)
        self.assertEqual(self.plugin._tool_watch_jobs, {})

    async def test_slow_share_still_sends_the_card_after_returning(self):
        result = await self.share(tool_context(), bvid="BV1slow")
        self.assertEqual(result, WATCH_PENDING_MESSAGE)
        self.plugin._send_bili_private_video_share.assert_not_awaited()
        await self._finish_jobs()
        self.plugin._send_bili_private_video_share.assert_awaited_once()

    async def test_backgrounded_watch_sends_a_followup_to_the_asking_session(self):
        await self.watch(tool_context(EVENT), bvid="BV1slow")
        self.plugin._send_tool_watch_followup.assert_not_awaited()
        await self._finish_jobs()
        await asyncio.gather(*self.plugin._tool_watch_followup_tasks)
        self.plugin._send_tool_watch_followup.assert_awaited_once()
        call = self.plugin._send_tool_watch_followup.await_args
        self.assertEqual(call.args[0], EVENT.unified_msg_origin)
        self.assertIn("看完了 BV1slow", call.args[1])
        self.assertEqual(call.kwargs, {"scene": "群聊", "user_text": "看看这个", "failed": False})

    async def test_fast_watch_does_not_send_a_second_message(self):
        self.release.set()
        await self.watch(tool_context(EVENT), bvid="BV1fast")
        await asyncio.sleep(0)
        self.plugin._send_tool_watch_followup.assert_not_awaited()

    async def test_repeat_callers_in_one_session_get_one_followup(self):
        await self.watch(tool_context(EVENT), bvid="BV1slow")
        await self.watch(tool_context(EVENT), bvid="BV1slow")
        await self._finish_jobs()
        await asyncio.gather(*self.plugin._tool_watch_followup_tasks)
        self.plugin._send_tool_watch_followup.assert_awaited_once()

    async def test_failed_background_watch_still_tells_the_session(self):
        async def fail(*_args, **_kwargs):
            await self.release.wait()
            raise RuntimeError("boom")

        self.plugin._watch_video_and_save_memory.side_effect = fail
        await self.watch(tool_context(EVENT), bvid="BV1bad")
        self.release.set()
        await asyncio.wait(set(self.plugin._tool_watch_jobs.values()))
        await asyncio.sleep(0)
        await asyncio.gather(*self.plugin._tool_watch_followup_tasks)
        self.assertTrue(self.plugin._send_tool_watch_followup.await_args.kwargs["failed"])


class ToolWatchFollowupTextTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, raw):
        from core.share import ShareMixin

        class Bot(ShareMixin):
            pass

        bot = Bot.__new__(Bot)
        bot._qq_session_provider_id = AsyncMock(return_value="p")
        bot._qq_session_persona_prompt = AsyncMock(return_value="persona")
        bot._llm_call = AsyncMock(return_value=raw)
        bot._send_share_reaction = AsyncMock()
        return bot

    async def send(self, bot, **kwargs):
        await bot._send_tool_watch_followup(
            "umo", "看完了", scene="群聊", user_text="看看", **kwargs
        )
        return bot._send_share_reaction.await_args.args[1]

    async def test_valid_json_is_sent_and_material_is_marked_untrusted(self):
        bot = self.make_bot('{"decision":"comment","text":"这期讲得挺细的"}')
        self.assertEqual(await self.send(bot), "这期讲得挺细的")
        self.assertIn("watch_result", bot._llm_call.await_args.args[0])

    async def test_malformed_output_is_never_sent(self):
        bot = self.make_bot("这期讲得挺细的")
        self.assertEqual(await self.send(bot), "刚才那个视频有结果了，可以再问问我。")

    async def test_failure_does_not_call_the_model(self):
        bot = self.make_bot("")
        self.assertIn("没看成", await self.send(bot, failed=True))
        bot._llm_call.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
