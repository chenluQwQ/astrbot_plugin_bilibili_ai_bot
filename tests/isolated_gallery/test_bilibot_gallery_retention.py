"""图库历史保留的故障注入；全部使用临时文件及内存 LM，不发布评论。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta

import test_bilibot_image_sync as fixture

gallery = fixture.load("image_gallery")
bridge = fixture.bridge


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bot = fixture.Bot(self.root)
        self.patches = [
            patch.object(gallery, "DATA_DIR", str(self.root)),
            patch.object(gallery, "PROACTIVE_LOG_FILE", str(self.root / "proactive_log.json")),
            patch.object(bridge, "MEDIA_STATE_FILE", str(self.root / "comment_media_state.json")),
            patch.object(bridge, "PROACTIVE_LOG_FILE", str(self.root / "proactive_log.json")),
            patch.object(bridge, "SYNC_STATE_FILE", str(self.root / "livingmemory_sync_state.json")),
        ]
        for item in self.patches:
            item.start()
        gallery._LOCK = asyncio.Lock()
        bridge._SYNC_LOCK = asyncio.Lock()
        (self.root / "liked_images/files").mkdir(parents=True)
        self.content = b"isolated image payload"
        self.sha = hashlib.sha256(self.content).hexdigest()
        self.file = self.root / "liked_images/files" / (self.sha + ".png")
        self.file.write_bytes(self.content)
        self.image = fixture.image(sha256=self.sha, source_bvid="BV_SOURCE")
        gallery.register_asset(self.bot, file_name=self.file.name, image=self.image, embedding=[1., 2.])
        self.event = fixture.comment(images=[self.image])
        self.save_events([self.event])

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def save_events(self, records):
        (self.root / "proactive_log.json").write_text(json.dumps(records, ensure_ascii=False))

    def asset(self):
        with gallery._db() as db:
            return dict(db.execute("SELECT * FROM assets").fetchone())

    def history(self):
        with gallery._db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT record FROM history_events")]

    async def test_evict_preserves_history_and_deletes_only_file_vector(self):
        await bridge.sync(self.bot)
        before = self.bot.engine.rows()[0]
        adds = self.bot.engine.adds
        result = await gallery.evict_asset(self.bot, "liked-cat")
        self.assertTrue(result["ok"])
        self.assertFalse(self.file.exists())
        self.assertIsNone(self.asset()["embedding"])
        after = self.bot.engine.rows()[0]
        self.assertEqual((before["id"], before["text"]), (after["id"], after["text"]))
        self.assertEqual(adds, self.bot.engine.adds)
        snapshot = after["metadata"]["bilibot_images"][0]
        self.assertEqual(snapshot["published_url"], self.image["published_url"])
        self.assertEqual(snapshot["description"], "猫咪捂脸")
        self.assertEqual(snapshot["source_bvid"], "BV_SOURCE")
        self.assertEqual(snapshot["local_status"], "evicted")
        self.assertEqual(self.history()[0]["comment"], self.event["comment"])

    async def test_offline_then_old_log_removed_still_syncs(self):
        self.event["time"] = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        self.save_events([self.event])
        self.bot.lm = None
        result = await gallery.evict_asset(self.bot, "liked-cat")
        self.assertTrue(result["ok"] and result["pending"])
        self.assertFalse(self.file.exists())
        self.save_events([])
        self.bot.lm = fixture.types.SimpleNamespace(initializer=fixture.types.SimpleNamespace(
            memory_engine=self.bot.engine, memory_processor=fixture.Processor()))
        result = await bridge.sync(self.bot)
        self.assertEqual(result["written"], 1)
        self.assertEqual(gallery.status()["history_pending"], 0)
        self.assertEqual(gallery.status()["notify_pending"], 0)
        self.assertEqual(self.bot.engine.rows()[0]["metadata"]["bilibot_images"][0]["local_status"], "evicted")
        second = await bridge.sync(self.bot)
        self.assertEqual(second["written"], 0)

    async def test_journal_failure_stops_deletion(self):
        with patch.object(gallery, "_read_comment_log", side_effect=OSError("disk failure")):
            result = await gallery.evict_asset(self.bot, "liked-cat")
        self.assertFalse(result["ok"])
        self.assertTrue(self.file.exists())
        self.assertEqual(self.asset()["status"], "available")
        self.assertIsNotNone(self.asset()["embedding"])

    async def test_corrupt_log_stops_deletion(self):
        (self.root / "proactive_log.json").write_text("{broken")
        self.assertFalse((await gallery.evict_asset(self.bot, "liked-cat"))["ok"])
        self.assertTrue(self.file.exists())

    async def test_history_transaction_failure_stops_deletion(self):
        with gallery._db() as db:
            db.execute("CREATE TRIGGER fail_history BEFORE INSERT ON history_events "
                       "BEGIN SELECT RAISE(ABORT,'injected journal failure'); END")
        self.assertFalse((await gallery.evict_asset(self.bot, "liked-cat"))["ok"])
        self.assertTrue(self.file.exists())
        self.assertEqual(self.asset()["status"], "available")
        self.assertEqual(gallery.status()["history_events"], 0)

    async def test_tombstone_write_failure_retries_from_journal(self):
        with patch.object(self.bot, "_save_json", side_effect=OSError("marker failure")):
            self.assertFalse((await gallery.evict_asset(self.bot, "liked-cat"))["ok"])
        self.assertTrue(self.file.exists())
        self.assertEqual(self.asset()["status"], "evicting")
        self.assertEqual(gallery.status()["history_events"], 1)
        self.assertTrue((await gallery.recover(self.bot))["ok"])
        self.assertFalse(self.file.exists())

    async def test_unlink_failure_recovery(self):
        with patch.object(gallery.os, "unlink", side_effect=PermissionError("injected")):
            self.assertFalse((await gallery.evict_asset(self.bot, "liked-cat"))["ok"])
        self.assertTrue(self.file.exists())
        self.assertEqual(gallery.status()["cleanup_pending"], 1)
        self.assertTrue((await gallery.recover(self.bot))["ok"])
        self.assertFalse(self.file.exists())

    async def test_missing_file_after_crash_is_idempotent(self):
        await gallery._prepare(self.bot, "liked-cat", "test")
        self.file.unlink()
        self.assertTrue((await gallery.recover(self.bot))["ok"])
        self.assertEqual(self.asset()["status"], "evicted")
        self.assertTrue((await gallery.evict_asset(self.bot, "liked-cat"))["ok"])
        self.assertEqual(gallery.status()["history_events"], 1)

    async def test_corrupt_tombstone_never_deletes(self):
        (self.root / "comment_media_state.json").write_text("{broken")
        self.assertFalse((await gallery.evict_asset(self.bot, "liked-cat"))["ok"])
        self.assertTrue(self.file.exists())

    async def test_symlink_file_and_directory_rejected(self):
        outside = self.root / "outside.png"
        outside.write_bytes(self.content)
        self.file.unlink()
        self.file.symlink_to(outside)
        self.assertFalse((await gallery.evict_asset(self.bot, "liked-cat"))["ok"])
        self.assertTrue(outside.exists())

    async def test_path_traversal_in_catalog_rejected(self):
        with gallery._db() as db, db:
            db.execute("UPDATE assets SET file_name='../outside.png'")
        self.assertFalse((await gallery.evict_asset(self.bot, "liked-cat"))["ok"])
        self.assertTrue(self.file.exists())

    async def test_changed_file_never_deleted(self):
        self.file.write_bytes(b"changed")
        self.assertFalse((await gallery.evict_asset(self.bot, "liked-cat"))["ok"])
        self.assertTrue(self.file.exists())

    async def test_shared_image_across_sessions_and_old_memories(self):
        await bridge.sync(self.bot)
        self.bot.umo = "another:FriendMessage:session"
        await bridge.sync(self.bot)
        texts = [r["text"] for r in self.bot.engine.rows()]
        self.assertEqual(len(texts), 2)
        self.bot.engine.fail_update = True
        result = await gallery.evict_asset(self.bot, "liked-cat")
        self.assertTrue(result["ok"] and result["pending"])
        self.bot.engine.fail_update = False
        await gallery.recover(self.bot)
        self.assertEqual(texts, [r["text"] for r in self.bot.engine.rows()])
        self.assertTrue(all(r["metadata"]["bilibot_images"][0]["local_status"] == "evicted"
                            for r in self.bot.engine.rows()))

    async def test_failed_and_nested_comments_never_archived(self):
        self.save_events([fixture.comment(images=[self.image], send_status="failed"),
                          fixture.comment(images=[self.image], root="99")])
        await gallery.evict_asset(self.bot, "liked-cat")
        self.assertEqual(gallery.status()["history_events"], 0)

    async def test_scope_isolation_and_user_deletion_not_resurrected(self):
        await gallery.evict_asset(self.bot, "liked-cat")
        self.save_events([])
        self.bot.umo = "another:FriendMessage:session"
        await bridge.sync(self.bot)
        self.assertEqual(self.bot.engine.adds, 0)
        self.bot.umo = "test:FriendMessage:isolated"
        await bridge.sync(self.bot)
        self.assertEqual(self.bot.engine.adds, 1)
        self.bot.engine.db_connection.db.execute("DELETE FROM documents")
        await bridge.sync(self.bot)
        self.assertEqual(self.bot.engine.adds, 1)

    async def test_failed_lm_add_keeps_pending_until_success(self):
        await gallery.evict_asset(self.bot, "liked-cat")
        self.save_events([])
        self.bot.engine.fail_add = True
        result = await bridge.sync(self.bot)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(gallery.status()["history_pending"], 1)
        self.bot.engine.fail_add = False
        await bridge.sync(self.bot)
        self.assertEqual(gallery.status()["history_pending"], 0)

    async def test_repeat_concurrent_evictions_do_not_duplicate(self):
        results = await asyncio.gather(*(gallery.evict_asset(self.bot, "liked-cat") for _ in range(3)))
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(gallery.status()["history_events"], 1)
        await bridge.sync(self.bot)
        self.assertEqual(self.bot.engine.adds, 1)

    async def test_dry_run_never_removes_interrupted_file(self):
        await gallery._prepare(self.bot, "liked-cat", "test")
        await bridge.sync(self.bot, dry_run=True)
        self.assertTrue(self.file.exists())
        self.assertEqual(gallery.status()["cleanup_pending"], 1)
        self.assertEqual(gallery.status()["history_pending"], 1)

    async def test_evicted_asset_cannot_reenter_gallery(self):
        await gallery.evict_asset(self.bot, "liked-cat")
        self.file.write_bytes(self.content)
        with self.assertRaises(ValueError):
            gallery.register_asset(self.bot, file_name=self.file.name, image=self.image)

    async def test_gallery_capacity_requires_safe_eviction(self):
        content = b"second isolated image"
        sha = hashlib.sha256(content).hexdigest()
        name = sha + ".png"
        (self.file.parent / name).write_bytes(content)
        with patch.object(gallery, "GALLERY_LIMIT", 1):
            with self.assertRaises(ValueError):
                gallery.register_asset(self.bot, file_name=name, image=fixture.image(asset_id="other", sha256=sha))

    async def test_directory_symlink_never_follows_outside_files(self):
        original = self.file.parent
        moved = self.root / "outside-files"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        self.assertFalse((await gallery.evict_asset(self.bot, "liked-cat"))["ok"])
        self.assertTrue((moved / self.file.name).exists())

    async def test_multiple_comments_keep_each_original_use_reason(self):
        other = fixture.comment(rpid="1002", images=[{**self.image, "use_reason": "表示认同"}])
        self.save_events([self.event, other])
        await gallery.evict_asset(self.bot, "liked-cat")
        self.assertEqual({r["images"][0]["use_reason"] for r in self.history()},
                         {"回应离谱情节", "表示认同"})
        self.save_events([])
        await bridge.sync(self.bot)
        self.assertEqual(len(self.bot.engine.rows()), 2)

    async def test_unreferenced_image_keeps_source_metadata(self):
        self.save_events([])
        await gallery.evict_asset(self.bot, "liked-cat")
        archived = json.loads(self.asset()["snapshot"])
        self.assertEqual(archived["description"], "猫咪捂脸")
        self.assertEqual(archived["source_bvid"], "BV_SOURCE")
        self.assertEqual(gallery.status()["history_events"], 0)


if __name__ == "__main__":
    unittest.main()
