"""现在在干嘛：按活动分开记，一个活动结束不影响别的；五种定时活动都说得出名字。"""
import unittest
from unittest.mock import AsyncMock, patch

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from parts import part
from core.features.dynamic_post import DynamicPost
from core.activities import SCHEDULED_ACTIVITIES
from core.lifecycle.now_doing import DOING_LABELS, NowDoing

LABELS = {"proactive": "主动浏览", "dynamic": "发布动态"}


class NowDoingTests(unittest.TestCase):
    def test_nothing_going_on(self):
        self.assertEqual(NowDoing().describe(LABELS), "")

    def test_stage_and_details(self):
        now = NowDoing()
        now.report("proactive", "正在分析视频", title="短片", up_name="")
        self.assertEqual(now.describe(LABELS), "正在主动浏览：正在分析视频；内容：《短片》")

    def test_new_stage_replaces_old_details(self):
        now = NowDoing()
        now.report("proactive", "正在分析视频", title="短片")
        now.report("proactive", "正在挑选视频")
        self.assertEqual(now.describe(LABELS), "正在主动浏览：正在挑选视频")

    def test_finishing_one_keeps_the_other(self):
        now = NowDoing()
        now.report("proactive", "正在分析视频")
        now.report("dynamic", "正在写动态")
        self.assertEqual(
            now.describe(LABELS),
            "正在主动浏览：正在分析视频；同时正在发布动态：正在写动态",
        )
        now.finish("dynamic")
        self.assertEqual(now.describe(LABELS), "正在主动浏览：正在分析视频")

    def test_every_scheduled_activity_has_a_label(self):
        self.assertLessEqual(set(SCHEDULED_ACTIVITIES), set(DOING_LABELS))


class ContextTests(unittest.TestCase):
    def test_context_line_for_qq(self):
        now = NowDoing()
        self.assertEqual(now.prompt(), "")
        now.report("special_follow", "正在分析视频", title="短片")
        self.assertEqual(
            now.prompt(),
            "【当前活动】正在看特别关注：正在分析视频；内容：《短片》。仅在用户询问当前活动或自然相关时提及；不得猜测未记录的进度。",
        )
        now.finish("special_follow")
        self.assertEqual(now.prompt(), "")


class DynamicReportsTests(unittest.IsolatedAsyncioTestCase):
    async def test_dynamic_reports_while_posting_and_finishes(self):
        seen = []

        class Seen:
            def report(self, kind, stage, **details):
                seen.append(("set", kind, stage, details))

            def finish(self, kind):
                seen.append(("clear", kind))

        class Bot:
            config = {}
            dynamic_post = part(DynamicPost)
            now_doing = Seen()

        with patch.object(DynamicPost, "_post_once", AsyncMock(side_effect=RuntimeError("发布失败"))):
            await Bot().dynamic_post.run(fresh=(1,))
        self.assertEqual(seen, [
            ("set", "dynamic", "正在写动态", {"title": ""}),
            ("clear", "dynamic"),
        ])


if __name__ == "__main__":
    unittest.main()
