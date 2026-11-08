"""Regression tests for the read-only Web interest snapshot."""

from base_parts import BaseParts
import sys
import tempfile
import types
import unittest

from itself import itself
from pathlib import Path
from unittest.mock import AsyncMock, patch


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

from core.web import config_page, interest
from core.web._common import _safe_display_text


class _PreferenceStore:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = 0

    async def current(self, *, limit=20):
        self.calls += 1
        if self.fail:
            raise RuntimeError("C:/secret/database.sqlite locked")
        return [{"signal_type": "topic", "value": "深海", "stage": "recent"}][:limit]


class _Plugin(BaseParts):
    taste = itself

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

        first = await interest._get_web_interest_payload(plugin)
        second = await interest._get_web_interest_payload(plugin)

        self.assertEqual(store.calls, 1)
        self.assertEqual(first["source"], "runtime")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertTrue(second["read_only"])

    async def test_repeated_database_failure_opens_gentle_breaker(self):
        store = _PreferenceStore(fail=True)
        plugin = _Plugin(store)

        with patch.object(interest, "WEB_INTEREST_CACHE_TTL_SECONDS", 0):
            for _ in range(interest.WEB_INTEREST_BREAKER_THRESHOLD):
                result = await interest._get_web_interest_payload(plugin)
                self.assertIn("本地副本", result["report"])
            breaker_result = await interest._get_web_interest_payload(plugin)

        self.assertEqual(store.calls, interest.WEB_INTEREST_BREAKER_THRESHOLD)
        self.assertEqual(breaker_result["source"], "circuit_fallback")
        self.assertTrue(breaker_result["stale"])
        self.assertNotIn("C:/secret", breaker_result["report"])

    def test_display_text_strips_control_characters_and_is_bounded(self):
        value = "安全\x00文本" + "x" * 100
        result = _safe_display_text(value, max_chars=12)
        self.assertNotIn("\x00", result)
        self.assertLessEqual(len(result), 12)


class WebProxyConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_is_validated_before_any_config_is_written(self):
        class Config(dict):
            def save_config(self):
                self.saved = True

        config = Config(PROXY_URL="")
        plugin = types.SimpleNamespace(config=config)
        for value in ("http://user:secret@host/subscribe", "socks4://host:1080"):
            body = {"OWNER_NAME": "must not change", "PROXY_URL": value}
            with patch.object(config_page, "request", types.SimpleNamespace(json=AsyncMock(return_value=body))):
                response = await config_page.handle_save_config(plugin)
            self.assertEqual(response["status"], "error")
            self.assertNotIn("secret", response["message"])
            self.assertNotIn("OWNER_NAME", config)
            self.assertFalse(getattr(config, "saved", False))

        body = {"PROXY_URL": "socks5h://localhost:1080/"}
        with patch.object(config_page, "request", types.SimpleNamespace(json=AsyncMock(return_value=body))):
            response = await config_page.handle_save_config(plugin)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(config["PROXY_URL"], "socks5://localhost:1080")
        self.assertIn("重载", response["message"])
        self.assertTrue(config.saved)



class MergedConfigTests(unittest.IsolatedAsyncioTestCase):
    """管理页合并掉的项：保存显示的那个时，把藏起来的写成一样的值。"""

    async def save(self, config, body):
        app = types.SimpleNamespace(forget_schedule_states=lambda: None, clear_day_schedules=lambda: None)
        plugin = types.SimpleNamespace(config=config, app=app)
        with patch.object(config_page, "request", types.SimpleNamespace(json=AsyncMock(return_value=body))):
            return await config_page.handle_save_config(plugin)

    async def test_hidden_twins_follow_the_shown_key(self):
        class Config(dict):
            def save_config(self):
                pass

        config = Config(PROACTIVE_COMMENT_COUNT=0)
        response = await self.save(config, {
            "PROACTIVE_TIMES_COUNT": 3, "DYNAMIC_DAILY_COUNT": 1,
            "MEMORY_ISOLATION_MODE": "safe_share", "PROACTIVE_COMMENT": True,
        })
        self.assertEqual(response["status"], "ok")
        self.assertEqual(config["AUTONOMOUS_PROACTIVE_DAILY_MAX"], 3)
        self.assertEqual(config["AUTONOMOUS_DYNAMIC_DAILY_MAX"], 1)
        self.assertIs(config["ENABLE_SAFE_CROSS_PLATFORM_MEMORY"], True)
        self.assertEqual(config["PROACTIVE_COMMENT_COUNT"], 1)

        await self.save(config, {"MEMORY_ISOLATION_MODE": "isolated"})
        self.assertIs(config["ENABLE_SAFE_CROSS_PLATFORM_MEMORY"], False)


if __name__ == "__main__":
    unittest.main()
