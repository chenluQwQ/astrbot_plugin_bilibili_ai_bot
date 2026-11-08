import importlib
import importlib.util
import json
import sys
import types
import unittest
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
    config.DEFAULT_DYNAMIC_PROMPT = ""
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
        cls.module = importlib.import_module(f"{_prepare_package()}.infra.search")

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
        result = self.module.WebSearch._format_firecrawl_results(data, 5)
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
        result = self.module.WebSearch._format_grok_response(data)
        self.assertIn("这是搜索结论。", result)
        self.assertEqual(result.count("https://example.com/a"), 1)


class ProviderConfigSchemaTests(unittest.TestCase):
    def test_schema_lists_new_backends(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("firecrawl", schema["WEB_SEARCH_BACKEND"]["options"])
        self.assertIn("grok", schema["WEB_SEARCH_BACKEND"]["options"])
        self.assertIn("novelai", schema["IMAGE_GEN_BACKEND"]["options"])


if __name__ == "__main__":
    unittest.main()
