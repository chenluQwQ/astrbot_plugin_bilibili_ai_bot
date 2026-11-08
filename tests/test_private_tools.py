"""私信回复前的工具（A1-3）：给模型哪些工具、白名单里的别名、次数和时限、看视频前那句话。"""
import random
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_private_reply_snapshot import BV, PrivateProbe, message
from test_comment_reply_snapshot import FixedDatetime, reply_json
from reply_modules import patched_in_reply_modules
from tool_fakes import WithToolFakes, call


class PrivateToolTests(WithToolFakes, unittest.IsolatedAsyncioTestCase):
    def probe(self, llm=(), **config):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return PrivateProbe(directory.name, config, llm=list(llm))

    async def handle(self, probe, msg):
        fake_uuid = types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex="0123456789abcdef"))
        with patched_in_reply_modules(datetime=FixedDatetime, random=random.Random(1), uuid=fake_uuid), \
                patch("time.time", lambda: 1761373800.0):
            return await probe.private_reply._handle_private_message(msg, [])

    def calls(self, probe, kind):
        return [row[1:] for row in probe.calls if row[0] == kind]

    def test_aliases_in_the_allowlist_map_to_one_tool(self):
        probe = self.probe(BILI_TOOL_ALLOWLIST=["get_up_info", "search_bilibili", "watch_video"])
        self.assertEqual(probe.private_reply._private_tool_names(),
                         {"bili_up_info": "get_up_info", "bili_video_search": "search_bilibili",
                          "watch_video": "watch_video", "bili_memo": "bili_memo"})

    def test_switches_gate_the_tools(self):
        allow = ["bili_up_info", "web_search"]
        probe = self.probe(BILI_TOOL_ALLOWLIST=allow, ENABLE_WEB_SEARCH=True)
        self.assertEqual(set(probe.private_reply._private_tool_names()), {"bili_up_info", "web_search", "bili_memo"})
        probe = self.probe(BILI_TOOL_ALLOWLIST=allow, ENABLE_WEB_SEARCH=True, PRIVATE_MESSAGE_BILI_SEARCH_ENABLED=False)
        self.assertEqual(set(probe.private_reply._private_tool_names()), {"web_search", "bili_memo"})
        # 备忘录不是B站查询：不看工具白名单，只看自己的开关。
        probe = self.probe(BILI_TOOL_ALLOWLIST=allow, BILI_ALLOW_SEARCH_TOOLS=False, ENABLE_WEB_SEARCH=True)
        self.assertEqual(probe.private_reply._private_tool_names(), {"bili_memo": "bili_memo"})
        probe = self.probe(BILI_TOOL_ALLOWLIST=allow, BILI_ALLOW_SEARCH_TOOLS=False, ENABLE_MEMO=False)
        self.assertEqual(probe.private_reply._private_tool_names(), {})

    async def test_alias_is_what_the_lookup_receives(self):
        probe = self.probe([call("bili_up_info", query="泛式"), reply_json("查到了")],
                           BILI_TOOL_ALLOWLIST=["get_up_info"])
        probe.private_lookup._execute_private_model_tool = AsyncMock(return_value="UP 资料")
        self.assertTrue(await self.handle(probe, message(777, "小柠", "泛式最近发了什么")))
        request = probe.private_lookup._execute_private_model_tool.await_args.args[0]
        self.assertEqual(request, {"name": "get_up_info", "query": "泛式", "view": None, "kind": None,
                                   "target": ""})

    async def test_memory_tool_reads_as_the_dm_domain(self):
        probe = self.probe([call("bili_memory", kind="dynamic", query="手绘"), reply_json("记得")])
        probe.memory_recall = types.SimpleNamespace(_search_memories=AsyncMock(return_value=["发过手绘动态"]))
        await self.handle(probe, message(777, "小柠", "你发过手绘的动态吗"))
        self.assertEqual(probe.memory_recall._search_memories.await_args.kwargs["reader_scope"], "bili_dm")

    async def test_lookups_share_the_comment_budget(self):
        probe = self.probe([call("bili_up_info", query=str(i)) for i in range(3)] + [reply_json("好")],
                           COMMENT_CONTEXT_MAX_CALLS=2)
        probe.private_lookup._execute_private_model_tool = AsyncMock(return_value="资料")
        await self.handle(probe, message(777, "小柠", "帮我查三个UP"))
        self.assertEqual(probe.private_lookup._execute_private_model_tool.await_count, 2)
        loop = self.calls(probe, "tool_loop")[0]
        self.assertEqual(loop[2], 3)  # max_steps
        self.assertIn("次数用完", self.calls(probe, "tool_result")[2][1])

    async def test_one_watch_per_message_and_one_progress_line(self):
        probe = self.probe([call("watch_video", query=BV, progress_reply="我去看看"),
                            call("watch_video", query="BV1yy411c7mD", progress_reply="再看一个"),
                            reply_json("看完了")])
        await self.handle(probe, message(777, "小柠", "帮我看两个"))
        self.assertEqual(len(self.calls(probe, "watch_video")), 1)
        self.assertEqual([row[1] for row in self.calls(probe, "send_private")], ["我去看看", "看完了"])

    async def test_non_text_and_suspicious_messages_get_no_tools(self):
        probe = self.probe([reply_json("看完啦")])
        await self.handle(probe, message(777, "小柠", f"https://www.bilibili.com/video/{BV}", kind="video_share"))
        self.assertEqual(self.calls(probe, "tool_loop")[0][1], [])

    def test_overall_timeout_counts_every_remaining_call(self):
        reply = self.probe().private_reply
        run = reply._private_tool_run()
        # 测试配置里次数是 2：看一次视频 + 一次普通查询，再加 3 轮模型。
        self.assertEqual(reply._private_loop_timeout(run, ["bili_up_info", "watch_video"]), 900 + 45 + 3 * 90)
        self.assertEqual(reply._private_loop_timeout(run, []), 90)


if __name__ == "__main__":
    unittest.main()
