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

from core.infra.bili.users import BiliUsers
from core.settings import Settings
from core.domain.affection import Affection
from core.domain.blocklist import BlockList
from core.domain.user_profiles import UserProfiles
from core.storage.json_file import JsonFile
from core.infra.bili.bangumi import BiliBangumi
from bili_parts import BiliParts, attach_bili_groups
from core.layered_runtime import LayeredRuntime
from private_parts import PrivateParts
from core.runtime import (
    ActionRequest,
    ActionOutcome,
    EventRuntime,
    WriteResultUnknownError,
)
from core.tools import create_tools
from base_parts import BaseParts
from core.infra.bili.http import BiliHttp
from summary_parts import SummaryParts


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


class WriteProbe(BiliParts):
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
            inspect.signature(BiliHttp._http_post).parameters["retries"].default,
            0,
        )
        probe = BaseParts()
        probe.config = {}
        session = FakeSession(enter_error=aiohttp.ClientConnectionError("lost"))
        with patch("core.infra.bili.http.plugin_http_session", fake_session_factory(session)):
            with self.assertRaises(WriteResultUnknownError):
                await probe.bili_http._http_post("https://example.invalid/write", data={"x": 1})
        self.assertEqual(len(session.post_calls), 1)

    async def test_post_decode_and_shape_errors_are_unknown_without_retry(self):
        for response in (
            FakeResponse(json_error=json.JSONDecodeError("bad", "x", 0)),
            FakeResponse(payload=[{"code": 0}]),
        ):
            with self.subTest(response=response):
                probe = BaseParts()
                probe.config = {}
                session = FakeSession(response=response)
                with patch("core.infra.bili.http.plugin_http_session", fake_session_factory(session)):
                    with self.assertRaises(WriteResultUnknownError):
                        await probe.bili_http._http_post("https://example.invalid/write")
                self.assertEqual(len(session.post_calls), 1)

    async def test_all_bilibili_write_wrappers_propagate_unknown(self):
        calls = (
            ("reply", lambda bot: bot.bili_comments._send_reply(1, 2, 1, "hello")),
            ("comment", lambda bot: bot.bili_comments._send_comment(1, "hello")),
            ("private", lambda bot: bot.bili_messages._send_bili_private_payload(1, 1, {"content": "hello"})),
            ("like", lambda bot: bot.bili_videos._like_video(1)),
            ("coin", lambda bot: bot.bili_videos._coin_video(1)),
            ("favorite", lambda bot: bot.bili_videos._fav_video(1)),
            ("follow", lambda bot: bot.bili_users._follow_user(1)),
            ("block", lambda bot: BiliUsers(bot)._block_user(1)),
            ("bangumi", lambda bot: BiliBangumi(bot)._follow_bangumi(1)),
            ("dynamic", lambda bot: bot.bili_dynamics._post_dynamic_text("hello")),
        )
        for name, call in calls:
            with self.subTest(name=name):
                bot = WriteProbe()
                bot.bili_http._http_post = AsyncMock(
                    side_effect=WriteResultUnknownError("ambiguous")
                )
                bot.bili_http._http_get = AsyncMock(
                    return_value=({"code": 0, "data": {"list": [{"id": 10}]}}, None)
                )
                with self.assertRaises(WriteResultUnknownError):
                    await call(bot)
                self.assertEqual(bot.bili_http._http_post.await_count, 1)

    async def test_definite_api_rejection_remains_a_normal_false(self):
        bot = WriteProbe()
        bot.bili_http._http_post = AsyncMock(return_value=({"code": -101, "message": "no"}, None))
        self.assertFalse(await bot.bili_comments._send_comment(1, "hello"))
        self.assertEqual(bot.bili_http._http_post.await_count, 1)

    async def test_comment_sync_to_dynamic_only_when_asked(self):
        """PR #50：开了才带 ``sync_to_dynamic=1``，关着时请求和原来一样。"""
        bot = WriteProbe()
        bot.bili_http._http_post = AsyncMock(return_value=({"code": 0}, None))
        await bot.bili_comments._send_comment(1, "hello")
        self.assertNotIn("sync_to_dynamic", bot.bili_http._http_post.await_args.kwargs["data"])
        await bot.bili_comments._send_comment(1, "hello", sync_to_dynamic=True)
        self.assertEqual(bot.bili_http._http_post.await_args.kwargs["data"]["sync_to_dynamic"], 1)

    async def test_comment_pictures_and_mentions_are_serialized(self):
        """评论附图：pictures 必须是 JSON 字符串；@ 只能拼进正文前缀。"""
        bot = WriteProbe()
        bot.bili_http._http_post = AsyncMock(
            return_value=({"code": 0, "data": {"rpid": 42}}, None)
        )
        sink = {}
        posted = await bot.bili_comments._send_comment(
            1,
            "好看",
            at_names=["小明"],
            result_sink=sink,
            pictures=[{
                "img_src": "https://i0.hdslb.com/a.jpg",
                "img_width": 10,
                "img_height": 20,
                "img_size": 30,
            }],
        )
        data = bot.bili_http._http_post.await_args.kwargs["data"]
        self.assertEqual(json.loads(data["pictures"]), [{
            "img_src": "https://i0.hdslb.com/a.jpg",
            "img_width": 10,
            "img_height": 20,
            "img_size": 30,
        }])
        self.assertEqual(data["message"], "@小明 好看")
        self.assertEqual(sink, {"send_status": "published", "rpid": "42"})
        self.assertEqual(posted.rpid, 42)


class DynamicFallbackSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_dynamic_unknown_never_falls_back_to_text(self):
        bot = WriteProbe()
        bot.bili_http._http_post = AsyncMock(return_value=({"code": 0}, None))
        session = FakeSession(enter_error=asyncio.TimeoutError())
        with patch("core.infra.bili.dynamics.plugin_http_session", fake_session_factory(session)):
            with self.assertRaises(WriteResultUnknownError):
                await bot.bili_dynamics._post_dynamic_with_image("caption", {"url": "image"})
        self.assertEqual(len(session.post_calls), 1)
        bot.bili_http._http_post.assert_not_awaited()

    async def test_image_dynamic_definite_rejection_may_fall_back_once(self):
        bot = WriteProbe()
        bot.bili_http._http_post = AsyncMock(return_value=({"code": 0}, None))
        session = FakeSession(response=FakeResponse({"code": -412, "message": "risk"}))
        with patch("core.infra.bili.dynamics.plugin_http_session", fake_session_factory(session)):
            self.assertTrue(await bot.bili_dynamics._post_dynamic_with_image("caption", {"url": "image"}))
        self.assertEqual(len(session.post_calls), 1)
        bot.bili_http._http_post.assert_awaited_once()
        self.assertEqual(bot.bili_http._http_post.call_args.kwargs["data"]["content"], "caption")

    async def test_weekly_and_daily_never_start_a_second_dynamic_after_an_outcome(self):
        for method_name in ("_deliver_weekly_summary", "_deliver_daily_summary"):
            for state in ("unknown", "failed"):
                with self.subTest(method=method_name, state=state):
                    bot = SummaryParts()
                    bot.config = {
                        "WEEKLY_SUMMARY_MODE": "dynamic",
                        "DAILY_SUMMARY_MODE": "dynamic",
                    }
                    bot.bili_dynamics._upload_image_to_bilibili = AsyncMock(return_value={"url": "image"})
                    bot.dynamic_post = types.SimpleNamespace(queue_post=AsyncMock(
                        return_value=ActionOutcome(False, "dynamic:1", state=state)
                    ))
                    bot.bili_dynamics._post_dynamic_text = Mock()
                    bot.json_store._load_json = Mock(return_value=[])
                    bot.json_store._save_json = Mock()
                    result = await getattr(bot.summaries, method_name)("summary", image_path="image.png")
                    self.assertEqual(bot.dynamic_post.queue_post.await_count, 1)
                    bot.bili_dynamics._post_dynamic_text.assert_not_called()
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
        plugin.settings = Settings(plugin.config)
        attach_bili_groups(plugin)
        tools = {tool.name: tool for tool in create_tools(plugin)}
        with patch("core.tools.actions.random.uniform", return_value=0):
            first = await tools["bili_action"].call(None, action="like", target="123")
            second = await tools["bili_action"].call(None, action="like", target="123")
        for text in (first, second):
            self.assertIn("结果未知", text)
            self.assertIn("请勿自动重试", text)
        self.assertEqual(plugin.bili_videos._like_video.await_count, 1)


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
                roles=types.SimpleNamespace(is_owner=Mock(return_value=False)),
                bili_users=types.SimpleNamespace(_block_user=AsyncMock(side_effect=WriteResultUnknownError("lost"))),
                _load_json=Mock(return_value={}),
                _save_json=Mock(),
            )
            plugin.settings = Settings(plugin.config)
            plugin.blocklist = BlockList(JsonFile(Path(directory) / "block_log.json"), JsonFile(Path(directory) / "block_count.json"))
            plugin.user_profiles = UserProfiles(JsonFile(Path(directory) / "profiles.json"))
            tools = {tool.name: tool for tool in create_tools(plugin)}
            first = await tools["bili_block_user"].call(None, uid="123")
            second = await tools["bili_block_user"].call(None, uid="123")
            for text in (first, second):
                self.assertIsInstance(text, str)
                self.assertIn("结果未知", text)
                self.assertIn("本地黑名单", text)
            self.assertEqual(plugin.bili_users._block_user.await_count, 1)
            self.assertTrue(plugin.blocklist.contains("123"))

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

            bot = PrivateParts()
            bot.config = {
                "PRIVATE_MESSAGE_AUTO_BLOCK": True,
                "DEDE_USER_ID": "999",
                "OWNER_MID": "42",
            }
            bot.event_runtime = EventRuntime()
            bot.roles = types.SimpleNamespace(is_owner=Mock(return_value=False))
            bot.private_reply._private_conversation_thread_id = Mock(return_value="private:123:test")
            bot.private_safety._private_sender_protected = Mock(return_value=False)
            bot.bili_users = types.SimpleNamespace(_block_user=AsyncMock(side_effect=WriteResultUnknownError("lost")))
            bot.security_log = types.SimpleNamespace(record=Mock())
            bot.affection = Affection(JsonFile(Path(directory) / "affection.json"), lambda _uid: False)
            bot.blocklist = BlockList(JsonFile(Path(directory) / "block_log.json"), JsonFile(Path(directory) / "block_count.json"))
            bot.json_store._load_json = load
            bot.json_store._save_json = save
            message = {
                "sender_uid": "123",
                "username": "unsafe",
                "content": "blocked content",
                "msg_key": "message-1",
                "content_type": "text",
                "timestamp": 1,
            }
            decision = types.SimpleNamespace(should_block=True, reason="test block")
            with patch("core.features.private_reply.assess_private_message", return_value=decision):
                handled = await bot.private_reply._handle_private_message(message, None)
            self.assertTrue(handled)
            bot.bili_users._block_user.assert_awaited_once_with(123)
            record = json.loads(
                (Path(directory) / "block_log.json").read_text(encoding="utf-8")
            )["123"]
            self.assertIsNone(record["api_blocked"])


if __name__ == "__main__":
    unittest.main()
