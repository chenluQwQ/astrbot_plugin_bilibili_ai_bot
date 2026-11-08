"""模型调用的行为：熔断、探测、各用途分开、独立 API。

只从 ``_llm_call`` / ``_summary_llm_call`` 这些入口看行为，不碰内部字段，
所以调用和熔断搬到 ``core/infra/llm.py`` 前后，这份测试一个字不用改。
"""
import asyncio
import sys
import types
import unittest
from unittest.mock import patch

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from llm_fakes import LegacyModels


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.results = []
        self.gate = None

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.gate is not None:
            await self.gate.wait()
        result = self.results.pop(0) if self.results else "好"
        if isinstance(result, Exception):
            raise result
        return types.SimpleNamespace(completion_text=result)

    def get_using_provider(self):
        return types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id="chat"))


class Bot(LegacyModels):
    def __init__(self, **config):
        self.config = {"LLM_CIRCUIT_FAILURE_THRESHOLD": 2, "LLM_CIRCUIT_COOLDOWN_SECONDS": 100, **config}
        self.context = FakeProvider()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class CircuitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = patch("time.monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _open(self, bot):
        bot.context.results = [RuntimeError("boom"), RuntimeError("boom")]
        self.assertIsNone(await bot._llm_call("1"))
        self.assertIsNone(await bot._llm_call("2"))

    async def test_opens_after_threshold_and_skips_the_provider(self):
        bot = Bot()
        await self._open(bot)
        calls = len(bot.context.calls)
        self.assertIsNone(await bot._llm_call("3"))
        self.assertEqual(len(bot.context.calls), calls)
        self.assertIn("冷却", bot._last_llm_error)

    async def test_failures_below_threshold_stay_closed(self):
        bot = Bot()
        bot.context.results = [RuntimeError("boom"), "好", RuntimeError("boom"), "还好"]
        self.assertIsNone(await bot._llm_call("1"))
        self.assertEqual(await bot._llm_call("2"), "好")
        self.assertIsNone(await bot._llm_call("3"))
        self.assertEqual(await bot._llm_call("4"), "还好")

    async def test_successful_probe_closes(self):
        bot = Bot()
        await self._open(bot)
        self.clock.now += 101
        bot.context.results = ["恢复了"]
        self.assertEqual(await bot._llm_call("probe"), "恢复了")
        bot.context.results = [RuntimeError("boom")]
        self.assertIsNone(await bot._llm_call("again"))
        # 计数清零了：这一次失败还不够重新打开
        self.assertEqual(await bot._llm_call("next"), "好")

    async def test_failed_probe_reopens_right_away(self):
        bot = Bot()
        await self._open(bot)
        self.clock.now += 101
        bot.context.results = [RuntimeError("boom")]
        self.assertIsNone(await bot._llm_call("probe"))
        calls = len(bot.context.calls)
        self.assertIsNone(await bot._llm_call("skipped"))
        self.assertEqual(len(bot.context.calls), calls)
        self.clock.now += 101
        self.assertEqual(await bot._llm_call("probe again"), "好")

    async def test_only_one_probe_at_a_time(self):
        bot = Bot()
        await self._open(bot)
        self.clock.now += 101
        bot.context.gate = asyncio.Event()
        probe = asyncio.create_task(bot._llm_call("probe"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertIsNone(await bot._llm_call("second"))
        self.assertIn("探测", bot._last_llm_error)
        bot.context.gate.set()
        self.assertEqual(await probe, "好")

    async def test_cancelled_probe_lets_the_next_call_probe(self):
        bot = Bot()
        await self._open(bot)
        self.clock.now += 101
        bot.context.gate = asyncio.Event()
        probe = asyncio.create_task(bot._llm_call("probe"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        probe.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await probe
        bot.context.gate = None
        self.assertEqual(await bot._llm_call("next"), "好")

    async def test_in_flight_success_does_not_close_an_open_circuit(self):
        bot = Bot()
        gate = asyncio.Event()
        bot.context.gate = gate
        slow = asyncio.create_task(bot._llm_call("slow"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        bot.context.gate = None
        await self._open(bot)
        # 慢请求是打开之前发的，它现在成功不能把熔断关掉
        bot.context.results = ["迟到的结果"]
        gate.set()
        self.assertEqual(await slow, "迟到的结果")
        calls = len(bot.context.calls)
        self.assertIsNone(await bot._llm_call("skipped"))
        self.assertEqual(len(bot.context.calls), calls)

    async def test_threshold_zero_never_opens(self):
        bot = Bot(LLM_CIRCUIT_FAILURE_THRESHOLD=0)
        bot.context.results = [RuntimeError("boom")] * 5
        for _ in range(5):
            self.assertIsNone(await bot._llm_call("x"))
        self.assertEqual(len(bot.context.calls), 5)

    async def test_empty_answer_counts_as_failure(self):
        bot = Bot()
        bot.context.results = ["", ""]
        self.assertIsNone(await bot._llm_call("1"))
        self.assertEqual(bot._last_llm_error, "模型返回空内容")
        self.assertIsNone(await bot._llm_call("2"))
        calls = len(bot.context.calls)
        self.assertIsNone(await bot._llm_call("3"))
        self.assertEqual(len(bot.context.calls), calls)


class PurposeTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_settings_use_the_chat_model_and_its_circuit(self):
        bot = Bot()
        bot.context.results = [RuntimeError("boom"), RuntimeError("boom")]
        self.assertIsNone(await bot._summary_llm_call("1"))
        self.assertIsNone(await bot._generation_llm_call("2"))
        calls = len(bot.context.calls)
        self.assertIsNone(await bot._llm_call("3"))
        self.assertEqual(len(bot.context.calls), calls)

    async def test_own_provider_has_its_own_circuit(self):
        bot = Bot(SUMMARY_PROVIDER_ID="summary-model")
        bot.context.results = [RuntimeError("boom"), RuntimeError("boom")]
        self.assertIsNone(await bot._summary_llm_call("1"))
        self.assertIsNone(await bot._summary_llm_call("2"))
        self.assertEqual(bot.context.calls[0]["chat_provider_id"], "summary-model")
        calls = len(bot.context.calls)
        self.assertIsNone(await bot._summary_llm_call("skipped"))
        self.assertEqual(len(bot.context.calls), calls)
        self.assertEqual(await bot._llm_call("chat still works"), "好")
        self.assertEqual(bot.context.calls[-1]["chat_provider_id"], "chat")

    async def test_system_prompt_goes_to_the_system_role(self):
        bot = Bot()
        await bot._generation_llm_call("写", system_prompt="人设", max_tokens=50)
        call = bot.context.calls[-1]
        self.assertEqual(
            (call["prompt"], call["system_prompt"], call["max_tokens"]), ("写", "人设", 50)
        )


class FakeOpenAI:
    created = []
    result = "独立结果"

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _create(self, **kwargs):
        FakeOpenAI.created.append({**self.kwargs, **kwargs})
        if isinstance(FakeOpenAI.result, Exception):
            raise FakeOpenAI.result
        message = types.SimpleNamespace(content=FakeOpenAI.result)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class IndependentApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeOpenAI.created = []
        FakeOpenAI.result = "独立结果"
        modules = patch.dict(sys.modules, {"openai": types.SimpleNamespace(AsyncOpenAI=FakeOpenAI)})
        proxy = patch("core.network.openai_proxy_kwargs", lambda config: {})
        modules.start()
        proxy.start()
        self.addCleanup(modules.stop)
        self.addCleanup(proxy.stop)

    async def test_calls_the_configured_endpoint(self):
        bot = Bot(SUMMARY_API_KEY="sk-secret", SUMMARY_MODEL="m", SUMMARY_API_BASE="https://example.com")
        self.assertEqual(await bot._summary_llm_call("事实", system_prompt="人设", max_tokens=9), "独立结果")
        call = FakeOpenAI.created[0]
        self.assertEqual(call["base_url"], "https://example.com/v1")
        self.assertEqual(call["model"], "m")
        self.assertEqual(call["max_tokens"], 9)
        self.assertEqual(call["messages"], [
            {"role": "system", "content": "人设"}, {"role": "user", "content": "事实"},
        ])
        self.assertEqual(bot.context.calls, [])

    async def test_missing_model_is_an_error_without_a_request(self):
        bot = Bot(SUMMARY_API_KEY="sk-secret")
        self.assertIsNone(await bot._summary_llm_call("事实"))
        self.assertIn("未填写模型名", bot._last_llm_error)
        self.assertEqual(FakeOpenAI.created, [])

    async def test_failures_open_only_this_purpose_and_hide_the_key(self):
        bot = Bot(GENERATION_API_KEY="sk-secret", GENERATION_MODEL="m")
        FakeOpenAI.result = RuntimeError("bad key sk-secret")
        self.assertIsNone(await bot._generation_llm_call("1"))
        self.assertNotIn("sk-secret", bot._last_llm_error)
        self.assertIsNone(await bot._generation_llm_call("2"))
        self.assertEqual(len(FakeOpenAI.created), 2)
        self.assertIsNone(await bot._generation_llm_call("skipped"))
        self.assertEqual(len(FakeOpenAI.created), 2)
        self.assertEqual(await bot._llm_call("chat"), "好")


if __name__ == "__main__":
    unittest.main()
