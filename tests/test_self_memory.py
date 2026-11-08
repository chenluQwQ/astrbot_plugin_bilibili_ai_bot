"""B4c 永久记忆：最多 8 条、Bot 用 bili_self_memory 自己增改删、默认只在和主人聊时给、私信和评论隔离。"""
import tempfile
import types
import unittest
from pathlib import Path

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  先装上假的 astrbot
from core.domain.self_memory import MAX_ENTRIES, SelfMemories, SelfMemoryRejected, format_numbered
from core.features._self_memory_tool import SelfMemoryTool
from core.infra.tool_loop import ToolMiss
from core.storage.json_file import JsonFile
from tests.test_comment_context import Bot, response
import test_private_tools
from test_comment_reply_snapshot import reply_json
from test_private_reply_snapshot import message
from tool_fakes import WithToolFakes, call


class _Store(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.file = JsonFile(Path(temp.name) / "permanent_memory.json")
        self.store = SelfMemories(self.file)


class StoreTests(_Store):
    def test_add_edit_delete_by_number(self):
        self.assertEqual(self.store.add("我喜欢猫", "bili_comment"), 1)
        self.assertEqual(self.store.add("主人在学吉他", "bili_comment"), 2)
        self.assertEqual(self.store.edit(1, "我更喜欢橘猫", "bili_comment"), "我喜欢猫")
        self.assertEqual(self.store.delete(2, "bili_comment"), "主人在学吉他")
        self.assertEqual(format_numbered(self.file.read([])), "#1 我更喜欢橘猫")

    def test_full_at_eight_and_rules_on_text(self):
        for i in range(MAX_ENTRIES):
            self.store.add(f"第{i}条", "bili_comment")
        with self.assertRaisesRegex(SelfMemoryRejected, "满8条"):
            self.store.add("第九条", "bili_comment")
        self.store.edit(1, "换掉一条", "bili_comment")  # 满了也能改
        for bad in ("", "  ", "长" * 61):
            with self.subTest(text=bad), self.assertRaises(SelfMemoryRejected):
                self.store.edit(1, bad, "bili_comment")
        self.store.delete(8, "bili_comment")
        with self.assertRaisesRegex(SelfMemoryRejected, "已经记着"):
            self.store.add("换掉一条", "bili_comment")
        for bad in (0, 9, True, "1", None):
            with self.subTest(number=bad), self.assertRaises(SelfMemoryRejected):
                self.store.delete(bad, "bili_comment")

    def test_old_files_over_the_cap_still_load_and_shrink(self):
        self.file.write([{"time": "2025-01-01 10:00", "text": f"旧{i}"} for i in range(12)] + [{"text": ""}, "坏的"])
        self.assertEqual(len(self.store.entries()), 12)
        with self.assertRaises(SelfMemoryRejected):
            self.store.add("新的", "bili_comment")
        self.store.delete(1, "bili_comment")
        self.assertEqual(self.store.entries()[0]["text"], "旧1")


class IsolationTests(_Store):
    def setUp(self):
        super().setUp()
        self.store.add("我喜欢猫", "bili_comment")
        self.store.add("主人最近在准备考研", "bili_dm")
        self.store.add("主人喜欢吉他", "bili_comment")

    def test_private_notes_stay_out_of_comments_and_dynamics(self):
        entries = self.file.read([])
        self.assertEqual(format_numbered(entries, "bili_comment"), "#1 我喜欢猫\n#2 主人喜欢吉他")
        self.assertEqual(format_numbered(entries, "proactive"), "#1 我喜欢猫\n#2 主人喜欢吉他")
        self.assertEqual(format_numbered(entries, "bili_dm"), "#1 我喜欢猫\n#2 主人最近在准备考研\n#3 主人喜欢吉他")
        self.assertEqual(len(self.store.entries()), 3)  # 管理员看全部

    def test_numbers_follow_what_the_place_can_see(self):
        self.assertEqual(self.store.delete(2, "bili_comment"), "主人喜欢吉他")
        with self.assertRaises(SelfMemoryRejected):
            self.store.edit(2, "看不到的那条", "bili_comment")
        self.assertEqual(self.store.add("又一条", "bili_comment"), 2)

    def test_editing_a_public_note_in_private_makes_it_private(self):
        self.store.edit(1, "我喜欢猫，主人说他也养了一只", "bili_dm")
        self.assertEqual(format_numbered(self.file.read([]), "bili_comment"), "#1 主人喜欢吉他")

    def test_full_message_mentions_hidden_private_notes(self):
        for i in range(MAX_ENTRIES - 3):
            self.store.add(f"补{i}", "bili_comment")
        with self.assertRaisesRegex(SelfMemoryRejected, "1 条是私信里记的"):
            self.store.add("新的", "bili_comment")

    def test_legacy_notes_without_a_place_count_as_public(self):
        self.file.write([{"text": "旧的"}])
        self.assertEqual(format_numbered(self.file.read([]), "bili_comment"), "#1 旧的")


class ToolGateTests(_Store):
    def tool(self, **settings):
        values = {"ENABLE_SELF_MEMORY": True, "SELF_MEMORY_OWNER_ONLY": True, **settings}
        host = types.SimpleNamespace(settings=values, self_memories=self.store,
                                     roles=types.SimpleNamespace(is_owner=lambda uid: uid == "777"))
        return SelfMemoryTool(host)

    def test_owner_only_by_default(self):
        tool = self.tool()
        self.assertTrue(tool.enabled("777"))
        self.assertFalse(tool.enabled("1"))
        self.assertFalse(tool.enabled(""))
        self.assertTrue(self.tool(SELF_MEMORY_OWNER_ONLY=False).enabled("1"))
        self.assertFalse(self.tool(ENABLE_SELF_MEMORY=False).enabled("777"))

    def test_apply_explains_failures_to_the_model(self):
        tool = self.tool()
        self.assertIn("第 1 条", tool.apply(action="add", text="我喜欢猫", scope="bili_comment"))
        for kwargs in ({"action": "edit", "number": 5, "text": "x"}, {"action": "drop", "number": 1}, {"action": "add", "text": ""}):
            with self.subTest(**kwargs), self.assertRaises(ToolMiss):
                tool.apply(scope="bili_comment", **kwargs)


def self_memory(action="add", **kw):
    return call("bili_self_memory", action=action, **kw)


def comment_bot(script):
    bot = Bot(script)
    bot.roles = types.SimpleNamespace(is_owner=lambda uid: uid == "777")
    return bot


class CommentFlowTests(WithToolFakes, unittest.IsolatedAsyncioTestCase):
    async def flow(self, bot, uid):
        return await bot.comment_context._run_comment_reply_flow("评论", "personality", 123, 1, "thread", uid=uid)

    async def test_owner_can_add_from_comments_and_it_is_public(self):
        bot = comment_bot([self_memory(text="主人说我画画进步了"), response()])
        await self.flow(bot, "777")
        self.assertIn("bili_self_memory", bot.tool_loop.calls[0]["tools"])
        self.assertIn("记下了", bot.tool_loop.results[0][1])
        self.assertEqual(bot.self_memories.entries("bili_comment")[0]["text"], "主人说我画画进步了")

    async def test_strangers_and_read_only_tests_do_not_get_it(self):
        bot = comment_bot([response()])
        await self.flow(bot, "1")
        self.assertNotIn("bili_self_memory", bot.tool_loop.calls[0]["tools"])
        bot = comment_bot([response()])
        await bot.comment_context._run_comment_reply_flow("p", "s", 123, 1, "manual_test", uid="777", test_mode=True)
        self.assertNotIn("bili_self_memory", bot.tool_loop.calls[0]["tools"])

    async def test_one_change_per_comment(self):
        bot = comment_bot([self_memory(text="一"), self_memory(text="二"), response()])
        await self.flow(bot, "777")
        self.assertEqual([e["text"] for e in bot.self_memories.entries()], ["一"])


class PrivateFlowTests(test_private_tools.PrivateToolTests):
    async def test_owner_notes_in_private_stay_private(self):
        probe = self.probe([self_memory(text="主人最近在准备考研"), reply_json("记住啦")])
        self.assertTrue(await self.handle(probe, message(777, "小柠", "我最近在准备考研")))
        entries = probe.self_memories.entries()
        self.assertEqual([(e["text"], e["scope"]) for e in entries], [("主人最近在准备考研", "bili_dm")])
        self.assertEqual(probe.self_memories.entries("bili_comment"), [])

    # 这个类继承来的测试已经在 test_private_tools 里跑过了。
    for _name in [n for n in dir(test_private_tools.PrivateToolTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name


if __name__ == "__main__":
    unittest.main()
