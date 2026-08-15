import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.adapter.events import ActionRequest as StoredActionRequest
from core.layered_runtime import LayeredRuntime
from core.runtime import ActionRequest, EventRuntime, EventState, InboundEvent


class LayeredRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.layers = LayeredRuntime(
            {"DEDE_USER_ID": "bot-1", "OWNER_MID": "42"},
            Path(self.temp_dir.name) / "bilibot.sqlite3",
        )
        await self.layers.open()
        self.runtime = EventRuntime(observer=self.layers)

    async def asyncTearDown(self):
        await self.layers.close()
        self.temp_dir.cleanup()

    @staticmethod
    def event(event_id="evt-1"):
        return InboundEvent(
            source="private",
            event_id=event_id,
            actor_id="42",
            actor_name="tester",
            content="hello",
            conversation_id="dm:42",
            metadata={"conversation_active": True},
        )

    async def test_claim_is_namespaced_persisted_and_deduplicated(self):
        event = self.event()
        claim = await self.runtime.claim(event)
        self.assertTrue(claim.accepted)

        row = await self.layers.db.fetch_one(
            "SELECT state,actor_id,priority FROM events WHERE source_event_id=?",
            (event.event_id,),
        )
        self.assertEqual(row["state"], "claimed")
        self.assertEqual(row["actor_id"], "bili:42")
        self.assertEqual(row["priority"], 20)

        await self.runtime.transition(claim.event_key, EventState.IGNORED, "test")
        duplicate_after_restart = await EventRuntime(observer=self.layers).claim(event)
        self.assertFalse(duplicate_after_restart.accepted)
        self.assertEqual(duplicate_after_restart.reason, "duplicate:ignored")

    async def test_pre_namespaced_actor_is_not_prefixed_twice(self):
        event = InboundEvent(
            source="comment",
            event_id="evt-namespaced",
            actor_id="bili:99",
            actor_name="namespaced",
        )
        claim = await self.runtime.claim(event)
        self.assertTrue(claim.accepted)
        row = await self.layers.db.fetch_one(
            "SELECT actor_id FROM events WHERE source_event_id=?", (event.event_id,)
        )
        self.assertEqual(row["actor_id"], "bili:99")

    async def test_action_is_idempotent_across_runtime_instances(self):
        claim = await self.runtime.claim(self.event("evt-action"))
        calls = 0

        async def send():
            nonlocal calls
            calls += 1
            return True

        request = ActionRequest(
            key="private_reply:evt-action",
            kind="private_reply",
            event_key=claim.event_key,
            target_id="42",
        )
        first = await self.runtime.execute(request, send)
        second = await EventRuntime(observer=self.layers).execute(request, send)

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.reason, "already_succeeded")
        self.assertEqual(calls, 1)
        row = await self.layers.db.fetch_one(
            "SELECT state FROM actions WHERE key=?", (request.key,)
        )
        self.assertEqual(row["state"], "succeeded")

    async def test_profile_persona_and_memory_store_are_live(self):
        await self.runtime.claim(self.event("evt-profile"))
        # 仅领取事件不应创建“已互动”画像；显式画像更新仍由存储层支持。
        self.assertIsNone(await self.layers.profiles.get("bili:42"))
        await self.layers._touch_profile("bili:42", "tester")
        profile = await self.layers.profiles.get("bili:42")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.display_name, "tester")
        self.assertEqual(profile.interact_count, 1)

        memory_id = await self.layers.memories.add(
            "bili_dm", "a small memory", actor_id="bili:42"
        )
        await self.layers.memories.promote(memory_id)
        memory = await self.layers.db.fetch_one(
            "SELECT level,promoted_at FROM memories WHERE id=?", (memory_id,)
        )
        self.assertEqual(memory["level"], "long_term")
        self.assertIsNotNone(memory["promoted_at"])

        snapshot = await self.layers.snapshot()
        self.assertTrue(snapshot["open"])
        self.assertGreaterEqual(snapshot["tables"]["events"], 1)
        self.assertIn("energy", snapshot["persona"])

    async def test_legacy_memory_roundtrip_separates_vector_and_metadata(self):
        record = {
            "rpid": "reply-100",
            "text": "用户说喜欢音游，Bot记住了。",
            "time": "2026-08-16 12:30",
            "created_at": 1786854600.0,
            "source": "bilibili_private",
            "scope": "bili_dm",
            "memory_type": "chat",
            "level": "today",
            "user_id": "42",
            "username": "tester",
            "actor_id": "bili:42",
            "thread_id": "private:42:1",
            "importance": 7,
            "embedding": [0.25, -0.5, 1.0],
            "custom": {"safe": True},
        }
        await self.layers.memories.upsert_legacy(record)
        loaded = await self.layers.memories.load_legacy()

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["rpid"], "reply-100")
        self.assertEqual(loaded[0]["scope"], "bili_dm")
        self.assertEqual(loaded[0]["custom"], {"safe": True})
        self.assertEqual(len(loaded[0]["embedding"]), 3)
        self.assertAlmostEqual(loaded[0]["embedding"][1], -0.5)
        row = await self.layers.db.fetch_one(
            "SELECT meta FROM memories WHERE id=?", (loaded[0]["_sqlite_id"],)
        )
        self.assertNotIn("embedding", row["meta"])

    async def test_legacy_snapshot_replace_removes_only_legacy_rows(self):
        native_id = await self.layers.memories.add(
            "bili_comment", "native row", actor_id="bili:7"
        )
        first = {
            "rpid": "old",
            "text": "old row",
            "scope": "bili_comment",
            "created_at": 1.0,
        }
        second = {
            "rpid": "keep",
            "text": "keep row",
            "scope": "bili_comment",
            "created_at": 2.0,
        }
        await self.layers.memories.replace_legacy([first, second])
        second["text"] = "updated row"
        await self.layers.memories.replace_legacy([second])

        loaded = await self.layers.memories.load_legacy()
        self.assertEqual([item["rpid"] for item in loaded], ["keep"])
        self.assertEqual(loaded[0]["text"], "updated row")
        native = await self.layers.db.fetch_one(
            "SELECT text FROM memories WHERE id=?", (native_id,)
        )
        self.assertEqual(native["text"], "native row")

    async def test_persona_rest_gate_keeps_urgent_priority_direction(self):
        self.layers.persona.current_segment = AsyncMock(
            return_value=SimpleNamespace(activity="rest")
        )
        urgent, _ = await self.layers.persona.should_respond(0)
        normal, reason = await self.layers.persona.should_respond(40)
        self.assertTrue(urgent)
        self.assertFalse(normal)
        self.assertIn("休息", reason)

    def test_stored_action_digest_uses_security_hash(self):
        key = StoredActionRequest(tool="post_dynamic", args={"text": "hi"}).digest_key()
        self.assertTrue(key.startswith("post_dynamic:none:"))


if __name__ == "__main__":
    unittest.main()
