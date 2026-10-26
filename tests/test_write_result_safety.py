import asyncio
import inspect
import json
import tempfile
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from core.affection import AffectionMixin
from core.bangumi import BangumiMixin
from core.bilibili import BilibiliAPIMixin
from core.layered_runtime import LayeredRuntime
from core.live_danmaku import LiveDanmakuMixin
from core.private_messages import PrivateMessageMixin
from core.runtime import (
    ActionRequest,
    ActionOutcome,
    EventRuntime,
    WriteResultUnknownError,
)
from core.tools import create_tools
from core.utils import UtilsMixin
from core.weekly import WeeklySummaryMixin


class FakeResponse:
    def __init__(self, payload=None, json_error=None):
        self.payload = payload
        self.json_error = json_error

    async def json(self, content_type=None):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeRequestContext:
    def __init__(self, response=None, enter_error=None):
        self.response = response
        self.enter_error = enter_error

    async def __aenter__(self):
        if self.enter_error is not None:
            raise self.enter_error
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, response=None, enter_error=None):
        self.response = response
        self.enter_error = enter_error
        self.post_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeRequestContext(self.response, self.enter_error)


def fake_session_factory(session):
    @asynccontextmanager
    async def factory(_config):
        yield session

    return factory


class WriteProbe(BilibiliAPIMixin, UtilsMixin, AffectionMixin, BangumiMixin):
    def __init__(self):
        self.config = {
            "SESSDATA": "session",
            "BILI_JCT": "csrf",
            "DEDE_USER_ID": "999",
            "BUVID3": "buvid",
        }


class HttpWriteSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_defaults_to_one_attempt_and_preserves_unknown(self):
        self.assertEqual(
            inspect.signature(UtilsMixin._http_post).parameters["retries"].default,
            0,
        )
        probe = UtilsMixin()
        probe.config = {}
        session = FakeSession(enter_error=aiohttp.ClientConnectionError("lost"))
        with patch("core.utils.plugin_http_session", fake_session_factory(session)):
            with self.assertRaises(WriteResultUnknownError):
                await probe._http_post("https://example.invalid/write", data={"x": 1})
        self.assertEqual(len(session.post_calls), 1)

    async def test_post_decode_and_shape_errors_are_unknown_without_retry(self):
        for response in (
            FakeResponse(json_error=json.JSONDecodeError("bad", "x", 0)),
            FakeResponse(payload=[{"code": 0}]),
        ):
            with self.subTest(response=response):
                probe = UtilsMixin()
                probe.config = {}
                session = FakeSession(response=response)
                with patch("core.utils.plugin_http_session", fake_session_factory(session)):
                    with self.assertRaises(WriteResultUnknownError):
                        await probe._http_post("https://example.invalid/write")
                self.assertEqual(len(session.post_calls), 1)

    async def test_all_bilibili_write_wrappers_propagate_unknown(self):
        calls = (
            ("reply", lambda bot: bot._send_reply(1, 2, 1, "hello")),
            ("comment", lambda bot: bot._send_comment(1, "hello")),
            ("private", lambda bot: bot._send_bili_private_payload(1, 1, {"content": "hello"})),
            ("like", lambda bot: bot._like_video(1)),
            ("coin", lambda bot: bot._coin_video(1)),
            ("favorite", lambda bot: bot._fav_video(1)),
            ("follow", lambda bot: bot._follow_user(1)),
            ("block", lambda bot: bot._block_user(1)),
            ("bangumi", lambda bot: bot._follow_bangumi(1)),
            ("dynamic", lambda bot: bot._post_dynamic_text("hello")),
        )
        for name, call in calls:
            with self.subTest(name=name):
                bot = WriteProbe()
                bot._http_post = AsyncMock(
                    side_effect=WriteResultUnknownError("ambiguous")
                )
                bot._http_get = AsyncMock(
                    return_value=({"code": 0, "data": {"list": [{"id": 10}]}}, None)
                )
                with self.assertRaises(WriteResultUnknownError):
                    await call(bot)
                self.assertEqual(bot._http_post.await_count, 1)

    async def test_definite_api_rejection_remains_a_normal_false(self):
        bot = WriteProbe()
        bot._http_post = AsyncMock(return_value=({"code": -101, "message": "no"}, None))
        self.assertFalse(await bot._send_comment(1, "hello"))
        self.assertEqual(bot._http_post.await_count, 1)

    async def test_comment_sync_to_dynamic_flag_controls_request_body(self):
        bot = WriteProbe()
        bot._http_post = AsyncMock(return_value=({"code": 0}, None))
        self.assertTrue(await bot._send_comment(1, "hello", sync_to_dynamic=True))
        self.assertEqual(bot._http_post.await_args.kwargs["data"]["sync_to_dynamic"], 1)
        bot._http_post.reset_mock()
        self.assertTrue(await bot._send_comment(1, "hello"))
        self.assertNotIn("sync_to_dynamic", bot._http_post.await_args.kwargs["data"])

    async def test_live_danmaku_propagates_unknown(self):
        bot = LiveDanmakuMixin()
        bot.config = {
            "LIVE_DANMAKU_ROOM_ID": "123",
            "SESSDATA": "session",
            "BILI_JCT": "csrf",
            "DEDE_USER_ID": "999",
        }
        bot._headers = lambda: {}
        bot._has_cookie = lambda: True
        bot._http_post = AsyncMock(side_effect=WriteResultUnknownError("lost"))
        bot._record_live_error = Mock()
        bot._live_send_backoff_until = 0.0
        with self.assertRaises(WriteResultUnknownError):
            await bot._send_live_danmaku_chunk("hello")
        self.assertEqual(bot._http_post.await_count, 1)
        self.assertIn("结果未知", bot._record_live_error.call_args.args[1])


class DynamicFallbackSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_dynamic_unknown_never_falls_back_to_text(self):
        bot = WriteProbe()
        bot._post_dynamic_text = AsyncMock(return_value=True)
        session = FakeSession(enter_error=asyncio.TimeoutError())
        with patch("core.bilibili.plugin_http_session", fake_session_factory(session)):
            with self.assertRaises(WriteResultUnknownError):
                await bot._post_dynamic_with_image("caption", {"url": "image"})
        self.assertEqual(len(session.post_calls), 1)
        bot._post_dynamic_text.assert_not_awaited()

    async def test_image_dynamic_definite_rejection_may_fall_back_once(self):
        bot = WriteProbe()
        bot._post_dynamic_text = AsyncMock(return_value=True)
        session = FakeSession(response=FakeResponse({"code": -412, "message": "risk"}))
        with patch("core.bilibili.plugin_http_session", fake_session_factory(session)):
            self.assertTrue(await bot._post_dynamic_with_image("caption", {"url": "image"}))
        self.assertEqual(len(session.post_calls), 1)
        bot._post_dynamic_text.assert_awaited_once_with("caption")

    async def test_weekly_and_daily_never_start_a_second_dynamic_after_an_outcome(self):
        for method_name in ("_deliver_weekly_summary", "_deliver_daily_summary"):
            for state in ("unknown", "failed"):
                with self.subTest(method=method_name, state=state):
                    bot = WeeklySummaryMixin()
                    bot.config = {
                        "WEEKLY_SUMMARY_MODE": "dynamic",
                        "DAILY_SUMMARY_MODE": "dynamic",
                    }
                    bot._upload_image_to_bilibili = AsyncMock(return_value={"url": "image"})
                    bot._queue_dynamic_post = AsyncMock(
                        return_value=ActionOutcome(False, "dynamic:1", state=state)
                    )
                    bot._post_dynamic_text = Mock()
                    bot._load_json = Mock(return_value=[])
                    bot._save_json = Mock()
                    result = await getattr(bot, method_name)("summary", image_path="image.png")
                    self.assertEqual(bot._queue_dynamic_post.await_count, 1)
                    bot._post_dynamic_text.assert_not_called()
                    if state == "unknown":
                        self.assertIn("B站动态结果未知", result)


class RuntimeAndToolSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_unknown_is_persistent_and_never_retried(self):
        runtime = EventRuntime()
        calls = 0

        async def send():
            nonlocal calls
            calls += 1
            raise WriteResultUnknownError("response lost")

        request = ActionRequest(key="comment:1", kind="comment")
        first = await runtime.execute(request, send)
        second = await runtime.execute(request, send)
        self.assertEqual(first.state, "unknown")
        self.assertTrue(second.duplicate)
        self.assertEqual(second.state, "unknown")
        self.assertEqual(calls, 1)

    async def test_layered_unknown_commits_budget_and_survives_runtime_replacement(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        layers = LayeredRuntime(
            {
                "BEHAVIOR_GLOBAL_MAX_PER_MINUTE": 0,
                "BEHAVIOR_GLOBAL_DAILY_LIMIT": 1,
                "AUTONOMOUS_REPLY_DAILY_LIMIT": 0,
                "AUTONOMOUS_PRIVATE_DAILY_LIMIT": 0,
            },
            Path(directory.name) / "runtime.sqlite3",
        )
        await layers.open()
        self.addAsyncCleanup(layers.close)
        calls = 0

        async def ambiguous():
            nonlocal calls
            calls += 1
            raise WriteResultUnknownError("lost")

        request = ActionRequest(key="unknown:budget", kind="comment_reply")
        first = await EventRuntime(observer=layers).execute(request, ambiguous)
        duplicate = await EventRuntime(observer=layers).execute(request, ambiguous)
        blocked_calls = 0

        async def should_not_run():
            nonlocal blocked_calls
            blocked_calls += 1
            return True

        denied = await EventRuntime(observer=layers).execute(
            ActionRequest(key="next:budget", kind="private_reply"), should_not_run
        )
        self.assertEqual(first.state, "unknown")
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.state, "unknown")
        self.assertTrue(denied.reason.startswith("budget_exhausted:"))
        self.assertEqual(calls, 1)
        self.assertEqual(blocked_calls, 0)

    async def test_tool_reports_unknown_and_does_not_call_api_again(self):
        plugin = types.SimpleNamespace(
            config={
                "DEDE_USER_ID": "999", "OWNER_MID": "42", "OWNER_NAME": "owner",
                "ENABLE_LLM_TOOLS": True,
            },
            event_runtime=EventRuntime(),
            layered_runtime=types.SimpleNamespace(
                authorize_tool_call=AsyncMock(
                    return_value=types.SimpleNamespace(allowed=True, reason="")
                )
            ),
            _like_video=AsyncMock(side_effect=WriteResultUnknownError("lost")),
        )
        tools = {tool.name: tool for tool in create_tools(plugin)}
        with patch("core.tools.random.uniform", return_value=0):
            first = await tools["bili_action"].call(None, action="like", target="123")
            second = await tools["bili_action"].call(None, action="like", target="123")
        for text in (first, second):
            self.assertIn("结果未知", text)
            self.assertIn("请勿自动重试", text)
        self.assertEqual(plugin._like_video.await_count, 1)


    async def test_block_tool_unknown_keeps_local_quarantine_and_no_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = types.SimpleNamespace(
                config={
                    "DEDE_USER_ID": "999", "OWNER_MID": "42", "OWNER_NAME": "owner",
                    "ENABLE_LLM_TOOLS": True,
                },
                event_runtime=EventRuntime(),
                layered_runtime=types.SimpleNamespace(
                    authorize_tool_call=AsyncMock(
                        return_value=types.SimpleNamespace(allowed=True, reason="")
                    )
                ),
                _is_owner=Mock(return_value=False),
                _block_user=AsyncMock(side_effect=WriteResultUnknownError("lost")),
                _load_json=Mock(return_value={}),
                _save_json=Mock(),
            )
            tools = {tool.name: tool for tool in create_tools(plugin)}
            with patch("core.tools.DATA_DIR", directory), patch(
                "core.tools.USER_PROFILE_FILE", str(Path(directory) / "profiles.json")
            ):
                first = await tools["bili_block_user"].call(None, uid="123")
                second = await tools["bili_block_user"].call(None, uid="123")
            for text in (first, second):
                self.assertIsInstance(text, str)
                self.assertIn("结果未知", text)
                self.assertIn("本地黑名单", text)
            self.assertEqual(plugin._block_user.await_count, 1)
            self.assertEqual(plugin._save_json.call_count, 2)

class PrivateBlockResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_remote_block_is_not_persisted_as_definite_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            def load(path, default=None):
                try:
                    return json.loads(Path(path).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return {} if default is None else default

            def save(path, value):
                Path(path).write_text(json.dumps(value), encoding="utf-8")

            bot = PrivateMessageMixin()
            bot.config = {
                "PRIVATE_MESSAGE_AUTO_BLOCK": True,
                "DEDE_USER_ID": "999",
                "OWNER_MID": "42",
            }
            bot.event_runtime = EventRuntime()
            bot._is_owner = Mock(return_value=False)
            bot._private_conversation_thread_id = Mock(return_value="private:123:test")
            bot._private_sender_protected = Mock(return_value=False)
            bot._block_user = AsyncMock(side_effect=WriteResultUnknownError("lost"))
            bot._log_security_event = Mock()
            bot._affection = {}
            bot._load_json = load
            bot._save_json = save
            message = {
                "sender_uid": "123",
                "username": "unsafe",
                "content": "blocked content",
                "msg_key": "message-1",
                "content_type": "text",
                "timestamp": 1,
            }
            decision = types.SimpleNamespace(should_block=True, reason="test block")
            with patch("core.private_messages.DATA_DIR", directory), patch(
                "core.private_messages.assess_private_message", return_value=decision
            ):
                handled = await bot._handle_private_message(message, None)
            self.assertTrue(handled)
            bot._block_user.assert_awaited_once_with(123)
            record = json.loads(
                (Path(directory) / "block_log.json").read_text(encoding="utf-8")
            )["123"]
            self.assertIsNone(record["api_blocked"])


if __name__ == "__main__":
    unittest.main()
