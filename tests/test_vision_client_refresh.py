"""配置页改了 Key / 地址 / 代理不重载插件，这些修复保证改完就生效。

- 视觉 / 向量 / 搜索客户端按配置指纹缓存，配置一变就重建；
- 视觉接口返回 content=None 当成空回复，不当成调用出错；
- 只带 ``.code`` / ``.status`` 的 SDK 异常也能按状态码分类；
- 一段视频超限只影响不比它小的片段，不拉黑整个模型；
- 之前判成「Key 无效」的模型，换了 Key 就重新试。
"""
import tempfile
import types
import unittest
from pathlib import Path

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  (installs the AstrBot stub)

from video_parts import VideoParts
from core.infra.vision import Vision, classify_vision_rejection
from core.network import cached_client, close_plugin_clients
from core.video_diagnostics import exception_detail, exception_status_code


class _Settings(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class _Host:
    def __init__(self, **settings):
        self.config = {}
        self.settings = _Settings(settings)
        self._embed_client = None
        self._video_vision_client = None
        self._image_vision_client = None
        self._web_search_client = None


class ClientRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_vision_client_rebuilt_after_key_change(self):
        host = _Host(VIDEO_VISION_API_KEY="old-key", VIDEO_VISION_API_BASE="https://a.example/v1")
        vision = Vision(host)
        first = vision._get_video_vision_client()
        self.assertIs(vision._get_video_vision_client(), first)
        host.settings["VIDEO_VISION_API_KEY"] = "new-key"
        second = vision._get_video_vision_client()
        self.assertIsNot(second, first)
        self.assertEqual(second.api_key, "new-key")
        await close_plugin_clients(host)

    async def test_base_change_and_key_removal(self):
        host = _Host(IMAGE_VISION_API_KEY="k", IMAGE_VISION_API_BASE="https://a.example/v1")
        vision = Vision(host)
        first = vision._get_image_vision_client()
        host.settings["IMAGE_VISION_API_BASE"] = "https://b.example/v1"
        second = vision._get_image_vision_client()
        self.assertIsNot(second, first)
        self.assertIn("b.example", str(second.base_url))
        host.settings["IMAGE_VISION_API_KEY"] = ""
        self.assertIsNone(vision._get_image_vision_client())
        self.assertIsNone(host._image_vision_client)

    async def test_proxy_change_rebuilds_embed_client(self):
        host = _Host(EMBED_API_KEY="k")
        vision = Vision(host)
        first = vision._get_embed_client()
        host.config["PROXY_URL"] = "http://127.0.0.1:7890"
        self.assertIsNot(vision._get_embed_client(), first)
        await close_plugin_clients(host)

    def test_injected_client_is_kept(self):
        # 测试或旧代码直接放进来的客户端，按当前配置认下，不被替换
        host = _Host()
        fake = object()
        host._video_vision_client = fake
        self.assertIs(cached_client(host, "_video_vision_client", "fp", lambda: object()), fake)
        self.assertIs(cached_client(host, "_video_vision_client", "fp", lambda: object()), fake)


class _Response:
    def __init__(self, content):
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]


class _Client:
    def __init__(self, content):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))
        self.content = content

    async def _create(self, **kwargs):
        return _Response(self.content)


class VisionCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_content_is_empty_reply(self):
        vision = Vision(_Host())
        self.assertIsNone(await vision._vision_call(_Client(None), "m", [{"type": "text", "text": "看"}]))
        self.assertEqual(await vision._vision_call(_Client("  画面  "), "m", []), "画面")


class StatusCodeTests(unittest.TestCase):
    def _exc(self, **attrs):
        exc = RuntimeError("boom")
        for key, value in attrs.items():
            setattr(exc, key, value)
        return exc

    def test_status_code_from_other_attributes(self):
        self.assertEqual(exception_status_code(self._exc(code=401)), 401)
        self.assertEqual(exception_status_code(self._exc(status=413)), 413)
        self.assertEqual(exception_status_code(self._exc(response=types.SimpleNamespace(status_code=404))), 404)
        # 字符串错误码、布尔值、不像状态码的数字都不算
        self.assertIsNone(exception_status_code(self._exc(code="INVALID_ARGUMENT")))
        self.assertIsNone(exception_status_code(self._exc(code=True)))
        self.assertIsNone(exception_status_code(self._exc(code=7)))

    def test_classify_and_detail_use_code(self):
        self.assertEqual(classify_vision_rejection(self._exc(code=403)), "unavailable")
        self.assertEqual(classify_vision_rejection(self._exc(status=413)), "too_large")
        self.assertIn("HTTP 401", exception_detail(self._exc(code=401), {}))


class TooLargeProbe(VideoParts):
    def __init__(self):
        self.config = {"VIDEO_VISION_FPS": 2}
        self.settings = _Settings(VIDEO_VISION_API_KEY="")
        self.limit = 1000
        self.uploads = 0
        owner = self

        class _Context:
            async def llm_generate(self, **kwargs):
                owner.uploads += 1
                size = owner.current_size
                if size > owner.limit:
                    exc = ValueError("Request Entity Too Large")
                    exc.code = 413  # 只带 .code 的 SDK 异常，顺带测状态码兼容
                    raise exc
                return types.SimpleNamespace(completion_text="看到了")

        self.context = _Context()
        self.vision = Vision(self)

    async def direct(self, seg):
        self.current_size = Path(seg).stat().st_size
        return await self.video_watch._try_video_direct(str(seg), "描述", "openai", "p/vision", None, "", {}, "")


class TooLargeTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_segment_does_not_blacklist_model(self):
        probe = TooLargeProbe()
        with tempfile.TemporaryDirectory() as tmp:
            big, bigger, small = (Path(tmp) / name for name in ("big.mp4", "bigger.mp4", "small.mp4"))
            big.write_bytes(b"0" * 2000)
            bigger.write_bytes(b"0" * 3000)
            small.write_bytes(b"0" * 500)
            self.assertIsNone(await probe.direct(big))
            self.assertEqual(probe.uploads, 1)
            # 更大的片段不再白传
            self.assertIsNone(await probe.direct(bigger))
            self.assertEqual(probe.uploads, 1)
            # 小片段（比如下一个短视频）照样直读
            self.assertEqual(await probe.direct(small), "看到了")
            self.assertEqual(probe.uploads, 2)
            key = probe.video_watch._video_direct_key("p/vision", "", "openai")
            self.assertNotIn(key, probe.video_watch._video_direct_skip_set())

    def test_key_change_invalidates_probe_key(self):
        probe = TooLargeProbe()
        before = probe.video_watch._video_direct_key("p/vision", "m", "auto")
        probe.settings["VIDEO_VISION_API_KEY"] = "fixed-key"
        self.assertNotEqual(before, probe.video_watch._video_direct_key("p/vision", "m", "auto"))


if __name__ == "__main__":
    unittest.main()
