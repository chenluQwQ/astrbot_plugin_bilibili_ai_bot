"""发动态：看完一轮想发的遇到别的动态正在发时排队，定时的照旧跳过。"""
import asyncio
import unittest

from parts import part
from schedule_parts import LegacyNowDoing

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from core.features.dynamic_post import DynamicPost


class BlockedPost(DynamicPost):
    async def _post_once(self, human_initiated=False, fresh=()):
        bot = self._host
        bot.calls.append(fresh)
        # 第一条卡住，模拟定时动态正在发。
        if len(bot.calls) == 1:
            await bot.release.wait()


class QueueBot:
    def __init__(self):
        self.config = {}
        self.calls = []
        self.release = asyncio.Event()
        self.dynamic_post = BlockedPost(self)

    now_doing = part(LegacyNowDoing)


    def _set_cross_platform_activity(self, *_args, **_kwargs):
        pass

    def _clear_cross_platform_activity(self, *_args):
        pass


async def _until(condition):
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("等不到条件成立")


class DynamicQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_dynamic_waits_for_running_post(self):
        bot = QueueBot()
        first = asyncio.ensure_future(bot.dynamic_post.run())
        await _until(lambda: bot.calls)
        second = asyncio.ensure_future(bot.dynamic_post.run(fresh=(1,)))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(bot.calls, [()])  # 还在等
        bot.release.set()
        self.assertTrue(await first)
        self.assertTrue(await second)
        self.assertEqual(bot.calls, [(), (1,)])

    async def test_scheduled_dynamic_is_skipped_while_another_runs(self):
        bot = QueueBot()
        first = asyncio.ensure_future(bot.dynamic_post.run(fresh=(1,)))
        await _until(lambda: bot.calls)
        self.assertFalse(await bot.dynamic_post.run())
        bot.release.set()
        await first
        self.assertEqual(bot.calls, [(1,)])


if __name__ == "__main__":
    unittest.main()
