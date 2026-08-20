import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _load_consolidation_module():
    package_name = "video_lifecycle_test_core"
    package = sys.modules.setdefault(package_name, types.ModuleType(package_name))
    package.__path__ = [str(ROOT / "core")]

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.logger = getattr(api, "logger", _Logger())
    astrbot.api = api

    config_name = f"{package_name}.config"
    config = types.ModuleType(config_name)
    config.MEMORY_FILE = "memory.json"
    config.CONSOLIDATION_DISCARD_THRESHOLD = 3
    config.CONSOLIDATION_BATCH_SIZE = 20
    config.RECENT_PROMOTE_DAYS = 14
    config.LONG_TERM_AGE_DAYS = 180
    config.CONSOLIDATION_STATE_FILE = "consolidation.json"
    sys.modules[config_name] = config

    module_name = f"{package_name}.consolidation"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "core" / "consolidation.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _Bot:
    def __init__(self, memories):
        self._memory = memories

    @staticmethod
    def _video_memory_windows():
        return 15, 90

    @staticmethod
    def _memory_timestamp(value):
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.strptime(str(value)[:16], "%Y-%m-%d %H:%M").timestamp()

    @staticmethod
    def _normalize_memory_entry(memory):
        return memory

    @staticmethod
    def _match_memory_type(memory, memory_types=None):
        return not memory_types or memory.get("memory_type") in set(memory_types)


class VideoMemoryLifecycleTests(unittest.TestCase):
    def test_video_memory_fades_to_low_weight_semantic_trace(self):
        module = _load_consolidation_module()
        now = datetime.now()
        memories = [
            {
                "rpid": "detail",
                "memory_type": "video",
                "created_at": (now - timedelta(days=10)).timestamp(),
                "level": "recent",
                "text": "完整视频内容",
                "video_title": "十天前的视频",
                "embedding": [1.0, 0.0],
                "importance": 6,
            },
            {
                "rpid": "long",
                "memory_type": "video",
                "created_at": (now - timedelta(days=20)).timestamp(),
                "level": "recent",
                "text": "仍保留的详细视频内容",
                "video_title": "二十天前的视频",
                "embedding": [1.0, 0.0],
                "importance": 8,
            },
            {
                "rpid": "faded",
                "memory_type": "video",
                "created_at": (now - timedelta(days=100)).timestamp(),
                "level": "long_term",
                "text": "非常长的旧视频详细分析",
                "video_summary": "讲的是一场关于记忆设计的讨论",
                "video_title": "一百天前的视频",
                "owner_name": "测试UP",
                "tname": "知识",
                "embedding": [1.0, 0.0],
                "embedding_model": "test",
                "importance": 8,
            },
        ]
        bot = _Bot(memories)
        result = module.ConsolidationEngine(bot)._apply_video_lifecycle()

        self.assertEqual(result, {"detail": 1, "long_term": 1, "faded": 1})
        self.assertEqual(memories[0]["video_memory_stage"], "detail")
        self.assertIn("embedding", memories[0])
        self.assertEqual(memories[1]["video_memory_stage"], "long_term")
        self.assertEqual(memories[1]["level"], "long_term")
        self.assertEqual(memories[1]["importance"], 4)
        self.assertIn("embedding", memories[1])
        self.assertEqual(memories[2]["video_memory_stage"], "faded")
        self.assertTrue(memories[2]["aged"])
        self.assertEqual(memories[2]["embedding"], [1.0, 0.0])
        self.assertEqual(memories[2]["embedding_model"], "test")
        self.assertIn("[看过·已淡忘]", memories[2]["text"])
        self.assertIn("大概是", memories[2]["text"])


if __name__ == "__main__":
    unittest.main()
