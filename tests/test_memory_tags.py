"""B4d-2 记忆标签：从现有字段算标签、sqlite 标签表跟着写和删、旧记忆启动时补标签。"""
import tempfile
import types
import unittest
from pathlib import Path

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  先装上假的 astrbot
from core.domain._memory_entry import MemoryEntry
from core.domain._memory_tags import tags_for
from core.domain.memory import MIGRATED_KEY, Memories
from core.layered_runtime import LayeredRuntime

BV = "BV1xx411c7mD"


class TagRuleTests(unittest.TestCase):
    def test_video_memory_gets_video_and_up(self):
        record = {"memory_type": "video", "bvid": BV, "owner_mid": "777", "user_id": "self", "actor_id": "sys:self"}
        self.assertEqual(tags_for(record), ["UP:777", f"视频:{BV}"])

    def test_chat_gets_the_bilibili_person_but_not_qq(self):
        self.assertEqual(tags_for({"actor_id": "bili:42", "user_id": "42"}), ["用户:42"])
        self.assertEqual(tags_for({"actor_id": "qq:42", "user_id": "42"}), [])
        self.assertEqual(tags_for({"actor_id": "bili:summary", "user_id": "summary"}), [])

    def test_other_sources(self):
        self.assertEqual(tags_for({"thread_id": f"video:{BV}"}), [f"视频:{BV}"])
        self.assertEqual(tags_for({"season_id": "12345"}), ["番剧:ss12345"])
        self.assertEqual(tags_for({"author_mid": "9", "memory_type": "dynamic"}), ["UP:9"])
        self.assertEqual(tags_for({"bvid": "not-a-bv", "season_id": "abc"}), [])

    def test_tags_already_on_the_record_are_kept(self):
        self.assertEqual(tags_for({"bvid": BV, "tags": ["发过动态:是", f"视频:{BV}", "", 3]}), ["发过动态:是", f"视频:{BV}"])


def _memories(layers, data):
    entry = MemoryEntry(types.SimpleNamespace(settings=types.SimpleNamespace(int=lambda key: 0)))
    return Memories(
        prepare=entry._prepare_memory_entry, normalize=entry._normalize_memory_entry, layered=lambda: layers,
        load_state=lambda: {}, save_state=lambda state: None, save_backup=lambda records: data.update(backup=records),
    )


class TagTableTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.layers = LayeredRuntime({"DEDE_USER_ID": "bot-1"}, Path(self.temp.name) / "bilibot.sqlite3")
        await self.layers.open()
        self.data = {}
        self.memories = _memories(self.layers, self.data)

    async def asyncTearDown(self):
        await self.layers.close()
        self.temp.cleanup()

    async def tagged(self, tag):
        return await self.layers.memories.legacy_keys_with_tag(tag)

    async def test_saved_memories_are_findable_by_tag_in_both_places(self):
        await self.memories.save({"rpid": "v1", "text": "看了视频", "memory_type": "video", "bvid": BV,
                                  "owner_mid": "777", "user_id": "self", "time": "2026-11-01 10:00"})
        await self.memories.save({"rpid": "c1", "text": "聊天", "user_id": "42", "thread_id": "t1",
                                  "time": "2026-11-01 11:00"})
        self.assertEqual(await self.tagged("UP:777"), ["v1"])
        self.assertEqual(await self.tagged("用户:42"), ["c1"])
        self.assertEqual([m["rpid"] for m in await self.memories.find(tag=f"视频:{BV}")], ["v1"])

    async def test_tags_follow_updates_and_deletes(self):
        await self.memories.save({"rpid": "v1", "text": "看了视频", "memory_type": "video", "bvid": BV, "user_id": "self"})
        await self.memories.save({"rpid": "v1", "text": "改了", "memory_type": "video", "bvid": "BV1yy411c7mD", "user_id": "self"})
        self.assertEqual(await self.tagged(f"视频:{BV}"), [])
        self.assertEqual(await self.tagged("视频:BV1yy411c7mD"), ["v1"])
        await self.memories.remove({"v1"})
        self.assertEqual(await self.tagged("视频:BV1yy411c7mD"), [])
        count = await self.layers.db.fetch_value("SELECT COUNT(*) FROM memory_tags", default=-1)
        self.assertEqual(count, 0)

    async def test_old_memories_get_tags_once_at_startup(self):
        await self.layers.memories.replace_legacy([
            {"rpid": "old", "text": "很久以前的聊天", "user_id": "42", "actor_id": "bili:42", "created_at": 1.0},
        ])
        self.assertEqual(await self.tagged("用户:42"), [])
        await self.layers.db.kv_set(MIGRATED_KEY, True)  # 老用户：旧 json 早就导入过了
        await self.memories.initialize()
        self.assertEqual(await self.tagged("用户:42"), ["old"])
        self.assertEqual(self.memories.records[0]["tags"], ["用户:42"])
        self.assertEqual(self.data["backup"][0]["tags"], ["用户:42"])


if __name__ == "__main__":
    unittest.main()
