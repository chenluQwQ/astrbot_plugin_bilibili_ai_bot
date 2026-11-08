"""旧数据样例：拿真实运行过的 data/ 的结构跑启动迁移，作为之后搬数据的基准。

样例在 ``tests/fixtures/legacy_data/``，来源和脱敏规则见那里的 README。
``legacy_data_broken/`` 是手工做的坏文件：截断、空文件、编码不对、类型不对。
"""
from base_parts import BaseParts
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from core.layered_runtime import LayeredRuntime
from core.storage.json_file import JsonFile
from test_unified_memory import _MemoryBotBase, _load_memory_module

FIXTURES = Path(__file__).parent / "fixtures"
LEGACY = FIXTURES / "legacy_data"
BROKEN = FIXTURES / "legacy_data_broken"

# _load_memory_module 给几个文件起了测试用的名字。
RENAMED = {"user_profiles.json": "profiles.json", "security_log.json": "security.json"}

# 文件顶层类型：之后的导入器按这个读。
TOP_LEVEL = {
    "affection.json": dict, "bangumi_memory.json": dict, "bangumi_watch_log.json": list,
    "commented_videos.json": list, "dynamic_log.json": list, "dynamic_watch_log.json": list,
    "external_memory.json": dict, "memory.json": list, "milestones.json": dict,
    "mood.json": dict, "owner_recommend_log.json": dict, "permanent_memory.json": list,
    "personality_evolution.json": dict, "preference_state.json": dict,
    "private_message_state.json": dict, "proactive_log.json": list,
    "proactive_trigger_log.json": list, "replied.json": list, "reply_log.json": list,
    "security_log.json": list, "seen_videos.json": dict, "user_profiles.json": dict,
    "video_memory.json": dict, "watch_log.json": list, "weekly_summary.json": list,
}


def _fixture(name):
    return json.loads((LEGACY / name).read_text(encoding="utf-8"))


class LegacyFixtureShapeTests(unittest.TestCase):
    def test_every_file_parses(self):
        for path in sorted(LEGACY.glob("*.json")):
            with self.subTest(path.name):
                json.loads(path.read_text(encoding="utf-8"))

    def test_top_level_types(self):
        for name, kind in TOP_LEVEL.items():
            with self.subTest(name):
                self.assertIsInstance(_fixture(name), kind)

    def test_references_line_up_across_files(self):
        # 脱敏时同一个原值换成同一个假值，跨文件的引用要还对得上。
        memories = _fixture("memory.json")
        profiles = _fixture("user_profiles.json")
        users = {m["user_id"] for m in memories if str(m.get("user_id", "")).isdigit()}
        self.assertTrue(users & set(profiles))
        watched = {e["bvid"] for e in _fixture("watch_log.json")}
        seen = set(_fixture("seen_videos.json"))
        self.assertTrue(watched & seen)


class LegacyStartupMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp_dir.name)
        self.module, self.config = _load_memory_module(self.temp_dir.name)
        self.layers = LayeredRuntime({"DEDE_USER_ID": "bot-1"}, self.dir / "bilibot.sqlite3")
        await self.layers.open()

        class Bot(self.module.MemoryParts, _MemoryBotBase, BaseParts):
            def _load_json(self, path, default=None):
                return JsonFile(path).read(default)

        self.bot_type = Bot

    async def asyncTearDown(self):
        await self.layers.close()
        self.temp_dir.cleanup()

    def _copy(self, source):
        for path in source.glob("*.json"):
            shutil.copy2(path, self.dir / RENAMED.get(path.name, path.name))

    def _bot(self):
        bot = self.bot_type()
        memory = bot.json_store._load_json(self.config.MEMORY_FILE, [])
        bot._memory = memory if isinstance(memory, list) else []
        bot.layered_runtime = self.layers
        return bot

    async def test_fixture_memories_and_seen_videos_migrate(self):
        self._copy(LEGACY)
        bot = self._bot()
        with_text = [m for m in _fixture("memory.json") if str(m.get("text") or "").strip()]

        self.assertTrue(await bot.memory_startup._initialize_unified_memory())
        self.assertEqual(len(bot._memory), len(with_text))
        self.assertEqual(await self.layers.memories.legacy_count(), len(with_text))
        self.assertEqual(
            {m["rpid"] for m in bot._memory}, {m["rpid"] for m in with_text}
        )

        total = await bot.memory_startup._initialize_seen_videos()
        self.assertGreaterEqual(total, len(_fixture("seen_videos.json")))
        self.assertTrue(set(_fixture("seen_videos.json")) <= await bot.seen_videos.bvids())

        # 第二次启动不重复导入。
        again = self._bot()
        await again.memory_startup._initialize_unified_memory()
        self.assertEqual(await self.layers.memories.legacy_count(), len(with_text))
        self.assertEqual(await again.memory_startup._initialize_seen_videos(), total)

    async def test_broken_files_do_not_stop_startup(self):
        self._copy(LEGACY)
        self._copy(BROKEN)
        bot = self._bot()

        self.assertEqual(bot._memory, [])
        self.assertTrue(await bot.memory_startup._initialize_unified_memory())
        self.assertEqual(await self.layers.memories.legacy_count(), 0)
        await bot.memory_startup._initialize_seen_videos()
        self.assertEqual(bot.json_store._load_json(self.config.AFFECTION_FILE, {}), {})

        # 截断和编码坏掉的文件在旁边留了一份，下次保存覆盖原文件后还能找回。
        for name in ("memory.json", "affection.json"):
            with self.subTest(name):
                self.assertTrue(list(self.dir.glob(name + ".corrupt-*")))


if __name__ == "__main__":
    unittest.main()
