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


def load_reply_module(data_dir):
    package_name = "bilibot_comment_runtime_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE_PATH.parent)]
    sys.modules[package_name] = package
    sys.modules[f"{package_name}.runtime"] = runtime

    config = types.ModuleType(f"{package_name}.config")
    constants = {
        "AFFECTION_FILE": str(Path(data_dir) / "affection.json"),
        "DATA_DIR": str(data_dir),
        "LEVEL_NAMES": {"friend": "好友"},
        "REPLIED_AT_FILE": str(Path(data_dir) / "replied_at.json"),
        "REPLIED_FILE": str(Path(data_dir) / "replied.json"),
        "REPLIED_CONTENT_KEYS_FILE": str(Path(data_dir) / "content_keys.json"),
        "REPLY_LOG_FILE": str(Path(data_dir) / "reply_log.json"),
        "BILI_AT_NOTIFY_URL": "at",
        "BILI_NOTIFY_URL": "notify",
        "VIDEO_MEMORY_FILE": str(Path(data_dir) / "video_memory.json"),
    }
    for key, value in constants.items():
        setattr(config, key, value)
    sys.modules[config.__name__] = config

    if "astrbot" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        astrbot_api = types.ModuleType("astrbot.api")
        astrbot_api.logger = types.SimpleNamespace(
            debug=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        )
        astrbot.api = astrbot_api
        sys.modules["astrbot"] = astrbot
        sys.modules["astrbot.api"] = astrbot_api

    reply_path = MODULE_PATH.with_name("reply.py")
    reply_spec = importlib.util.spec_from_file_location(
        f"{package_name}.reply", reply_path
    )
    reply_module = importlib.util.module_from_spec(reply_spec)
    sys.modules[reply_spec.name] = reply_module
    reply_spec.loader.exec_module(reply_module)
    return reply_module, constants


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

    async def test_action_queue_serializes_different_side_effects(self):
        manager = runtime.EventRuntime()
        active = 0
        peak = 0

        async def send():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return True

        tasks = [
            asyncio.create_task(
                manager.execute(
                    runtime.ActionRequest(key=f"queued:{index}", kind="comment_reply"),
                    send,
                )
            )
            for index in range(4)
        ]
        outcomes = await asyncio.gather(*tasks)

        self.assertTrue(all(outcome.success for outcome in outcomes))
        self.assertEqual(peak, 1)

    async def test_action_queue_prefers_urgent_waiting_action(self):
        manager = runtime.EventRuntime()
        started = asyncio.Event()
        release = asyncio.Event()
        order = []

        async def blocker():
            order.append("blocker")
            started.set()
            await release.wait()
            return True

        async def record(label):
            order.append(label)
            return True

        blocker_task = asyncio.create_task(
            manager.execute(
                runtime.ActionRequest(key="priority:blocker", kind="like"), blocker
            )
        )
        await started.wait()
        background = asyncio.create_task(
            manager.execute(
                runtime.ActionRequest(
                    key="priority:background",
                    kind="like",
                    priority=runtime.EventPriority.BACKGROUND,
                ),
                lambda: record("background"),
            )
        )
        urgent = asyncio.create_task(
            manager.execute(
                runtime.ActionRequest(
                    key="priority:urgent",
                    kind="private_reply",
                    priority=runtime.EventPriority.ADMIN,
                ),
                lambda: record("urgent"),
            )
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(blocker_task, background, urgent)

        self.assertEqual(order, ["blocker", "urgent", "background"])

    async def test_timed_out_action_becomes_unknown_and_is_not_retried(self):
        manager = runtime.EventRuntime(action_timeout=0.01)
        calls = 0

        async def ambiguous_send():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.1)
            return True

        request = runtime.ActionRequest(key="timeout:1", kind="private_reply")
        first = await manager.execute(request, ambiguous_send)
        second = await manager.execute(request, ambiguous_send)

        self.assertFalse(first.success)
        self.assertEqual(first.state, "unknown")
        self.assertTrue(second.duplicate)
        self.assertEqual(second.state, "unknown")
        self.assertEqual(calls, 1)

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

    def test_priority_is_derived_from_shared_event_flags(self):
        admin = self.make_event("admin")
        admin = runtime.InboundEvent(
            source=admin.source,
            event_id=admin.event_id,
            actor_id=admin.actor_id,
            metadata={"is_admin": True, "direct_mention": True},
        )
        direct = runtime.InboundEvent(
            source="comment",
            event_id="direct",
            actor_id="43",
            metadata={"direct_mention": True},
        )
        active = runtime.InboundEvent(
            source="private",
            event_id="active",
            actor_id="44",
            metadata={"conversation_active": True},
        )
        normal = runtime.InboundEvent(
            source="comment",
            event_id="normal",
            actor_id="45",
        )

        self.assertEqual(admin.priority, runtime.EventPriority.ADMIN)
        self.assertEqual(direct.priority, runtime.EventPriority.DIRECT_MENTION)
        self.assertEqual(active.priority, runtime.EventPriority.ACTIVE_CONVERSATION)
        self.assertEqual(normal.priority, runtime.EventPriority.NORMAL)

    def test_rank_events_prefers_priority_then_requested_time_order(self):
        manager = runtime.EventRuntime()
        events = [
            runtime.InboundEvent(
                source="comment",
                event_id="normal-new",
                actor_id="1",
                occurred_at=30,
            ),
            runtime.InboundEvent(
                source="comment",
                event_id="direct-old",
                actor_id="2",
                occurred_at=10,
                metadata={"direct_mention": True},
            ),
            runtime.InboundEvent(
                source="comment",
                event_id="direct-new",
                actor_id="3",
                occurred_at=20,
                metadata={"direct_mention": True},
            ),
        ]

        oldest_first = manager.rank_events(events)
        newest_first = manager.rank_events(events, newest_first=True)

        self.assertEqual(
            [event.event_id for event in oldest_first],
            ["direct-old", "direct-new", "normal-new"],
        )
        self.assertEqual(
            [event.event_id for event in newest_first],
            ["direct-new", "direct-old", "normal-new"],
        )

    async def test_snapshot_exposes_priority_without_message_content(self):
        manager = runtime.EventRuntime()
        secret = "private message body must not be exposed"
        event = runtime.InboundEvent(
            source="private",
            event_id="safe-snapshot",
            actor_id="42",
            content=secret,
            metadata={"conversation_active": True},
        )
        await manager.claim(event)

        snapshot = await manager.snapshot()
        self.assertEqual(snapshot["event_priorities"]["active_conversation"], 1)
        self.assertEqual(snapshot["recent_events"][0]["source"], "private")
        self.assertNotIn(secret, str(snapshot["recent_events"]))


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
                    self.relationship_records = 0

                def _is_owner(self, _mid):
                    return False

                def _peek_milestone(self, *_args):
                    return None

                def _commit_milestone(self, *_args):
                    pass

                def _record_relationship_interaction(self, *_args, **_kwargs):
                    self.relationship_records += 1
                    return {}

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

                async def _compress_user_memory(self, _mid, _username, _scope="bili_comment"):
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
                    "decision": "reply",
                    "_protocol_validated": True,
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
            self.assertEqual(bot.relationship_records, 0)

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
                    self.relationship_records = 0

                def _is_owner(self, _mid):
                    return False

                def _peek_milestone(self, *_args):
                    return None

                def _commit_milestone(self, *_args):
                    pass

                def _record_relationship_interaction(self, *_args, **_kwargs):
                    self.relationship_records += 1
                    return {}

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

                async def _compress_user_memory(self, _mid, _username, _scope="bili_comment"):
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
                    "decision": "reply",
                    "_protocol_validated": True,
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
            self.assertEqual(bot.relationship_records, 1)
            self.assertIn(constants["AFFECTION_FILE"], bot.saved_paths)
            self.assertIn(constants["REPLY_LOG_FILE"], bot.saved_paths)


class CommentReplyCommitTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, reply_module, send_ok):
        class Bot(reply_module.ReplyMixin):
            def __init__(self):
                self.config = {
                    "ENABLE_AFFECTION": True,
                    "ENABLE_AUTO_BLOCK": False,
                }
                self.event_runtime = runtime.EventRuntime()
                self._affection = {"42": 10}
                self.profile_updates = 0
                self.memory_writes = 0
                self.saved_paths = []
                self.relationship_records = 0

            async def _oid_to_bvid(self, _oid):
                return ""

            def _is_owner(self, _mid):
                return False

            def _is_block_whitelisted(self, _mid):
                return False

            def _peek_milestone(self, *_args):
                return None

            def _commit_milestone(self, *_args):
                pass

            def _record_relationship_interaction(self, *_args, **_kwargs):
                self.relationship_records += 1
                return {}

            async def _send_reply(self, *_args):
                return send_ok

            async def _block_user(self, _mid):
                return True

            def _save_json(self, path, _value):
                self.saved_paths.append(path)

            def _load_json(self, _path, default=None):
                return {} if default is None else default

            def _update_user_profile(self, *_args, **_kwargs):
                self.profile_updates += 1

            async def _save_memory_record(self, *_args, **_kwargs):
                self.memory_writes += 1

            async def _compress_thread_memory(self, _thread_id):
                pass

            async def _compress_oid_memory(self, _oid):
                pass

            async def _compress_user_memory(self, _mid, _username, _scope):
                pass

            def _log_security_event(self, *_args):
                pass

            def _get_level(self, _score, _mid):
                return "friend"

        return Bot()

    async def test_failed_comment_send_does_not_commit_side_effects(self):
        with tempfile.TemporaryDirectory() as data_dir:
            reply_module, _ = load_reply_module(data_dir)
            bot = self.make_bot(reply_module, False)
            sent = await bot._apply_reply_result(
                mid="42",
                username="tester",
                content="hello",
                oid=1,
                rpid="comment-failed",
                comment_type=1,
                thread_id="thread-1",
                result={
                    "decision": "reply",
                    "_protocol_validated": True,
                    "reply": "hi",
                    "score_delta": 2,
                    "impression": "friendly",
                    "user_facts": ["likes tests"],
                    "permanent_memory": "must not become self memory",
                },
            )
            self.assertFalse(sent)
            self.assertEqual(bot._affection["42"], 10)
            self.assertEqual(bot.profile_updates, 0)
            self.assertEqual(bot.memory_writes, 0)
            self.assertEqual(bot.saved_paths, [])
            self.assertEqual(bot.relationship_records, 0)

    async def test_successful_comment_send_commits_scoped_side_effects(self):
        with tempfile.TemporaryDirectory() as data_dir:
            reply_module, constants = load_reply_module(data_dir)
            bot = self.make_bot(reply_module, True)
            sent = await bot._apply_reply_result(
                mid="42",
                username="tester",
                content="hello",
                oid=1,
                rpid="comment-success",
                comment_type=1,
                thread_id="thread-1",
                result={
                    "decision": "reply",
                    "_protocol_validated": True,
                    "reply": "hi",
                    "score_delta": 2,
                    "impression": "friendly",
                    "user_facts": ["likes tests"],
                    "permanent_memory": "must not become self memory",
                },
            )
            self.assertTrue(sent)
            self.assertEqual(bot._affection["42"], 12)
            self.assertEqual(bot.profile_updates, 1)
            self.assertEqual(bot.memory_writes, 1)
            self.assertEqual(bot.relationship_records, 1)
            self.assertIn(constants["AFFECTION_FILE"], bot.saved_paths)
            self.assertIn(constants["REPLY_LOG_FILE"], bot.saved_paths)


if __name__ == "__main__":
    unittest.main()
