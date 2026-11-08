"""B站消息走 AstrBot 工具循环的地基（A1-1）：次数和去重、B站事件、模型调用接到 ``tool_loop_agent``。

AstrBot 的类在这里换成最小的假类，只看我们交给 AstrBot 的东西对不对。
"""
import asyncio
import sys
import types
import unittest
from unittest.mock import patch

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from llm_fakes import LegacyModels

from core.infra import bili_event
from core.infra.tool_loop import ToolMiss, ToolRun
from tool_fakes import FakeFunctionTool, fake_platform_modules, fake_tool_module


class ToolRunTests(unittest.IsolatedAsyncioTestCase):
    def run_with(self, **kwargs):
        return ToolRun(wrap=lambda text, name: f"<{name}>{text}</{name}>", **kwargs)

    async def test_stops_after_the_call_budget(self):
        run = self.run_with(max_calls=2)
        calls = []

        async def lookup(q):
            calls.append(q)
            return f"结果{q}"

        self.assertEqual(await run.invoke("search", {"q": "a"}, lookup), "<search>结果a</search>")
        await run.invoke("search", {"q": "b"}, lookup)
        refused = await run.invoke("search", {"q": "c"}, lookup)
        self.assertEqual(calls, ["a", "b"])
        self.assertIn("次数用完", refused)
        self.assertEqual([row["status"] for row in run.trace], ["ok", "ok", "budget"])

    async def test_zero_budget_never_runs_a_tool(self):
        run = self.run_with(max_calls=0)

        async def lookup():
            raise AssertionError("不该执行")

        self.assertIn("次数用完", await run.invoke("search", {}, lookup))

    async def test_same_request_runs_once_and_does_not_use_budget(self):
        run = self.run_with(max_calls=3)
        calls = []

        async def lookup(q):
            calls.append(q)
            return "有"

        await run.invoke("search", {"q": "a"}, lookup)
        self.assertIn("已经做过", await run.invoke("search", {"q": "a", "empty": ""}, lookup))
        self.assertEqual(calls, ["a"])
        self.assertEqual(run.used, 1)

    async def test_per_tool_limit(self):
        run = self.run_with(max_calls=6, limits={"inspect": 2})

        async def lookup(q):
            return "看了"

        for q in ("1", "2"):
            await run.invoke("inspect", {"q": q}, lookup)
        self.assertIn("次数用完", await run.invoke("inspect", {"q": "3"}, lookup))
        self.assertEqual(await run.invoke("search", {"q": "3"}, lookup), "<search>看了</search>")

    async def test_long_arguments_are_shortened_in_the_trace(self):
        run = self.run_with(max_calls=1)
        seen = []

        async def lookup(q):
            seen.append(q)
            return "有"

        await run.invoke("search", {"q": "长" * 500}, lookup)
        self.assertEqual(len(seen[0]), 500)
        self.assertEqual(len(run.trace[0]["args"]["q"]), 200)

    async def test_known_miss_tells_the_model_why(self):
        run = self.run_with(max_calls=3)

        async def no_subtitles():
            raise ToolMiss("这个视频没有字幕")

        self.assertEqual(await run.invoke("subtitles", {}, no_subtitles), "这个视频没有字幕")
        self.assertEqual(run.trace[-1]["status"], "unavailable")
        self.assertEqual(run.used, 1)

    async def test_per_tool_timeout(self):
        run = self.run_with(max_calls=3, timeout=0.01, timeouts={"inspect": 1})

        async def a_bit_slow():
            await asyncio.sleep(0.05)
            return "看完了"

        self.assertIn("超时", await run.invoke("search", {}, a_bit_slow))
        self.assertEqual(await run.invoke("inspect", {}, a_bit_slow), "<inspect>看完了</inspect>")

    async def test_stop_refuses_everything_until_resumed(self):
        run = self.run_with(max_calls=3)

        async def lookup():
            return "有"

        run.stop("转后台了")
        self.assertEqual(await run.invoke("search", {}, lookup), "转后台了")
        self.assertEqual((run.used, run.trace[-1]["status"]), (0, "stopped"))
        run.resume()
        self.assertEqual(await run.invoke("search", {}, lookup), "<search>有</search>")

    async def test_answers_keep_what_the_model_saw_including_late_results(self):
        run = self.run_with(max_calls=3)

        async def lookup(q):
            return "结果" + q

        await run.invoke("search", {"q": "a"}, lookup)
        run.record("inspect", {"query": "猫"}, "ok", "有猫", 12.3)
        self.assertEqual(run.answers, [
            {"tool": "search", "args": {"q": "a"}, "text": "<search>结果a</search>"},
            {"tool": "inspect", "args": {"query": "猫"}, "text": "<inspect>有猫</inspect>"},
        ])
        self.assertEqual(run.trace_line(), "search(ok) → inspect(ok)")

    async def test_failures_tell_the_model_without_wrapping_or_raising(self):
        run = self.run_with(max_calls=3, timeout=0.01)

        async def slow():
            await asyncio.sleep(1)

        async def broken():
            raise RuntimeError("boom")

        async def empty():
            return ""

        self.assertEqual(await run.invoke("slow", {}, slow), "查询超时，没有取得可靠结果。")
        self.assertEqual(await run.invoke("broken", {}, broken), "查询失败，没有取得可靠结果。")
        self.assertEqual(await run.invoke("empty", {}, empty), "没有查到结果。")
        self.assertEqual([row["status"] for row in run.trace], ["timeout", "failed", "empty"])
        self.assertEqual(run.trace_line(), "slow(timeout) → broken(failed) → empty(empty)")

    async def test_tool_goes_through_the_budget_when_astrbot_calls_its_handler(self):
        run = self.run_with(max_calls=1)

        async def lookup(q):
            return q

        with patch.dict(sys.modules, {"astrbot.core.agent.tool": fake_tool_module()}):
            tool = run.tool(name="search", description="搜", parameters={"type": "object"}, impl=lookup)
        self.assertEqual(tool.name, "search")
        self.assertEqual(await tool.handler(object(), q="x"), "<search>x</search>")
        self.assertIn("次数用完", await tool.handler(object(), q="y"))


class BiliEventTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(sys.modules, fake_platform_modules())
        patcher.start()
        self.addCleanup(patcher.stop)
        bili_event._event_class = None
        self.addCleanup(setattr, bili_event, "_event_class", None)

    def test_private_message_is_a_member_event_on_the_bilibili_platform(self):
        event = bili_event.make_bili_event(channel="private", uid=123, text="在吗", nickname="路人", self_uid="9")
        self.assertFalse(event.is_admin())
        self.assertEqual(event.platform_meta.name, "bilibili")
        self.assertEqual(event.unified_msg_origin, "bilibili_ai_bot:FriendMessage:private:123")
        self.assertEqual(event.message_obj.sender.user_id, "123")
        self.assertEqual(event.message_str, "在吗")

    def test_comment_and_private_sessions_differ(self):
        comment = bili_event.make_bili_event(channel="comment", uid=123, text="x")
        private = bili_event.make_bili_event(channel="private", uid=123, text="x")
        self.assertNotEqual(comment.unified_msg_origin, private.unified_msg_origin)

    def test_unknown_channel_is_rejected(self):
        with self.assertRaises(ValueError):
            bili_event.make_bili_event(channel="qq", uid=1, text="x")

    def test_event_cannot_send(self):
        event = bili_event.make_bili_event(channel="private", uid=1, text="x")
        with self.assertRaises(RuntimeError):
            asyncio.run(event.send(None))


class LoopContext:
    def __init__(self):
        self.loop_calls = []
        self.generate_calls = []
        self.results = []

    async def tool_loop_agent(self, **kwargs):
        self.loop_calls.append(kwargs)
        result = self.results.pop(0) if self.results else "好"
        if isinstance(result, Exception):
            raise result
        return types.SimpleNamespace(completion_text=result)

    async def llm_generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        return types.SimpleNamespace(completion_text="直接回复")

    def get_using_provider(self):
        return types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id="chat"))


class Bot(LegacyModels):
    def __init__(self, **config):
        self.config = {"LLM_CIRCUIT_FAILURE_THRESHOLD": 2, "LLM_CIRCUIT_COOLDOWN_SECONDS": 100, **config}
        self.context = LoopContext()


class GatewayToolLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.dict(sys.modules, {"astrbot.core.agent.tool": fake_tool_module()})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_passes_only_our_tools_and_the_step_limit(self):
        bot = Bot()
        event, tools = object(), ["t1", "t2"]
        text = await bot.models.tool_loop("问题", "人设", event=event, tools=tools, max_steps=3,
                                          tool_call_timeout=45)
        self.assertEqual(text, "好")
        call = bot.context.loop_calls[0]
        self.assertIs(call["event"], event)
        self.assertEqual(call["tools"].tools, tools)
        self.assertEqual((call["max_steps"], call["tool_call_timeout"]), (3, 45))
        self.assertEqual((call["chat_provider_id"], call["prompt"], call["system_prompt"]), ("chat", "问题", "人设"))

    async def test_no_tools_is_a_plain_call(self):
        bot = Bot()
        self.assertEqual(await bot.models.tool_loop("问题", event=object(), tools=[], max_steps=3), "直接回复")
        self.assertEqual(bot.context.loop_calls, [])

    async def test_failures_open_the_same_circuit_as_plain_calls(self):
        bot = Bot()
        bot.context.results = [RuntimeError("boom"), RuntimeError("boom")]
        for _ in range(2):
            self.assertIsNone(await bot.models.tool_loop("问题", event=object(), tools=["t"], max_steps=3))
        self.assertIsNone(await bot._llm_call("普通调用"))
        self.assertEqual(bot.context.generate_calls, [])
        self.assertIn("冷却", bot._last_llm_error)

    async def test_overall_timeout_counts_as_a_circuit_failure(self):
        bot = Bot()

        async def hangs(**_kwargs):
            await asyncio.sleep(1)

        bot.context.tool_loop_agent = hangs
        for _ in range(2):
            self.assertIsNone(await bot.models.tool_loop("问题", event=object(), tools=["t"], max_steps=3, timeout=0.01))
        self.assertIn("没有完成", bot._last_llm_error)
        self.assertIsNone(await bot._llm_call("普通调用"))
        self.assertIn("冷却", bot._last_llm_error)
        self.assertEqual(bot.context.generate_calls, [])

    async def test_empty_final_answer_is_a_failure(self):
        bot = Bot()
        bot.context.results = [""]
        self.assertIsNone(await bot.models.tool_loop("问题", event=object(), tools=["t"], max_steps=3))
        self.assertEqual(bot._last_llm_error, "模型返回空内容")


if __name__ == "__main__":
    unittest.main()
