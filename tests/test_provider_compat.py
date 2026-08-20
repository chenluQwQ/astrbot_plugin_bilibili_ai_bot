import asyncio
import base64
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _prepare_package():
    package_name = "provider_compat_core"
    package = sys.modules.setdefault(package_name, types.ModuleType(package_name))
    package.__path__ = [str(ROOT / "core")]

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.logger = _Logger()
    astrbot.api = api

    config = types.ModuleType(f"{package_name}.config")
    config.WEB_SEARCH_CACHE_FILE = ""
    config.DEFAULT_DYNAMIC_TOPICS = []
    config.DYNAMIC_LOG_FILE = ""
    config.DAILY_SUMMARY_FILE = ""
    config.PERMANENT_MEMORY_FILE = ""
    config.TEMP_IMAGE_DIR = ""
    config.WATCH_LOG_FILE = ""
    config.WEEKLY_SUMMARY_FILE = ""
    sys.modules[config.__name__] = config

    runtime = types.ModuleType(f"{package_name}.runtime")
    runtime.ActionRequest = object
    runtime.EventPriority = types.SimpleNamespace(BACKGROUND=50)
    sys.modules[runtime.__name__] = runtime
    return package_name


def _load_module(name):
    package_name = _prepare_package()
    module_name = f"{package_name}.{name}"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "core" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class SearchProviderCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module("search")

    def test_firecrawl_v2_response_is_formatted_with_sources(self):
        data = {
            "success": True,
            "data": {
                "web": [
                    {
                        "title": "示例页面",
                        "url": "https://example.com/page",
                        "description": " 第一行\n第二行 ",
                    }
                ]
            },
        }
        result = self.module.WebSearchMixin._format_firecrawl_results(data, 5)
        self.assertIn("[示例页面](https://example.com/page)", result)
        self.assertIn("第一行 第二行", result)

    def test_grok_responses_payload_keeps_text_and_unique_citations(self):
        data = {
            "output": [
                {"type": "web_search_call", "status": "completed"},
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "这是搜索结论。",
                            "annotations": [
                                {"type": "url_citation", "url": "https://example.com/a", "title": "来源A"},
                                {"type": "url_citation", "url": "https://example.com/a", "title": "来源A"},
                            ],
                        }
                    ],
                },
            ]
        }
        result = self.module.WebSearchMixin._format_grok_response(data)
        self.assertIn("这是搜索结论。", result)
        self.assertEqual(result.count("https://example.com/a"), 1)


class NovelAICompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module("dynamic")

    def test_novelai_json_image_response_is_decoded(self):
        expected = b"\x89PNG\r\n\x1a\nimage"
        body = json.dumps({"images": [{"image": base64.b64encode(expected).decode("ascii")}]}).encode("utf-8")
        actual = self.module.DynamicMixin._decode_novelai_image(body, "application/json")
        self.assertEqual(actual, expected)

    def test_novelai_zip_image_response_is_decoded(self):
        expected = b"\x89PNG\r\n\x1a\nzip-image"
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("image_0.png", expected)
        actual = self.module.DynamicMixin._decode_novelai_image(stream.getvalue(), "application/zip")
        self.assertEqual(actual, expected)

    def test_novelai_is_not_called_by_automatic_dynamic_task(self):
        module = self.module

        class Bot(module.DynamicMixin):
            def __init__(self):
                self.config = {
                    "IMAGE_GEN_BACKEND": "novelai",
                    "IMAGE_GEN_API_KEY": "token",
                }
                self.called = False

            async def _generate_novelai_image(self, *_args):
                self.called = True
                return "unexpected"

        bot = Bot()
        result = asyncio.run(bot._generate_image("test", human_initiated=False))
        self.assertIsNone(result)
        self.assertFalse(bot.called)

    def test_novelai_is_available_for_manual_dynamic_command(self):
        module = self.module

        class Bot(module.DynamicMixin):
            def __init__(self):
                self.config = {
                    "IMAGE_GEN_BACKEND": "novelai",
                    "IMAGE_GEN_API_KEY": "token",
                }

            async def _generate_novelai_image(self, prompt, api_key, base_url, model):
                self.received = (prompt, api_key, base_url, model)
                return "generated.png"

        bot = Bot()
        result = asyncio.run(bot._generate_image("test", human_initiated=True))
        self.assertEqual(result, "generated.png")
        self.assertEqual(bot.received[1:], ("token", "https://image.novelai.net", "nai-diffusion-4-5-full"))

    def test_automatic_dynamic_can_explicitly_choose_silence(self):
        module = self.module

        class Bot(module.DynamicMixin):
            def __init__(self):
                self.config = {}
                self.prompt = ""

            def _load_json(self, _path, default=None):
                return default

            def _get_today_mood(self):
                return "平静", ""

            async def _get_system_prompt(self):
                return "测试人设"

            async def _llm_call(self, prompt, **_kwargs):
                self.prompt = prompt
                return '{"decision":"skip","text":"","need_image":false,"image_prompt":""}'

        bot = Bot()
        result = asyncio.run(bot._generate_dynamic_content(human_initiated=False))
        self.assertEqual(result["decision"], "skip")
        self.assertIn("定时时刻本身不是发布理由", bot.prompt)
        self.assertIn("今天没有足够具体", bot.prompt)


class ProviderConfigSchemaTests(unittest.TestCase):
    def test_schema_lists_new_backends(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("firecrawl", schema["WEB_SEARCH_BACKEND"]["options"])
        self.assertIn("grok", schema["WEB_SEARCH_BACKEND"]["options"])
        self.assertIn("novelai", schema["IMAGE_GEN_BACKEND"]["options"])


if __name__ == "__main__":
    unittest.main()
