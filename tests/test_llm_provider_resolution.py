"""Regression tests for AstrBot 4.27+ chat provider resolution."""
import asyncio
from types import SimpleNamespace

from core.llm import LLMMixin


class FakeProvider:
    def __init__(self, provider_id):
        self._provider_id = provider_id

    def meta(self):
        return SimpleNamespace(id=self._provider_id)


class FakeResponse:
    completion_text = "generated"


class FakeContext:
    def __init__(self, provider=None):
        self.provider = provider
        self.calls = []

    def get_using_provider(self):
        return self.provider

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse()


class FakeBot(LLMMixin):
    def __init__(self, config, context):
        self.config = config
        self.context = context


def test_llm_call_uses_astrbot_default_provider_when_plugin_override_is_empty():
    async def run():
        context = FakeContext(FakeProvider("default-chat"))
        bot = FakeBot({"LLM_PROVIDER_ID": ""}, context)
        result = await bot._llm_call("plan")
        assert result == "generated"
        assert context.calls[0]["chat_provider_id"] == "default-chat"

    asyncio.run(run())


def test_llm_call_prefers_explicit_plugin_provider():
    async def run():
        context = FakeContext(FakeProvider("default-chat"))
        bot = FakeBot({"LLM_PROVIDER_ID": "configured-chat"}, context)
        await bot._llm_call("plan")
        assert context.calls[0]["chat_provider_id"] == "configured-chat"

    asyncio.run(run())
