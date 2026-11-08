"""Offline regression tests for audit findings F1/F4/F5.

F1: share short-link resolution must never send Bilibili login cookies to
    non-Bilibili hosts, and must not even request spoofed hosts.
F4: a cancelled half-open circuit-breaker probe must not deadlock the LLM
    circuit; the next call may probe again.
F5: delayed share cleanup must include the copied send_paths, so
    share_send_* directories do not accumulate on disk.

All tests are offline; no network and no real credentials.
"""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"


def _load_module(alias, filename, config_values):
    package_name = f"auditfix_{alias}_core"
    package = sys.modules.setdefault(package_name, types.ModuleType(package_name))
    package.__path__ = [str(CORE)]

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.logger = MagicMock()
    astrbot.api = api

    config_name = f"{package_name}.config"
    config = types.ModuleType(config_name)
    for key, value in config_values.items():
        setattr(config, key, value)
    sys.modules[config_name] = config

    # network module is a real dependency of share.py; load it under the pkg.
    network_name = f"{package_name}.network"
    if network_name not in sys.modules:
        spec_net = importlib.util.spec_from_file_location(network_name, CORE / "network.py")
        network = importlib.util.module_from_spec(spec_net)
        sys.modules[network_name] = network
        spec_net.loader.exec_module(network)

    module_name = f"{package_name}.{alias}"
    spec = importlib.util.spec_from_file_location(module_name, CORE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ShareHostValidationTests(unittest.TestCase):
    def setUp(self):
        from core.infra.bili import share_links

        self.module = share_links
        self.links = share_links.ShareLinks(types.SimpleNamespace(config={}))

    def test_allowed_hosts(self):
        for url in (
            "https://b23.tv/abc123",
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://m.bilibili.com/video/BV1xx411c7mD",
            "http://bili2233.cn/xyz",
        ):
            with self.subTest(url=url):
                self.assertTrue(self.module.ShareLinks._is_allowed_share_host(url))

    def test_spoofed_hosts_rejected(self):
        for url in (
            "http://127.0.0.1:8080/b23.tv/abc",
            "http://127.0.0.1/bilibili.com/video/BV1xx411c7mD",
            "https://b23.tv.evil.com/abc",
            "https://evilbilibili.com/video/BV1xx411c7mD",
            "https://bilibili.com.evil.com/x",
            "not a url at all",
            "",
        ):
            with self.subTest(url=url):
                self.assertFalse(self.module.ShareLinks._is_allowed_share_host(url))

    def test_extract_target_never_requests_spoofed_shortlink(self):
        async def run():
            # 伪造主机：路径里含 b23.tv，旧子串判断会误放行并发起请求。
            target = await self.links._extract_bili_share_target(
                "http://127.0.0.1:9/b23.tv/abc123"
            )
            return target

        with patch.object(
            self.links, "_resolve_share_url", new=AsyncMock()
        ) as resolver:
            result = asyncio.run(run())
        self.assertIsNone(result)
        resolver.assert_not_awaited()

    def test_resolve_share_url_does_not_send_cookies(self):
        captured = {}

        class FakeResponse:
            url = "https://www.bilibili.com/video/BV1xx411c7mD"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            def get(self, url, headers=None, **kwargs):
                captured["headers"] = headers or {}
                return FakeResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        async def run():
            with patch.object(
                self.module, "plugin_http_session", return_value=FakeSession()
            ):
                return await self.links._resolve_share_url("https://b23.tv/abc")

        result = asyncio.run(run())
        self.assertIn("bilibili.com/video/BV1xx411c7mD", result)
        cookie = captured["headers"].get("Cookie", "")
        self.assertEqual(cookie, "")
        self.assertNotIn("SESSDATA", str(captured["headers"]))

    def test_redirect_to_offsite_is_not_followed_for_target(self):
        async def run():
            with patch.object(
                self.links,
                "_resolve_share_url",
                new=AsyncMock(return_value="https://evil.com/video/BV1xx411c7mD"),
            ):
                return await self.links._extract_bili_share_target("https://b23.tv/abc")

        self.assertIsNone(asyncio.run(run()))


class ShareSendCleanupTests(unittest.TestCase):
    """F5: finally 分支必须把 send_paths（发送副本）并入延迟清理列表。"""

    def test_cleanup_includes_send_copies(self):
        source = (CORE / "features" / "_share_delivery.py").read_text(encoding="utf-8")
        finally_idx = source.index("finally:", source.index("async def _send_shared_video"))
        tail = source[finally_idx : finally_idx + 900]
        self.assertIn("list(send_paths)", tail)
        self.assertIn("list(raw_send_paths)", tail)
        self.assertLess(tail.index("list(raw_send_paths)"), tail.index("list(send_paths)"))


class LLMCircuitProbeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        import test_autonomous_ranges  # noqa: F401  装上假的 astrbot
        import llm_fakes
        cls.module = llm_fakes

    async def test_cancelled_probe_releases_half_open_slot(self):
        bot = self.module.LegacyModels()
        bot.config = {
            "LLM_CIRCUIT_FAILURE_THRESHOLD": 1,
            "LLM_CIRCUIT_COOLDOWN_SECONDS": 0.01,
        }

        calls = {"n": 0}

        class Resp:
            completion_text = "ok"

        class Context:
            def get_using_provider(self):
                provider = MagicMock()
                provider.meta = lambda: types.SimpleNamespace(id="test-provider")
                return provider

            async def llm_generate(self, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("boom")  # open the circuit
                if calls["n"] == 2:
                    await asyncio.sleep(60)  # probe gets cancelled below
                return Resp()

        bot.context = Context()

        # 1) failure opens the circuit (threshold=1)
        self.assertIsNone(await bot._llm_call("x"))
        circuit = bot._llm_gateway().circuit
        self.assertGreater(circuit.open_until, 0)

        # 2) cooldown 最小被钳到 1s，直接手动把冷却时间挪到过去，
        #    然后启动探测并在其挂起期间取消它
        circuit.open_until = 0.001  # 已过期的冷却（time.monotonic() 起点之后）
        probe_task = asyncio.create_task(bot._llm_call("x"))
        await asyncio.sleep(0.1)  # let the probe claim the half-open slot
        self.assertTrue(circuit.probing)
        probe_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await probe_task

        # 3) the half-open flag must be released, next call probes again
        self.assertFalse(circuit.probing)
        result = await bot._llm_call("x")
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
