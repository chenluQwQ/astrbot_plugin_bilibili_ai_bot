"""Regression tests for AstrBot 4.27+ chat provider resolution."""

import sys
import tempfile
import types
import unittest
from pathlib import Path


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _install_astrbot_stub():
    """Import core.* without a real AstrBot install (matches the other test modules)."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    if isinstance(getattr(sys.modules.get("astrbot"), "api", None), types.ModuleType):
        return
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.logger = _Logger()
    star = types.ModuleType("astrbot.api.star")
    data_dir = Path(tempfile.mkdtemp(prefix="bilibot-test-"))
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda _name: data_dir)
    event = types.ModuleType("astrbot.api.event")

    class _MessageChain:
        def __init__(self, *_args, **_kwargs):
            self.chain = []

        def message(self, *_args, **_kwargs):
            return self

    event.MessageChain = _MessageChain
    api.star = star
    api.event = event
    astrbot.api = api
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": star,
        "astrbot.api.event": event,
    })


_install_astrbot_stub()

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


def _check_llm_call_uses_astrbot_default_provider_when_plugin_override_is_empty():
    async def run():
        context = FakeContext(FakeProvider("default-chat"))
        bot = FakeBot({"LLM_PROVIDER_ID": ""}, context)
        result = await bot._llm_call("plan")
        assert result == "generated"
        assert context.calls[0]["chat_provider_id"] == "default-chat"

    asyncio.run(run())


def _check_llm_call_prefers_explicit_plugin_provider():
    async def run():
        context = FakeContext(FakeProvider("default-chat"))
        bot = FakeBot({"LLM_PROVIDER_ID": "configured-chat"}, context)
        await bot._llm_call("plan")
        assert context.calls[0]["chat_provider_id"] == "configured-chat"

    asyncio.run(run())


class LLMProviderResolutionTests(unittest.TestCase):
    test_llm_call_uses_astrbot_default_provider_when_plugin_override_is_empty = staticmethod(_check_llm_call_uses_astrbot_default_provider_when_plugin_override_is_empty)
    test_llm_call_prefers_explicit_plugin_provider = staticmethod(_check_llm_call_prefers_explicit_plugin_provider)
