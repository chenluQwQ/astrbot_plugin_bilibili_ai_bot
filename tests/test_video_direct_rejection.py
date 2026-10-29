"""Video direct read must stop after AstrBot rejects the part shape.

线上现象：主动看视频时每个切片都先试「视频直读」，日志里反复出现
``AstrBot 多模态 provider 调用失败：ValueError: Invalid image file: data URI mime='video/mp4'``，
然后才回退截帧——截帧每次都成功，画面分析结果一直都有。

根因：AstrBot 的 provider 链路上没有「视频 part」——
  * 原生 Gemini 适配器把 ``image_url`` 一律当图片严格解析，mp4 会被当场拒掉
    （``Invalid image file: data URI mime='video/mp4' payload_len=…``）；
  * 同一适配器遇到 ``video_url`` / ``file`` part 会落进它的音频分支
    （``KeyError: 'audio_url'``）。
这两种拒答都不带 HTTP 状态码，旧实现把它们归到 ``other``，于是每个切片、每个视频都重新
上传一遍整段视频（线上实测单次 2.1–7.2 MB），而 ``_video_direct_skip_set()`` 从没被写入。
"""
import tempfile
import unittest
from pathlib import Path

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  (installs the AstrBot stub)

from core.video import VideoMixin
from core.vision import VisionMixin, classify_vision_rejection

# 线上实测（astrbot.log 2026-10-29 14:31:52，payload 是一段 2.5 MB 的 mp4）
ASTRBOT_IMAGE_ONLY = "Invalid image file: data URI mime='video/mp4' payload_len=2530484"
# 原生 Gemini 适配器遇到 video_url / file part 时落到音频分支
ASTRBOT_PART_CRASH = "KeyError: 'audio_url'"


class Probe(VideoMixin, VisionMixin):
    """只走 AstrBot provider、没有插件自有视觉 API 的最小替身。"""

    def __init__(self, error=ASTRBOT_IMAGE_ONLY):
        self.config = {"VIDEO_VISION_FPS": 2}
        self.error = error
        self.uploads = []
        owner = self

        class _Context:
            async def llm_generate(self, **kwargs):
                # 记录每次真正会发出去的报文形状，用来断言「试了哪几种」
                owner.uploads.append([part.get("type") for part in kwargs.get("prompt") or []])
                raise ValueError(owner.error)

        self.context = _Context()

    async def direct(self, seg, fmt, client=None, model=""):
        return await self._try_video_direct(
            str(seg), "描述这一段", fmt, "p/vision", client, model, {}, ""
        )

    def skip_key(self, fmt, model=""):
        return self._video_direct_key("p/vision", model, fmt)


class VisionRejectionClassifyTests(unittest.TestCase):
    def test_astrbot_image_only_rejection_is_unsupported(self):
        # image_url 里的 mp4 被 AstrBot 的严格图片解析拒掉：这是形状问题，重试救不回来
        self.assertEqual(classify_vision_rejection(ValueError(ASTRBOT_IMAGE_ONLY)), "unsupported")

    def test_astrbot_unknown_part_crash_is_unsupported(self):
        # video_url / file part 落进 AstrBot 的音频分支：同样说明这个形状它不吃
        self.assertEqual(classify_vision_rejection(ValueError(ASTRBOT_PART_CRASH)), "unsupported")

    def test_unavailable_still_wins_over_part_rejection(self):
        # 鉴权/模型不存在时不该被判成「形状不支持」，否则会白跑截帧
        self.assertEqual(
            classify_vision_rejection(ValueError(f"unauthorized: {ASTRBOT_IMAGE_ONLY}")),
            "unavailable",
        )

    def test_transient_errors_stay_retryable(self):
        # 回归保护：网络抖动一次就把直读永久拉黑，比多试几次更糟
        for message in ("Connection reset by peer", "502 Bad Gateway", "ReadTimeout"):
            self.assertEqual(classify_vision_rejection(ValueError(message)), "other")


class VideoDirectRejectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.segment = Path(self.tmp.name) / "seg.mp4"
        self.segment.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_part_rejection_skips_remaining_segments(self):
        bot = Probe(error=ASTRBOT_IMAGE_ONLY)
        self.assertIsNone(await bot.direct(self.segment, "qwen"))
        self.assertEqual(len(bot.uploads), 1)
        self.assertIn(bot.skip_key("qwen"), bot._video_direct_skip_set())
        # 同一视频的下一段：不再上传整段视频
        self.assertIsNone(await bot.direct(self.segment, "qwen"))
        self.assertEqual(len(bot.uploads), 1)

    async def test_gemini_shape_is_still_attempted_once(self):
        # 刻意不预先排除 gemini：OpenAI 兼容网关会把这个 part 原样转发，吃不吃取决于网关，
        # 插件不该替用户判断；只在被拒之后才跳过。
        bot = Probe(error=ASTRBOT_IMAGE_ONLY)
        self.assertIsNone(await bot.direct(self.segment, "gemini"))
        self.assertEqual(bot.uploads, [["image_url", "text"]])
        self.assertIn(bot.skip_key("gemini"), bot._video_direct_skip_set())
        await bot.direct(self.segment, "gemini")
        self.assertEqual(len(bot.uploads), 1)

    async def test_auto_probe_tries_every_shape_then_stops(self):
        bot = Probe(error=ASTRBOT_PART_CRASH)
        self.assertIsNone(await bot.direct(self.segment, "auto"))
        shapes = sorted({part_type for parts in bot.uploads for part_type in parts})
        self.assertEqual(shapes, ["file", "image_url", "text", "video_url"])
        self.assertEqual(len(bot.uploads), 4)
        self.assertIn(bot.skip_key("auto"), bot._video_direct_skip_set())
        # 下一段不再重复探测
        await bot.direct(self.segment, "auto")
        self.assertEqual(len(bot.uploads), 4)

    async def test_transient_failure_keeps_retrying(self):
        bot = Probe(error="502 Bad Gateway")
        await bot.direct(self.segment, "qwen")
        self.assertNotIn(bot.skip_key("qwen"), bot._video_direct_skip_set())
        await bot.direct(self.segment, "qwen")
        self.assertEqual(len(bot.uploads), 2)

    async def test_own_client_still_gets_its_turn_after_astrbot_rejects(self):
        # 兼容：自带视觉 API 的用户，AstrBot 拒答后仍会用自己的 client 再试一次
        bot = Probe(error=ASTRBOT_IMAGE_ONLY)
        await bot.direct(self.segment, "gemini", client=object(), model="own-vision")
        self.assertEqual(len(bot.uploads), 1)


if __name__ == "__main__":
    unittest.main()
