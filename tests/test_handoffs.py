"""交接：分支递过来的东西交给唯一接手的分支；看完想发动态由发动态那边查开关。"""
import unittest

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from core.features.dynamic_post import DynamicPost
from dynamic_fakes import HostRecordedPost
from parts import part
from core.lifecycle.handoffs import Handoffs, WantsToPostDynamic
from proactive_parts import ProactiveParts
from core.activities import HANDOFF_ROUTES
from schedule_parts import ScheduleParts


class HandoffsTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_waits_for_the_handler(self):
        got = []

        async def take(item):
            got.append(item.fresh)

        handoffs = Handoffs()
        handoffs.route(WantsToPostDynamic, take)
        self.assertTrue(await handoffs.send(WantsToPostDynamic(fresh=(1,))))
        self.assertEqual(got, [(1,)])

    async def test_unrouted_handoff_is_dropped(self):
        self.assertFalse(await Handoffs().send(WantsToPostDynamic(fresh=())))

    def test_one_handler_per_kind(self):
        async def take(item):
            pass

        handoffs = Handoffs()
        handoffs.route(WantsToPostDynamic, take)
        with self.assertRaises(ValueError):
            handoffs.route(WantsToPostDynamic, take)

    async def test_handler_errors_propagate(self):
        async def take(item):
            raise RuntimeError("boom")

        handoffs = Handoffs()
        handoffs.route(WantsToPostDynamic, take)
        with self.assertRaises(RuntimeError):
            await handoffs.send(WantsToPostDynamic(fresh=()))

    def test_every_route_points_at_a_plugin_method(self):
        for kind, method in HANDOFF_ROUTES:
            self.assertTrue(
                _route_exists(method),
                f"{kind.__name__} → {method} 找不到",
            )


def _route_exists(method):
    """接手的方法：``dynamic_post.xxx`` 这种插件上某个对象的。"""
    owner, _, name = method.rpartition(".")
    return bool(owner) and hasattr({"dynamic_post": DynamicPost}[owner], name)


class VideoDynamicBot(ProactiveParts, ScheduleParts):
    dynamic_post = part(HostRecordedPost)

    def __init__(self, config):
        self.config = config
        self.posted = []

    async def _run_dynamic(self, human_initiated=False, fresh=()):
        self.posted.append(fresh)
        return True


class VideoDynamicHandoffTests(unittest.IsolatedAsyncioTestCase):
    CANDIDATES = [3, 4]  # 这一轮放进动态池子的编号

    async def test_this_rounds_ideas_go_to_dynamic(self):
        bot = VideoDynamicBot({"ENABLE_DYNAMIC": True, "DYNAMIC_FROM_VIDEO": True})
        await bot.proactive_browse._post_video_dynamic(self.CANDIDATES)
        self.assertEqual(bot.posted, [(3, 4)])

    async def test_nothing_when_dynamic_is_off(self):
        bot = VideoDynamicBot({"ENABLE_DYNAMIC": False, "DYNAMIC_FROM_VIDEO": True})
        await bot.proactive_browse._post_video_dynamic(self.CANDIDATES)
        self.assertEqual(bot.posted, [])

    async def test_nothing_when_video_dynamics_are_off(self):
        bot = VideoDynamicBot({"ENABLE_DYNAMIC": True, "DYNAMIC_FROM_VIDEO": False})
        await bot.proactive_browse._post_video_dynamic(self.CANDIDATES)
        self.assertEqual(bot.posted, [])

    async def test_nothing_without_candidates(self):
        bot = VideoDynamicBot({"ENABLE_DYNAMIC": True, "DYNAMIC_FROM_VIDEO": True})
        await bot.proactive_browse._post_video_dynamic([])
        self.assertEqual(bot.posted, [])


if __name__ == "__main__":
    unittest.main()
