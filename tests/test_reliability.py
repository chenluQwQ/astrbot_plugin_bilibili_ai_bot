import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _load_core_module(name, filename, config_values):
    package_name = "reliability_core"
    package = sys.modules.setdefault(package_name, types.ModuleType(package_name))
    package.__path__ = [str(ROOT / "core")]

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.logger = _Logger()
    astrbot.api = api

    config_name = f"{package_name}.config"
    config = types.ModuleType(config_name)
    for key, value in config_values.items():
        setattr(config, key, value)
    sys.modules[config_name] = config

    module_name = f"{package_name}.{name}"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "core" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class JsonPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_core_module(
            "utils",
            "utils.py",
            {
                "DATA_DIR": "",
                "TEMP_IMAGE_DIR": "",
                "TEMP_VIDEO_DIR": "",
                "USER_AGENT": "test",
            },
        )

    def test_concurrent_writers_use_unique_temporary_files(self):
        helper = self.module.UtilsMixin()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"

            def write(index):
                helper._save_json(target, {"writer": index, "payload": "x" * 1000})

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(write, range(32)))

            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn(saved["writer"], range(32))
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    def test_serialization_failure_preserves_original_file(self):
        helper = self.module.UtilsMixin()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            target.write_text('{"old": true}', encoding="utf-8")

            with self.assertRaises(TypeError):
                helper._save_json(target, {"bad": object()})

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"old": True})
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])


class PrivateMessageReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = str(Path(self.temp_dir.name) / "private_state.json")
        constants = {
            "AFFECTION_FILE": str(Path(self.temp_dir.name) / "affection.json"),
            "BILI_PRIVATE_MESSAGES_URL": "messages",
            "BILI_PRIVATE_SESSIONS_URL": "sessions",
            "DATA_DIR": self.temp_dir.name,
            "LEVEL_NAMES": {},
            "PERMANENT_MEMORY_FILE": str(Path(self.temp_dir.name) / "permanent.json"),
            "PRIVATE_MESSAGE_STATE_FILE": self.state_file,
            "REPLY_LOG_FILE": str(Path(self.temp_dir.name) / "reply.json"),
        }
        self.module = _load_core_module("private_messages", "private_messages.py", constants)

        module = self.module

        class Bot(module.PrivateMessageMixin):
            def __init__(self):
                self.config = {
                    "DEDE_USER_ID": "999",
                    "PRIVATE_MESSAGE_MAX_PER_POLL": 3,
                    "PRIVATE_MESSAGE_MAX_MESSAGE_AGE": 3600,
                }

            @staticmethod
            def _load_json(path, default=None):
                try:
                    return json.loads(Path(path).read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    return default

            @staticmethod
            def _save_json(path, data):
                Path(path).write_text(
                    json.dumps(data, ensure_ascii=False), encoding="utf-8"
                )

        self.Bot = Bot

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _message():
        return {
            "msg_key": "message-1",
            "msg_seqno": 12,
            "talker_id": 123,
            "session_type": 1,
            "sender_uid": "123",
            "username": "tester",
            "content": "hello",
            "content_type": "text",
            "timestamp": 100,
            "retry_count": 0,
            "next_retry_at": 0,
        }

    def _write_pending_state(self, bot, message):
        state = bot._default_private_message_state("999")
        state["initialized"] = True
        state["pending_messages"] = [message]
        bot._save_json(self.state_file, state)

    def test_success_acknowledges_pending_message(self):
        bot = self.Bot()
        message = self._message()
        self._write_pending_state(bot, message)

        outcome, _ = bot._finish_private_message(message)
        state = bot._load_json(self.state_file, {})

        self.assertEqual(outcome, "acknowledged")
        self.assertEqual(state["pending_messages"], [])
        self.assertEqual(state["processed_keys"], ["message-1"])

    def test_failed_message_retries_then_enters_quarantine(self):
        bot = self.Bot()
        message = self._message()
        self._write_pending_state(bot, message)

        self.assertEqual(bot._finish_private_message(message, RuntimeError("one")), ("retry", 30))
        state = bot._load_json(self.state_file, {})
        retry_message = state["pending_messages"][0]
        self.assertEqual(retry_message["retry_count"], 1)
        self.assertEqual(state["processed_keys"], [])

        self.assertEqual(bot._finish_private_message(retry_message, RuntimeError("two")), ("retry", 60))
        state = bot._load_json(self.state_file, {})
        retry_message = state["pending_messages"][0]
        self.assertEqual(retry_message["retry_count"], 2)

        self.assertEqual(
            bot._finish_private_message(retry_message, RuntimeError("three")),
            ("quarantined", 3),
        )
        state = bot._load_json(self.state_file, {})
        self.assertEqual(state["pending_messages"], [])
        self.assertEqual(state["processed_keys"], ["message-1"])
        self.assertEqual(state["failed_messages"][0]["last_error"], "three")

    def test_persisted_pending_message_is_replayed_without_network(self):
        bot = self.Bot()
        message = self._message()
        self._write_pending_state(bot, message)

        async def fail_if_called():
            raise AssertionError("pending replay should not call Bilibili")

        bot._get_private_sessions = fail_if_called
        replayed = asyncio.run(bot._poll_private_inbox())

        self.assertEqual([item["msg_key"] for item in replayed], ["message-1"])

    def test_poll_persists_message_before_advancing_remote_cursor(self):
        bot = self.Bot()
        state = bot._default_private_message_state("999")
        state["initialized"] = True
        state["sessions"] = {"1:123": 10}
        bot._save_json(self.state_file, state)

        async def sessions():
            return [{
                "talker_id": 123,
                "session_type": 1,
                "max_seqno": 12,
                "account_info": {"name": "tester"},
            }]

        async def messages(_talker_id, _session_type, _begin_seqno):
            return {
                "max_seqno": 12,
                "messages": [{
                    "msg_key": "message-1",
                    "msg_seqno": 12,
                    "sender_uid": 123,
                    "msg_type": 1,
                    "timestamp": int(time.time()),
                    "content": '{"content": "hello"}',
                }],
            }

        bot._get_private_sessions = sessions
        bot._fetch_private_session_messages = messages
        fetched = asyncio.run(bot._poll_private_inbox())
        saved = bot._load_json(self.state_file, {})

        self.assertEqual([item["msg_key"] for item in fetched], ["message-1"])
        self.assertEqual(saved["sessions"]["1:123"], 12)
        self.assertEqual(saved["processed_keys"], [])
        self.assertEqual(
            [item["msg_key"] for item in saved["pending_messages"]],
            ["message-1"],
        )

    def test_poll_loop_only_acknowledges_completed_handler(self):
        bot = self.Bot()
        bot.config.update({
            "ENABLE_PRIVATE_MESSAGES": True,
            "PRIVATE_MESSAGE_POLL_INTERVAL": 60,
        })
        message = self._message()
        self._write_pending_state(bot, message)
        bot._private_message_next_poll_at = 0
        bot._private_message_backoff_seconds = 0

        async def inbox():
            return [message]

        async def fail_handler(_message, _trusted_domains):
            raise RuntimeError("temporary")

        bot._poll_private_inbox = inbox
        bot._handle_private_message = fail_handler
        asyncio.run(bot._poll_private_messages())
        state = bot._load_json(self.state_file, {})
        self.assertEqual(state["processed_keys"], [])
        self.assertEqual(state["pending_messages"][0]["retry_count"], 1)

        retry_message = state["pending_messages"][0]
        bot._private_message_next_poll_at = 0

        async def retry_inbox():
            return [retry_message]

        async def complete_handler(_message, _trusted_domains):
            return True

        bot._poll_private_inbox = retry_inbox
        bot._handle_private_message = complete_handler
        asyncio.run(bot._poll_private_messages())
        state = bot._load_json(self.state_file, {})
        self.assertEqual(state["pending_messages"], [])
        self.assertEqual(state["processed_keys"], ["message-1"])

    def test_poll_loop_uses_idle_and_active_intervals(self):
        bot = self.Bot()
        bot.config.update({
            "ENABLE_PRIVATE_MESSAGES": True,
            "PRIVATE_MESSAGE_POLL_INTERVAL": 60,
            "PRIVATE_MESSAGE_IDLE_POLL_INTERVAL": 180,
            "PRIVATE_MESSAGE_ACTIVE_WINDOW": 600,
        })
        bot._private_message_next_poll_at = 0
        bot._private_message_backoff_seconds = 0

        async def empty_inbox():
            return []

        bot._poll_private_inbox = empty_inbox
        with mock.patch.object(self.module.random, "uniform", return_value=0):
            before = time.monotonic()
            asyncio.run(bot._poll_private_messages())
        idle_delay = bot._private_message_next_poll_at - before
        self.assertGreaterEqual(idle_delay, 179)
        self.assertLessEqual(idle_delay, 181)

        bot._private_message_next_poll_at = 0
        bot._private_message_last_activity_at = time.monotonic()
        with mock.patch.object(self.module.random, "uniform", return_value=0):
            before = time.monotonic()
            asyncio.run(bot._poll_private_messages())
        active_delay = bot._private_message_next_poll_at - before
        self.assertGreaterEqual(active_delay, 59)
        self.assertLessEqual(active_delay, 61)

    def test_poll_loop_prioritizes_owner_before_older_normal_message(self):
        bot = self.Bot()
        bot.config.update({
            "ENABLE_PRIVATE_MESSAGES": True,
            "PRIVATE_MESSAGE_POLL_INTERVAL": 60,
            "OWNER_MID": "42",
        })
        normal = self._message()
        owner = {
            **self._message(),
            "msg_key": "message-owner",
            "msg_seqno": 13,
            "sender_uid": "42",
            "talker_id": 42,
            "username": "owner",
            "timestamp": 101,
        }
        state = bot._default_private_message_state("999")
        state["initialized"] = True
        state["pending_messages"] = [normal, owner]
        bot._save_json(self.state_file, state)
        bot._private_message_next_poll_at = 0
        bot._private_message_backoff_seconds = 0
        handled = []

        async def inbox():
            return [normal, owner]

        async def handler(message, _trusted_domains):
            handled.append(message["sender_uid"])
            return True

        bot._poll_private_inbox = inbox
        bot._handle_private_message = handler
        asyncio.run(bot._poll_private_messages())

        self.assertEqual(handled, ["42", "123"])


if __name__ == "__main__":
    unittest.main()
