"""Proxy routing tests use local fake endpoints, never Bilibili or a paid API."""
import asyncio
import importlib.util
import os
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from core.network import (
    normalize_proxy_url, configured_proxy, plugin_http_session,
    openai_proxy_kwargs, download_proxy_args, redact_proxy_error,
    close_plugin_clients,
)


class ProxyConfigTests(unittest.TestCase):
    def test_empty_setting_keeps_existing_behavior(self):
        self.assertEqual(configured_proxy({}), "")
        self.assertEqual(openai_proxy_kwargs({}), {})
        self.assertEqual(download_proxy_args({}), [])

    def test_valid_schemes_ipv6_and_auth(self):
        for value in ("http://localhost:7890", "https://user:pass@localhost:8443", "socks5://[::1]:1080"):
            self.assertEqual(normalize_proxy_url(value + "/"), value)
        self.assertEqual(normalize_proxy_url("socks5h://localhost"), "socks5://localhost:1080")

    def test_invalid_urls_never_echo_credentials(self):
        for value in ("localhost:7890", "file:///tmp/test", "http://", "http://host:99999", "http://host:0", "http://u:secret@host/subscription?key=secret", "http://host\n/path", 123):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                normalize_proxy_url(value)
            self.assertNotIn("secret", str(caught.exception))

    def test_download_proxy_and_password_redaction(self):
        config = {"PROXY_URL": "socks5h://alice:p%40ssword@localhost:1080"}
        args = download_proxy_args(config)
        self.assertEqual(args, ["--proxy", "socks5://alice:p%40ssword@localhost:1080"])
        clean = redact_proxy_error(f"{config['PROXY_URL']} {args[1]} password=p@ssword", config)
        self.assertNotIn("p%40ssword", clean)
        self.assertNotIn("p@ssword", clean)

    def test_all_direct_http_sessions_use_shared_network_entry(self):
        core = Path(__file__).resolve().parents[1] / "core"
        for path in core.glob("*.py"):
            if path.name != "network.py":
                self.assertNotIn("aiohttp.ClientSession(", path.read_text(encoding="utf-8"), path.name)


class ProxyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.server = await asyncio.start_server(self.handle_http, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.proxy = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def handle_http(self, reader, writer):
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            self.requests.append(headers.decode("latin1"))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"ok":true}')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_http_get_and_post_use_proxy_without_global_changes(self):
        before = dict(os.environ)
        async with plugin_http_session({"PROXY_URL": self.proxy}) as session:
            async with session.get("http://bilibot.invalid/read", timeout=aiohttp.ClientTimeout(total=3)) as response:
                self.assertTrue((await response.json())["ok"])
            async with session.post("http://bilibot.invalid/write", json={"test": True}, timeout=aiohttp.ClientTimeout(total=3)) as response:
                self.assertTrue((await response.json())["ok"])
        self.assertTrue(self.requests[0].startswith("GET http://bilibot.invalid/read "))
        self.assertTrue(self.requests[1].startswith("POST http://bilibot.invalid/write "))
        self.assertEqual(dict(os.environ), before)

    async def test_blank_setting_does_not_add_proxy_to_requests(self):
        async with plugin_http_session({}) as session:
            async with session.get(self.proxy + "/direct") as response:
                await response.read()
        self.assertTrue(self.requests[0].startswith("GET /direct "))

    async def test_proxy_auth_is_separate_from_destination_auth(self):
        proxy = self.proxy.replace("://", "://alice:password@")
        async with plugin_http_session({"PROXY_URL": proxy}) as session:
            async with session.get("http://bilibot.invalid/auth", headers={"Authorization": "Bearer destination-token"}) as response:
                await response.read()
        self.assertIn("Proxy-Authorization: Basic ", self.requests[0])
        self.assertIn("Authorization: Bearer destination-token", self.requests[0])

    async def test_failed_proxy_is_sanitized_and_never_falls_back(self):
        proxy = "http://user:private-password@127.0.0.1:7890"
        raw = MagicMock()
        raw.__aenter__ = AsyncMock(return_value=raw)
        raw.__aexit__ = AsyncMock(return_value=False)
        raw.get.side_effect = aiohttp.ClientError(f"failed {proxy}")
        with patch("core.network.aiohttp.ClientSession", return_value=raw) as factory:
            with self.assertRaises(aiohttp.ClientError) as caught:
                async with plugin_http_session({"PROXY_URL": proxy}) as session:
                    session.get("http://bilibot.invalid")
        self.assertNotIn("private-password", str(caught.exception))
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(raw.get.call_count, 1)

    @unittest.skipUnless(importlib.util.find_spec("openai"), "requires plugin OpenAI dependency")
    async def test_independent_sdk_uses_same_proxy(self):
        client = openai_proxy_kwargs({"PROXY_URL": self.proxy})["http_client"]
        try:
            result = await client.get("http://bilibot.invalid/sdk")
            self.assertTrue(result.json()["ok"])
        finally:
            await client.aclose()
        self.assertTrue(self.requests[0].startswith("GET http://bilibot.invalid/sdk "))

    @unittest.skipUnless(importlib.util.find_spec("aiohttp_socks"), "requires SOCKS dependency")
    async def test_socks5_auth_and_remote_dns_for_http_and_sdk(self):
        domains = []

        async def handle_socks(reader, writer):
            try:
                greeting = await reader.readexactly(2)
                methods = await reader.readexactly(greeting[1])
                self.assertIn(2, methods)
                writer.write(b"\x05\x02")
                await writer.drain()
                auth = await reader.readexactly(2)
                self.assertEqual(auth[0], 1)
                self.assertEqual(await reader.readexactly(auth[1]), b"alice")
                password_size = (await reader.readexactly(1))[0]
                self.assertEqual(await reader.readexactly(password_size), b"password")
                writer.write(b"\x01\x00")
                await writer.drain()
                connect = await reader.readexactly(4)
                self.assertEqual(connect, b"\x05\x01\x00\x03")
                size = (await reader.readexactly(1))[0]
                domains.append((await reader.readexactly(size)).decode())
                await reader.readexactly(2)
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
                await writer.drain()
                await self.handle_http(reader, writer)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle_socks, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            config = {"PROXY_URL": f"socks5h://alice:password@127.0.0.1:{port}"}
            async with plugin_http_session(config) as session:
                async with session.get("http://bilibot.invalid/socks", timeout=aiohttp.ClientTimeout(total=3)) as response:
                    self.assertTrue((await response.json())["ok"])
            if importlib.util.find_spec("openai"):
                client = openai_proxy_kwargs(config)["http_client"]
                try:
                    result = await client.get("http://bilibot.invalid/sdk-socks")
                    self.assertTrue(result.json()["ok"])
                finally:
                    await client.aclose()
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual(domains, ["bilibot.invalid"] * (2 if importlib.util.find_spec("openai") else 1))

    async def test_shutdown_closes_only_plugin_clients(self):
        client = types.SimpleNamespace(close=AsyncMock())
        provider = types.SimpleNamespace(close=AsyncMock())
        plugin = types.SimpleNamespace(_embed_client=client, context=provider)
        await close_plugin_clients(plugin)
        client.close.assert_awaited_once()
        provider.close.assert_not_awaited()
        self.assertIsNone(plugin._embed_client)


if __name__ == "__main__":
    unittest.main()
