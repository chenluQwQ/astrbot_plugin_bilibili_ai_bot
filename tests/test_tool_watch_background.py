import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from core.tools import WATCH_PENDING_MESSAGE, create_tools


def tool_context():
    return types.SimpleNamespace(context=types.SimpleNamespace(event=None))


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


if __name__ == "__main__":
    unittest.main()
