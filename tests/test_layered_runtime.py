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
