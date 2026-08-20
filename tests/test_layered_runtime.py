import asyncio
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
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

    async def test_behavior_budget_is_atomic_and_shared_by_actions(self):
        await self.layers.close()
        self.layers = LayeredRuntime(
            {
                "DEDE_USER_ID": "bot-1",
                "OWNER_MID": "42",
                "BEHAVIOR_GLOBAL_MAX_PER_MINUTE": 1,
                "BEHAVIOR_GLOBAL_DAILY_LIMIT": 0,
                "AUTONOMOUS_REPLY_DAILY_LIMIT": 0,
            },
            Path(self.temp_dir.name) / "bilibot.sqlite3",
        )
        await self.layers.open()
        self.runtime = EventRuntime(observer=self.layers)
        calls = 0

        async def send():
            nonlocal calls
            calls += 1
            return True

        first, second = await asyncio.gather(
            self.runtime.execute(
                ActionRequest(key="budget:1", kind="comment_reply"), send
            ),
            self.runtime.execute(
                ActionRequest(key="budget:2", kind="private_reply"), send
            ),
        )

        self.assertEqual(sum(outcome.success for outcome in (first, second)), 1)
        denied = second if first.success else first
        self.assertTrue(denied.reason.startswith("budget_exhausted:"))
        self.assertFalse(denied.duplicate)
        self.assertEqual(calls, 1)

    async def test_definite_failure_refunds_budget_but_unknown_does_not(self):
        await self.layers.close()
        self.layers = LayeredRuntime(
            {
                "BEHAVIOR_GLOBAL_MAX_PER_MINUTE": 0,
                "BEHAVIOR_GLOBAL_DAILY_LIMIT": 1,
                "AUTONOMOUS_REPLY_DAILY_LIMIT": 0,
                "AUTONOMOUS_PRIVATE_DAILY_LIMIT": 0,
            },
            Path(self.temp_dir.name) / "bilibot.sqlite3",
        )
        await self.layers.open()
        self.runtime = EventRuntime(observer=self.layers, action_timeout=0.01)

        failed = await self.runtime.execute(
            ActionRequest(key="refund:failed", kind="comment_reply"), lambda: False
        )
        after_refund = await self.runtime.execute(
            ActionRequest(key="refund:success", kind="private_reply"), lambda: True
        )

        self.assertFalse(failed.success)
        self.assertTrue(after_refund.success)

        async def timeout_send():
            await asyncio.sleep(0.1)
            return True

        # The successful action already consumes the only daily slot. Give the
        # timeout its own exempted global setup, then verify its state directly.
        unknown = await self.runtime.execute(
            ActionRequest(
                key="unknown:1",
                kind="private_reply",
                metadata={"budget_exempt": True},
            ),
            timeout_send,
        )
        self.assertEqual(unknown.state, "unknown")
        row = await self.layers.db.fetch_one(
            "SELECT state FROM actions WHERE key='unknown:1'"
        )
        self.assertEqual(row["state"], "unknown")
        duplicate = await EventRuntime(observer=self.layers).execute(
            ActionRequest(
                key="unknown:1",
                kind="private_reply",
                metadata={"budget_exempt": True},
            ),
            lambda: True,
        )
        self.assertFalse(duplicate.success)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.state, "unknown")

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

    async def test_preference_lifecycle_is_idempotent_and_supports_decay(self):
        at = datetime(2026, 8, 20, 12, tzinfo=timezone.utc).timestamp()

        async def add(source, signal_type, value, polarity, strength, days_ago):
            return await self.layers.preferences.record_video_signals(
                source_ref=source,
                occurred_at=at - days_ago * 86400,
                signals=[{
                    "type": signal_type,
                    "value": value,
                    "polarity": polarity,
                    "strength": strength,
                    "evidence": "测试证据",
                }],
            )

        self.assertEqual(await add("BV-candidate", "theme", "灯塔", "curious", 0.7, 0), 1)
        self.assertEqual(await add("BV-candidate", "theme", "灯塔", "curious", 0.7, 0), 0)
        await add("BV-up-1", "up", "泛式", "like", 0.9, 1)
        await add("BV-up-2", "up", "泛式", "like", 0.8, 3)
        await add("BV-eva-1", "work", "EVA", "like", 0.8, 16)
        await add("BV-eva-2", "work", "EVA", "like", 0.9, 9)
        await add("BV-eva-3", "work", "EVA", "like", 1.0, 2)
        await add("BV-food-1", "food", "白酒测评", "fatigue", 0.8, 1)
        await add("BV-food-2", "food", "白酒测评", "fatigue", 0.9, 2)

        refreshed = await self.layers.preferences.refresh(at=at)
        by_value = {item["value"]: item for item in refreshed["current"]}
        self.assertEqual(by_value["灯塔"]["stage"], "candidate")
        self.assertEqual(by_value["泛式"]["stage"], "recent")
        self.assertEqual(by_value["EVA"]["stage"], "stable")
        self.assertEqual(by_value["白酒测评"]["polarity"], "fatigue")

        decayed = await self.layers.preferences.refresh(at=at + 10 * 86400)
        decayed_by_value = {item["value"]: item for item in decayed["current"]}
        self.assertNotIn("灯塔", decayed_by_value)
        self.assertEqual(decayed_by_value["泛式"]["lifecycle_action"], "weakened")
        self.assertEqual(decayed_by_value["EVA"]["stage"], "stable")

        expired = await self.layers.preferences.refresh(at=at + 110 * 86400)
        self.assertEqual(expired["current"], [])
        self.assertTrue(any(item["lifecycle_action"] == "deleted" for item in expired["changes"]))

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

    async def test_schema_v2_database_adds_action_queue_columns(self):
        legacy_path = Path(self.temp_dir.name) / "legacy-v2.sqlite3"
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            "CREATE TABLE actions (key TEXT PRIMARY KEY,kind TEXT NOT NULL,"
            "event_key TEXT NOT NULL DEFAULT '',target_id TEXT NOT NULL DEFAULT '',"
            "state TEXT NOT NULL DEFAULT 'running',digest TEXT NOT NULL DEFAULT '',"
            "detail TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,finished_at REAL)"
        )
        conn.commit()
        conn.close()

        migrated = LayeredRuntime({}, legacy_path)
        await migrated.open()
        columns = await migrated.db.fetch_all("PRAGMA table_info(actions)")
        await migrated.close()

        names = {row["name"] for row in columns}
        self.assertTrue({"priority", "attempts", "budget", "updated_at"} <= names)

    async def test_restart_refunds_queued_and_quarantines_running_actions(self):
        at = 1000.0
        reservation = json.dumps(
            [
                {
                    "bucket": "behavior:global:day",
                    "window_key": "test-day",
                    "amount": 1,
                }
            ]
        )
        await self.layers.db.execute(
            "INSERT INTO counters(bucket,window_key,count,updated_at) VALUES(?,?,?,?)",
            ("behavior:global:day", "test-day", 1, at),
        )
        await self.layers.db.execute(
            "INSERT INTO actions(key,kind,state,budget,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            ("restart:queued", "comment_reply", "queued", reservation, at, at),
        )
        await self.layers.db.execute(
            "INSERT INTO actions(key,kind,state,budget,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            ("restart:running", "private_reply", "running", reservation, at, at),
        )
        await self.layers.close()
        self.layers = LayeredRuntime(
            {"DEDE_USER_ID": "bot-1"},
            Path(self.temp_dir.name) / "bilibot.sqlite3",
        )
        await self.layers.open()

        queued = await self.layers.db.fetch_one(
            "SELECT state,detail FROM actions WHERE key='restart:queued'"
        )
        running = await self.layers.db.fetch_one(
            "SELECT state,detail FROM actions WHERE key='restart:running'"
        )
        counter = await self.layers.db.fetch_value(
            "SELECT count FROM counters WHERE bucket=? AND window_key=?",
            ("behavior:global:day", "test-day"),
        )
        self.assertEqual(queued["state"], "failed")
        self.assertEqual(queued["detail"], "restart_before_send")
        self.assertEqual(running["state"], "unknown")
        self.assertEqual(running["detail"], "restart_during_send")
        self.assertEqual(counter, 0)

    async def test_seen_video_ledger_is_uncapped_and_survives_restart(self):
        for index in range(260):
            await self.layers.seen_videos.mark_seen(
                f"BV{index:010d}", title=f"视频{index}", source="test"
            )
        self.assertEqual(await self.layers.seen_videos.count(), 260)
        self.assertTrue(await self.layers.seen_videos.contains("bv0000000000"))

        database_path = Path(self.temp_dir.name) / "bilibot.sqlite3"
        await self.layers.close()
        self.layers = LayeredRuntime({}, database_path)
        await self.layers.open()

        self.assertEqual(await self.layers.seen_videos.count(), 260)
        first = await self.layers.seen_videos.get("BV0000000000")
        self.assertEqual(first["title"], "视频0")

    async def test_seen_video_legacy_import_is_idempotent(self):
        record = {
            "bvid": "BV1TEST00000",
            "first_seen_at": 100.0,
            "last_related_at": 200.0,
            "title": "旧视频",
            "source": "legacy",
        }
        self.assertEqual(await self.layers.seen_videos.import_many([record]), 1)
        self.assertEqual(await self.layers.seen_videos.import_many([record]), 0)
        row = await self.layers.seen_videos.get(record["bvid"])
        self.assertEqual(row["watch_count"], 1)
        self.assertEqual(row["first_seen_at"], 100.0)
        self.assertEqual(row["last_related_at"], 200.0)

    async def test_feedback_candidates_are_idempotent_and_relation_weighted(self):
        owner = await self.layers.feedback.record_candidate(
            event_key="bili_comment:owner-1", actor_id="42", actor_name="主人",
            scope="bili_comment", feedback_type="suggestion", topic="回复太机械",
            event_summary="主人建议说话自然一点",
            possible_mistake="回复像客服模板", next_time="先回应具体内容",
            confidence=0.95, relation_weight=3.0, is_owner=True,
        )
        duplicate = await self.layers.feedback.record_candidate(
            event_key="bili_comment:owner-1", actor_id="42", actor_name="主人",
            scope="bili_comment", feedback_type="suggestion", topic="回复太机械",
            relation_weight=3.0, is_owner=True,
        )
        await self.layers.feedback.record_candidate(
            event_key="bili_comment:user-1", actor_id="99", actor_name="群友",
            scope="bili_comment", feedback_type="suggestion", topic="回复太机械",
            next_time="少用服务式反问", confidence=0.8, relation_weight=1.0,
        )

        self.assertTrue(owner)
        self.assertFalse(duplicate)
        aggregate = await self.layers.feedback.aggregate(days=7)
        self.assertEqual(aggregate[0]["count"], 2)
        self.assertEqual(aggregate[0]["distinct_actors"], 2)
        self.assertEqual(aggregate[0]["owner_count"], 1)
        self.assertEqual(aggregate[0]["weighted_score"], 4.0)

    async def test_feedback_recall_requires_support_and_scene_relevance(self):
        await self.layers.feedback.record_candidate(
            event_key="comment:owner-mechanical", actor_id="42", actor_name="主人",
            scope="bili_comment", feedback_type="correction", topic="机械回复",
            next_time="先回应评论里的具体内容", relation_weight=3.0,
            is_owner=True,
        )
        await self.layers.feedback.record_candidate(
            event_key="comment:single-service", actor_id="99", actor_name="普通用户",
            scope="bili_comment", feedback_type="suggestion", topic="客服腔",
            next_time="少用服务式结尾", relation_weight=1.0,
        )
        await self.layers.feedback.record_candidate(
            event_key="comment:owner-download", actor_id="42", actor_name="主人",
            scope="bili_comment", feedback_type="correction", topic="视频下载失败",
            next_time="更换下载格式", relation_weight=3.0, is_owner=True,
        )

        relevant = await self.layers.feedback.relevant(
            "你这次回复太机械了，也有客服腔", days=30
        )

        self.assertEqual([item["topic"] for item in relevant], ["机械回复"])
        self.assertGreater(relevant[0]["relevance_score"], 0)

    def test_stored_action_digest_uses_security_hash(self):
        key = StoredActionRequest(tool="post_dynamic", args={"text": "hi"}).digest_key()
        self.assertTrue(key.startswith("post_dynamic:none:"))


if __name__ == "__main__":
    unittest.main()
