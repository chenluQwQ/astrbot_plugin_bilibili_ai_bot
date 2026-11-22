"""AstrBot 删掉图片/视频只用文字重试时不收回答；QQ 命令不被分享解析抢走；动态池子跟着设置清理。"""
import asyncio
import logging
import time
import types
import unittest

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401

from core.features.dynamic_post import DynamicPost
from core.features.share import _COMMAND_WORD
from core.infra.vision import Vision, _image_mime
from core.video_diagnostics import get_call_diagnostic
from test_dynamic_ideas import PostHost

# AstrBot 通用接口删图重试时打的那条日志（astrbot/core/provider/sources/openai_source.py）
ASTRBOT_DROP_LOG = "检测到图片请求失败（%s），已移除图片并重试（保留文本内容）。"


class _Host:
    def __init__(self, drop_reason=None):
        self.config = {}
        self.settings = {}
        self.calls = 0
        owner = self

        class _Context:
            async def llm_generate(self, **kwargs):
                owner.calls += 1
                if drop_reason:
                    logging.getLogger("astrbot").warning(ASTRBOT_DROP_LOG, drop_reason)
                return types.SimpleNamespace(completion_text="视频里有一只猫在弹钢琴")

        self.context = _Context()


FRAME = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
         {"type": "text", "text": "描述画面"}]


class MediaDropTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_after_media_dropped_is_not_used(self):
        vision = Vision(_Host("invalid_attachment"))
        self.assertIsNone(await vision._astrbot_multimodal_generate("p/vision", FRAME))
        detail = get_call_diagnostic()
        self.assertEqual(detail.get("rejection"), "unsupported")
        self.assertIn("只用文字重试", detail.get("reason", ""))

    async def test_moderation_drop_is_not_a_model_capability(self):
        vision = Vision(_Host("image_content_moderated"))
        self.assertIsNone(await vision._astrbot_multimodal_generate("p/vision", FRAME))
        self.assertEqual(get_call_diagnostic().get("rejection"), "other")

    async def test_normal_answer_passes(self):
        vision = Vision(_Host())
        self.assertEqual(await vision._astrbot_multimodal_generate("p/vision", FRAME), "视频里有一只猫在弹钢琴")

    async def test_other_tasks_drop_does_not_leak(self):
        """别的调用（另一个任务）删了图，不影响这次。"""
        vision = Vision(_Host())

        async def someone_else():
            logging.getLogger("astrbot").warning(ASTRBOT_DROP_LOG, "model_not_vlm")

        async def ours():
            await asyncio.sleep(0)
            return await vision._astrbot_multimodal_generate("p/vision", FRAME)

        _, result = await asyncio.gather(someone_else(), ours())
        self.assertEqual(result, "视频里有一只猫在弹钢琴")

    async def test_text_only_call_is_not_watched(self):
        vision = Vision(_Host("invalid_attachment"))
        self.assertEqual(
            await vision._astrbot_multimodal_generate("p/vision", [{"type": "text", "text": "hi"}]),
            "视频里有一只猫在弹钢琴",
        )

    def test_image_mime_from_header(self):
        import base64
        self.assertEqual(_image_mime(base64.b64encode(b"\x89PNG\r\n\x1a\n0000").decode()), "image/png")
        self.assertEqual(_image_mime(base64.b64encode(b"RIFF0000WEBPVP8 ").decode()), "image/webp")
        self.assertEqual(_image_mime(base64.b64encode(b"GIF89a000000").decode()), "image/gif")
        self.assertEqual(_image_mime(base64.b64encode(b"\xff\xd8\xff\xe0").decode()), "image/jpeg")


class ShareCommandWordTests(unittest.TestCase):
    def test_commands_after_prefix_stripped_are_skipped(self):
        for text in ("bili测试查询 BV1xx411c7mD", "biliUMO", "bili解析", "bili性格回滚 2"):
            self.assertTrue(_COMMAND_WORD.match(text), text)

    def test_links_and_chat_are_not_commands(self):
        for text in ("bilibili.com/video/BV1xx411c7mD", "BV1xx411c7mD 好看", "b23.tv/abc", "bili上有个视频BV1xx"):
            self.assertFalse(_COMMAND_WORD.match(text), text)

    def test_every_registered_command_matches(self):
        import pathlib
        import re
        main = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        names = re.findall(r'filter\.command\("([^"]+)"', main)
        self.assertTrue(names)
        for name in names:
            self.assertTrue(_COMMAND_WORD.match(name), name)


def _idea(pool, key, kind="分享", source="proactive"):
    return pool.add(key=key, kind=kind, source=source, title=key, angle="想说的", now=time.time())


class IdeaTidyTests(unittest.TestCase):
    def test_turning_off_from_video_drops_video_ideas_keeps_external(self):
        host = PostHost([], {"DYNAMIC_FROM_VIDEO": False})
        _idea(host.pool, "BV1")
        _idea(host.pool, "ep:9", source="bangumi")
        _idea(host.pool, "feed:x", kind="转述", source="feed")
        DynamicPost(host).tidy_ideas()
        states = {item["key"]: (item["state"], item["reason"]) for item in host.pool.all()}
        self.assertEqual(states["BV1"][0], "dropped")
        self.assertIn("看完视频", states["BV1"][1])
        self.assertEqual(states["ep:9"][0], "dropped")
        self.assertEqual(states["feed:x"][0], "pending")

    def test_custom_topics_without_a_kind_drop_that_kind(self):
        host = PostHost([], {"DYNAMIC_TOPICS": ["吐槽：具体的事", "日常：作息"]})
        _idea(host.pool, "BV1", kind="分享")
        _idea(host.pool, "BV2", kind="吐槽")
        offered = DynamicPost(host)._offered_ideas()
        self.assertEqual([item["key"] for item in offered], ["BV2"])

    def test_free_text_topics_do_not_filter(self):
        host = PostHost([], {"DYNAMIC_TOPICS": ["随便聊聊"]})
        _idea(host.pool, "BV1", kind="分享")
        self.assertEqual(len(DynamicPost(host)._offered_ideas()), 1)


class MediaHeavyNoteTests(unittest.TestCase):
    def _post(self, entries):
        host = PostHost([])
        for entry in entries:
            host.dynamic_log.record({"time": "2026-11-20 10:00", **entry})
        return DynamicPost(host)

    def test_note_when_most_recent_posts_are_about_videos(self):
        post = self._post([{"text": "这个视频笑死", "bvid": "BV1"}, {"text": "《某番》第三集"},
                           {"text": "今天下雨"}, {"text": "看了个视频"}])
        note = post._media_heavy_note()
        self.assertIn("4 条动态里有 3 条", note)

    def test_no_note_when_mixed(self):
        post = self._post([{"text": "这个视频笑死", "bvid": "BV1"}, {"text": "今天下雨"}, {"text": "晚饭吃面"}])
        self.assertEqual(post._media_heavy_note(), "")

    def test_weekly_and_comment_sync_do_not_count(self):
        post = self._post([{"text": "视频1", "comment_sync": True}, {"text": "视频2", "weekly": True},
                           {"text": "视频3", "bvid": "BV3"}])
        self.assertEqual(post._media_heavy_note(), "")


if __name__ == "__main__":
    unittest.main()
