import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "core" / "runtime.py"
SPEC = importlib.util.spec_from_file_location("bilibot_event_runtime", MODULE_PATH)
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


def load_private_message_module(data_dir):
    package_name = "bilibot_private_runtime_test"
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package
    sys.modules[f"{package_name}.runtime"] = runtime

    config = types.ModuleType(f"{package_name}.config")
    constants = {
        "AFFECTION_FILE": str(Path(data_dir) / "affection.json"),
        "BILI_PRIVATE_MESSAGES_URL": "messages",
        "BILI_PRIVATE_SESSIONS_URL": "sessions",
        "DATA_DIR": str(data_dir),
        "LEVEL_NAMES": {"friend": "好友"},
        "PERMANENT_MEMORY_FILE": str(Path(data_dir) / "permanent.json"),
        "PRIVATE_MESSAGE_STATE_FILE": str(Path(data_dir) / "private_state.json"),
        "REPLY_LOG_FILE": str(Path(data_dir) / "reply_log.json"),
    }
    for key, value in constants.items():
        setattr(config, key, value)
    sys.modules[config.__name__] = config

    if "astrbot" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        astrbot_api = types.ModuleType("astrbot.api")
        logger = types.SimpleNamespace(
            debug=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        )
        astrbot_api.logger = logger
        astrbot.api = astrbot_api
        sys.modules["astrbot"] = astrbot
        sys.modules["astrbot.api"] = astrbot_api

    private_path = MODULE_PATH.with_name("private_messages.py")
    private_spec = importlib.util.spec_from_file_location(
        f"{package_name}.private_messages", private_path
    )
    private_module = importlib.util.module_from_spec(private_spec)
    sys.modules[private_spec.name] = private_module
    private_spec.loader.exec_module(private_module)
    return private_module, constants


class EventRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def make_event(self, event_id="1"):
        return runtime.InboundEvent(
            source="private",
            event_id=event_id,
            actor_id="42",
            actor_name="tester",
            content="hello",
            conversation_id="private:42",
        )

    async def test_claim_deduplicates_same_event(self):
        manager = runtime.EventRuntime()
        first = await manager.claim(self.make_event())
        second = await manager.claim(self.make_event())

        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(first.event_key, second.event_key)
        self.assertEqual(second.reason, "duplicate:processing")

    async def test_successful_action_runs_only_once(self):
        manager = runtime.EventRuntime()
        claim = await manager.claim(self.make_event())
        calls = 0

        async def send():
            nonlocal calls
            calls += 1
            return True

        request = runtime.ActionRequest(
            key="private_reply:1",
            kind="private_reply",
            event_key=claim.event_key,
            target_id="42",
        )
        first = await manager.execute(request, send)
        second = await manager.execute(request, send)

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertTrue(second.duplicate)
        self.assertEqual(calls, 1)
        snapshot = await manager.snapshot()
        self.assertEqual(snapshot["event_states"]["sent"], 1)
        self.assertEqual(snapshot["action_states"]["succeeded"], 1)

    async def test_concurrent_duplicate_does_not_send_twice(self):
        manager = runtime.EventRuntime()
        claim = await manager.claim(self.make_event())
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def slow_send():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return True

        request = runtime.ActionRequest(
            key="private_reply:1",
            kind="private_reply",
            event_key=claim.event_key,
        )
        first_task = asyncio.create_task(manager.execute(request, slow_send))
        await started.wait()
        duplicate = await manager.execute(request, slow_send)
        release.set()
        first = await first_task

        self.assertTrue(first.success)
        self.assertFalse(duplicate.success)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.reason, "already_running")
        self.assertEqual(calls, 1)

    async def test_failed_action_can_be_retried(self):
        manager = runtime.EventRuntime()
        claim = await manager.claim(self.make_event())
        results = iter((False, True))
        calls = 0

        async def flaky_send():
            nonlocal calls
            calls += 1
            return next(results)

        request = runtime.ActionRequest(
            key="private_reply:1",
            kind="private_reply",
            event_key=claim.event_key,
        )
        first = await manager.execute(request, flaky_send)
        second = await manager.execute(request, flaky_send)

        self.assertFalse(first.success)
        self.assertTrue(second.success)
        self.assertEqual(calls, 2)

    async def test_failed_event_can_be_reclaimed_only_for_explicit_retry(self):
        manager = runtime.EventRuntime()
        event = self.make_event()
        claim = await manager.claim(event)
        await manager.transition(claim.event_key, runtime.EventState.FAILED, "temporary")

        duplicate = await manager.claim(event)
        retry = await manager.claim(event, allow_retry_failed=True)
        running_duplicate = await manager.claim(event, allow_retry_failed=True)

        self.assertFalse(duplicate.accepted)
        self.assertTrue(retry.accepted)
        self.assertEqual(retry.reason, "retry")
        self.assertFalse(running_duplicate.accepted)

    async def test_ignored_event_is_visible_in_snapshot(self):
        manager = runtime.EventRuntime()
        claim = await manager.claim(self.make_event())
        changed = await manager.transition(
            claim.event_key, runtime.EventState.IGNORED, "scope_denied"
        )

        self.assertTrue(changed)
        snapshot = await manager.snapshot()
        self.assertEqual(snapshot["event_states"]["ignored"], 1)


class PrivateReplyCommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_send_does_not_commit_relationship_or_memory(self):
        with tempfile.TemporaryDirectory() as data_dir:
            private_module, _ = load_private_message_module(data_dir)

            class Bot(private_module.PrivateMessageMixin):
                def __init__(self):
                    self.config = {"ENABLE_AFFECTION": True}
                    self.event_runtime = runtime.EventRuntime()
                    self._affection = {"42": 10}
                    self.profile_updates = 0
                    self.memory_writes = 0
                    self.saved_paths = []

                def _is_owner(self, _mid):
                    return False

                def _peek_milestone(self, *_args):
                    return None

                def _commit_milestone(self, *_args):
                    pass

                async def _send_bili_private_message(self, _mid, _text):
                    return False

                def _save_json(self, path, _value):
                    self.saved_paths.append(path)

                def _load_json(self, _path, default=None):
                    return [] if default is None else default

                def _update_user_profile(self, *_args, **_kwargs):
                    self.profile_updates += 1

                async def _save_memory_record(self, *_args, **_kwargs):
                    self.memory_writes += 1

                async def _compress_thread_memory(self, _thread_id):
                    pass

                async def _compress_user_memory(self, _mid, _username):
                    pass

                def _get_level(self, _score, _mid):
                    return "friend"

            bot = Bot()
            sent = await bot._apply_private_reply_result(
                {
                    "sender_uid": "42",
                    "username": "tester",
                    "content": "hello",
                    "msg_key": "message-1",
                },
                {
                    "reply": "hi",
                    "score_delta": 2,
                    "impression": "friendly",
                    "user_facts": ["likes tests"],
                    "permanent_memory": "important",
                },
            )

            self.assertFalse(sent)
            self.assertEqual(bot._affection["42"], 10)
            self.assertEqual(bot.profile_updates, 0)
            self.assertEqual(bot.memory_writes, 0)
            self.assertEqual(bot.saved_paths, [])

    async def test_successful_send_commits_side_effects(self):
        with tempfile.TemporaryDirectory() as data_dir:
            private_module, constants = load_private_message_module(data_dir)

            class Bot(private_module.PrivateMessageMixin):
                def __init__(self):
                    self.config = {"ENABLE_AFFECTION": True}
                    self.event_runtime = runtime.EventRuntime()
                    self._affection = {"42": 10}
                    self.profile_updates = 0
                    self.memory_writes = 0
                    self.saved_paths = []

                def _is_owner(self, _mid):
                    return False

                def _peek_milestone(self, *_args):
                    return None

                def _commit_milestone(self, *_args):
                    pass

                async def _send_bili_private_message(self, _mid, _text):
                    return True

                def _save_json(self, path, _value):
                    self.saved_paths.append(path)

                def _load_json(self, _path, default=None):
                    return [] if default is None else default

                def _update_user_profile(self, *_args, **_kwargs):
                    self.profile_updates += 1

                async def _save_memory_record(self, *_args, **_kwargs):
                    self.memory_writes += 1

                async def _compress_thread_memory(self, _thread_id):
                    pass

                async def _compress_user_memory(self, _mid, _username):
                    pass

                def _get_level(self, _score, _mid):
                    return "friend"

            bot = Bot()
            sent = await bot._apply_private_reply_result(
                {
                    "sender_uid": "42",
                    "username": "tester",
                    "content": "hello",
                    "msg_key": "message-2",
                },
                {
                    "reply": "hi",
                    "score_delta": 2,
                    "impression": "friendly",
                    "user_facts": ["likes tests"],
                    "permanent_memory": "important",
                },
            )

            self.assertTrue(sent)
            self.assertEqual(bot._affection["42"], 12)
            self.assertEqual(bot.profile_updates, 1)
            self.assertEqual(bot.memory_writes, 1)
            self.assertIn(constants["AFFECTION_FILE"], bot.saved_paths)
            self.assertIn(constants["REPLY_LOG_FILE"], bot.saved_paths)


if __name__ == "__main__":
    unittest.main()
