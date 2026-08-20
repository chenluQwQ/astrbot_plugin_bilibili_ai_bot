"""Regression tests for the read-only Web interest snapshot."""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _install_astrbot_stub():
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    astrbot = sys.modules.get("astrbot") or types.ModuleType("astrbot")
    astrbot.__path__ = getattr(astrbot, "__path__", [])
    api = sys.modules.get("astrbot.api") or types.ModuleType("astrbot.api")
    api.__path__ = getattr(api, "__path__", [])
    api.logger = getattr(api, "logger", _Logger())
    star = sys.modules.get("astrbot.api.star") or types.ModuleType("astrbot.api.star")
    star.Context = object
    if not hasattr(star, "StarTools"):
        star.StarTools = types.SimpleNamespace(
            get_data_dir=lambda _name: Path(tempfile.mkdtemp(prefix="bilibot-web-test-"))
        )
    web = sys.modules.get("astrbot.api.web") or types.ModuleType("astrbot.api.web")
    web.error_response = lambda message, status_code=400: {
        "status": "error", "message": message, "status_code": status_code
    }
    web.json_response = lambda payload: payload
    web.request = types.SimpleNamespace(json=lambda default=None: default)
    api.star = star
    api.web = web
    astrbot.api = api
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": star,
        "astrbot.api.web": web,
    })


_install_astrbot_stub()

from core import webui_bridge


class _PreferenceStore:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = 0

    async def current(self, *, limit=20):
        self.calls += 1
        if self.fail:
            raise RuntimeError("C:/secret/database.sqlite locked")
        return [{"signal_type": "topic", "value": "深海", "stage": "recent"}][:limit]


class _Plugin:
    def __init__(self, store):
        self.layered_runtime = types.SimpleNamespace(is_open=True, preferences=store)

    def _load_json(self, _path, default):
        return {
            "current": [{"signal_type": "topic", "value": "本地副本", "stage": "candidate"}]
        }

    def _format_interest_report(self, *, lifecycle_items=None):
        value = (lifecycle_items or [{}])[0].get("value", "暂无")
        return f"🎯 BiliBot 视频兴趣\n【已沉淀偏好】\n  · {value}"


class WebInterestTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_snapshot_is_cached(self):
        store = _PreferenceStore()
        plugin = _Plugin(store)

        first = await webui_bridge._get_web_interest_payload(plugin)
        second = await webui_bridge._get_web_interest_payload(plugin)

        self.assertEqual(store.calls, 1)
        self.assertEqual(first["source"], "runtime")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertTrue(second["read_only"])

    async def test_repeated_database_failure_opens_gentle_breaker(self):
        store = _PreferenceStore(fail=True)
        plugin = _Plugin(store)

        with patch.object(webui_bridge, "WEB_INTEREST_CACHE_TTL_SECONDS", 0):
            for _ in range(webui_bridge.WEB_INTEREST_BREAKER_THRESHOLD):
                result = await webui_bridge._get_web_interest_payload(plugin)
                self.assertIn("本地副本", result["report"])
            breaker_result = await webui_bridge._get_web_interest_payload(plugin)

        self.assertEqual(store.calls, webui_bridge.WEB_INTEREST_BREAKER_THRESHOLD)
        self.assertEqual(breaker_result["source"], "circuit_fallback")
        self.assertTrue(breaker_result["stale"])
        self.assertNotIn("C:/secret", breaker_result["report"])

    def test_display_text_strips_control_characters_and_is_bounded(self):
        value = "安全\x00文本" + "x" * 100
        result = webui_bridge._safe_display_text(value, max_chars=12)
        self.assertNotIn("\x00", result)
        self.assertLessEqual(len(result), 12)


if __name__ == "__main__":
    unittest.main()
