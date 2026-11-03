"""隔离的图片同步回归：不联网、不初始化 AstrBot、不写真实记忆。"""
import ast
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parent
CORE = Path(os.environ.get("BILIBOT_TEST_CORE", str(ROOT.parents[1] / "core")))
PACKAGE = "_isolated_bilibot"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(CORE)]
sys.modules[PACKAGE] = package
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("image-sync-test")
api.sp = types.SimpleNamespace()
sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
sys.modules["astrbot.api"] = api
config = types.ModuleType(PACKAGE + ".config")
for key in ("DATA_DIR", "PROACTIVE_LOG_FILE", "REPLY_LOG_FILE", "WATCH_LOG_FILE"):
    setattr(config, key, key)
# 未初始化图库的测试使用绝对临时路径，避免 Linux 服务账户继承 /root 工作目录。
config.DATA_DIR = str(Path(tempfile.gettempdir()) / f"isolated-bilibot-no-gallery-{id(config)}")
sys.modules[config.__name__] = config
redact = types.ModuleType(PACKAGE + ".security.redact")
redact.redact_outbound = lambda text, **kwargs: (text, [])
redact.redact_pii = lambda text: text
sys.modules[redact.__name__] = redact


def load(name):
    spec = importlib.util.spec_from_file_location(PACKAGE + "." + name, CORE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


media = load("comment_media")
bridge = load("livingmemory_bridge")
bridge.target_umo = lambda bot: bot.umo


async def persona(context, umo):
    return "test-persona"


bridge._resolve_persona_id = persona


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    async def fetchall(self):
        return self.cursor.fetchall()


class Database:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)")

    async def execute(self, sql, params=()):
        return Cursor(self.db.execute(sql, params))


class Engine:
    def __init__(self):
        self.db_connection = Database()
        self.index_writer = object()
        self.adds = self.replaces = self.updates = 0
        self.fail_add = self.fail_update = self.fail_replace = False

    def rows(self):
        return [dict(r, metadata=json.loads(r["metadata"])) for r in
                self.db_connection.db.execute("SELECT * FROM documents")]

    async def add_memory(self, content, session_id=None, persona_id=None, importance=0.5, metadata=None):
        if self.fail_add:
            raise RuntimeError("injected add failure")
        self.adds += 1
        meta = {"session_id": session_id, "persona_id": persona_id, "create_time": 123.0, **metadata}
        cursor = self.db_connection.db.execute(
            "INSERT INTO documents(text,metadata) VALUES (?,?)", (content, json.dumps(meta)))
        return cursor.lastrowid

    async def replace_memory(self, memory_id, *, content, metadata, importance):
        if self.fail_replace:
            raise RuntimeError("injected replacement failure")
        old = next(r for r in self.rows() if r["id"] == memory_id)
        new_meta = {**old["metadata"], **metadata}
        new_id = await self.add_memory(content, metadata=new_meta)
        self.db_connection.db.execute("DELETE FROM documents WHERE id=?", (memory_id,))
        self.replaces += 1
        return new_id

    async def update_memory(self, memory_id, updates):
        if self.fail_update:
            return False
        old = next(r for r in self.rows() if r["id"] == memory_id)
        meta = {**old["metadata"], **updates["metadata"]}
        self.db_connection.db.execute(
            "UPDATE documents SET metadata=? WHERE id=?", (json.dumps(meta), memory_id))
        self.updates += 1
        return True


class Processor:
    def build_memory_from_structured_data(self, *, structured_data, **kwargs):
        s = structured_data
        content = s["summary"] + " | " + "；".join(s["key_facts"][:5])
        return content, {
            "persona_summary": s["summary"], "canonical_summary": content,
            "key_facts": s["key_facts"], "topics": s["topics"],
        }, s["importance"]


class Bot:
    def __init__(self, path):
        self.path = path
        self.umo = "test:FriendMessage:isolated"
        self.config = {}
        self.context = types.SimpleNamespace()
        self.engine = Engine()
        self.lm = types.SimpleNamespace(initializer=types.SimpleNamespace(
            memory_engine=self.engine, memory_processor=Processor()))
        self.context.get_registered_star = lambda name: types.SimpleNamespace(star_cls=self.lm)
        self.fail_state_save = False

    def _load_json(self, name, default):
        p = self.path / Path(name).name
        return json.loads(p.read_text()) if p.exists() else default

    def _save_json(self, name, value):
        if self.fail_state_save and name == bridge.SYNC_STATE_FILE:
            raise OSError("injected state write failure")
        (self.path / Path(name).name).write_text(json.dumps(value, ensure_ascii=False))


def image(**patch):
    return {
        "asset_id": "liked-cat", "sha256": "a" * 64, "description": "猫咪捂脸",
        "emotion": "无奈", "use_reason": "回应离谱情节",
        "source_url": "https://i0.hdslb.com/source.jpg",
        "published_url": "https://i0.hdslb.com/uploaded.jpg",
        "local_status": "available", **patch,
    }


def comment(**patch):
    return {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bvid": "BV_ISOLATED", "title": "隔离视频", "comment": "这个情节太离谱了",
            "rpid": "1001", "send_status": "published", **patch}


class MediaTests(unittest.TestCase):
    def normalize(self, images, published=True):
        return media.normalize_images(images, published=published, clip=bridge._clip)

    def test_sha_dedup(self):
        self.assertEqual(len(self.normalize([image(), image(asset_id="other")])), 1)

    def test_asset_dedup_without_hash(self):
        self.assertEqual(len(self.normalize([image(sha256=""), image(sha256="")])), 1)

    def test_url_dedup_without_identity(self):
        self.assertEqual(len(self.normalize([image(sha256="", asset_id="")] * 2)), 1)

    def test_non_image_payload_ignored(self):
        self.assertEqual(self.normalize([None, "x", {}]), [])

    def test_image_limit(self):
        images = [image(sha256=f"{i:064x}", asset_id=str(i)) for i in range(20)]
        self.assertEqual(len(self.normalize(images)), 9)

    def test_no_binary_paths_vectors_or_credentials(self):
        output = self.normalize([image(
            path="/private/key", embedding=[1, 2], api_key="secret", binary="blob",
            published_url="https://i0.hdslb.com/u.jpg?token=secret",
        )])[0]
        self.assertEqual(output["published_url"], "https://i0.hdslb.com/u.jpg")
        self.assertFalse({"path", "embedding", "api_key", "binary"} & output.keys())

    def test_draft_does_not_claim_uploaded_url(self):
        self.assertEqual(self.normalize([image()], published=False)[0]["published_url"], "")

    def test_disallow_local_and_foreign_urls(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1/img", "https://hdslb.com.evil/img",
                    "https://user:pwd@i0.hdslb.com/x", "https://i0.hdslb.com:9000/x"):
            self.assertEqual(media.public_bili_url(url), "")

    def test_event_key_stable_across_image_status_changes(self):
        self.assertEqual(media.comment_key(comment(images=[])),
                         media.comment_key(comment(images=[image()], send_status="draft")))

    def test_merge_preserves_missing_historical_fields(self):
        old = self.normalize([image()])
        merged = media.merge_images(old, self.normalize([image(description="", published_url="")]))
        self.assertEqual(merged[0]["description"], "猫咪捂脸")
        self.assertTrue(merged[0]["published_url"])

    def test_eviction_retains_original_snapshot(self):
        old = self.normalize([image()])
        new = media.apply_evictions(old, {"liked-cat": 456})
        self.assertEqual(new[0]["local_status"], "evicted")
        self.assertEqual(new[0]["description"], old[0]["description"])
        self.assertEqual(new[0]["published_url"], old[0]["published_url"])
        self.assertEqual(old[0]["local_status"], "available")

    def test_evicted_snapshot_not_accidentally_reactivated(self):
        old = self.normalize([image(local_status="evicted")])
        self.assertEqual(media.merge_images(old, self.normalize([image()]))[0]["local_status"], "evicted")

    def test_floor_reply_has_no_images(self):
        item = bridge._comment_item(comment(root="200", images=[image()]))
        self.assertEqual(item["metadata"]["bilibot_images"], [])


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = Bot(Path(self.temp.name))
        bridge._SYNC_LOCK = asyncio.Lock()

    async def asyncTearDown(self):
        self.bot.engine.db_connection.db.close()
        self.temp.cleanup()

    def logs(self, comments=None, watch=None, replies=None):
        self.bot._save_json(config.PROACTIVE_LOG_FILE, comments or [])
        self.bot._save_json(config.WATCH_LOG_FILE, watch or [])
        self.bot._save_json(config.REPLY_LOG_FILE, replies or [])

    async def sync(self):
        result = await bridge.sync(self.bot, reason="isolated-test")
        self.assertTrue(result["ok"], result)
        return result

    async def test_repeat_no_writes(self):
        self.logs([comment(images=[image(), image()])])
        await self.sync()
        result = await self.sync()
        self.assertEqual((result["written"], result["updated"]), (0, 0))
        self.assertEqual(len(self.bot.engine.rows()), 1)

    async def test_late_images_update_existing_comment(self):
        self.logs([comment()])
        await self.sync()
        self.logs([comment(images=[image()])])
        result = await self.sync()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(self.bot.engine.rows()), 1)
        self.assertIn("猫咪捂脸", self.bot.engine.rows()[0]["text"])
        self.assertEqual(self.bot.engine.rows()[0]["metadata"]["create_time"], 123)
        self.assertEqual((await self.sync())["updated"], 0)

    async def test_multiple_comments_same_video_distinct(self):
        self.logs([comment(images=[image()]), comment(rpid="1002", images=[image()])])
        result = await self.sync()
        self.assertEqual(result["written"], 2)
        self.assertEqual(len(self.bot.engine.rows()), 2)

    async def test_duplicate_records_same_rpid(self):
        rec = comment(images=[image()])
        self.logs([rec, dict(rec)])
        self.assertEqual((await self.sync())["written"], 1)

    async def test_status_transition_draft_to_published(self):
        self.logs([comment(send_status="draft", images=[image()])])
        await self.sync()
        self.assertEqual(self.bot.engine.rows()[0]["metadata"]["bilibot_images"][0]["published_url"], "")
        self.logs([comment(images=[image()])])
        await self.sync()
        self.assertIn("已发布", self.bot.engine.rows()[0]["text"])
        self.assertTrue(self.bot.engine.rows()[0]["metadata"]["bilibot_images"][0]["published_url"])

    async def test_unknown_and_failed_never_published(self):
        for status in ("unknown", "failed"):
            self.logs([comment(send_status=status, images=[image()])])
            await self.sync()
            self.assertEqual(self.bot.engine.rows()[0]["metadata"]["bilibot_images"][0]["published_url"], "")

    async def test_eviction_outside_log_window_and_other_session(self):
        self.logs([comment(images=[image()])])
        await self.sync()
        row = self.bot.engine.rows()[0]
        meta = {**row["metadata"], "session_id": "other:session"}
        await self.bot.engine.add_memory(row["text"], metadata=meta)
        self.logs()
        result = await bridge.mark_asset_evicted(self.bot, "liked-cat")
        self.assertEqual(result["updated"], 2)
        self.assertEqual(self.bot.engine.replaces, 0)
        for r in self.bot.engine.rows():
            self.assertEqual(r["metadata"]["bilibot_images"][0]["local_status"], "evicted")
            self.assertIn("猫咪捂脸", r["text"])

    async def test_offline_eviction_replayed_after_restart(self):
        self.logs([comment(images=[image()])])
        await self.sync()
        lm = self.bot.lm
        self.bot.lm = None
        self.assertTrue((await bridge.mark_asset_evicted(self.bot, "liked-cat"))["pending"])
        self.bot.lm = lm
        self.logs()
        self.assertEqual((await self.sync())["evictions_updated"], 1)
        self.assertEqual((await self.sync())["evictions_updated"], 0)

    async def test_metadata_url_only_change_no_reembedding(self):
        self.logs([comment(images=[image(published_url="")])])
        await self.sync()
        self.logs([comment(images=[image()])])
        await self.sync()
        self.assertEqual(self.bot.engine.replaces, 0)
        self.assertEqual(self.bot.engine.updates, 1)

    async def test_state_write_failure_does_not_duplicate(self):
        self.logs([comment(images=[image()])])
        self.bot.fail_state_save = True
        self.assertFalse((await self.sync())["state_saved"])
        self.bot.fail_state_save = False
        self.assertEqual((await self.sync())["written"], 0)
        self.assertEqual(len(self.bot.engine.rows()), 1)

    async def test_failed_add_retry(self):
        self.logs([comment(images=[image()])])
        self.bot.engine.fail_add = True
        self.assertEqual((await self.sync())["failed"], 1)
        self.bot.engine.fail_add = False
        self.assertEqual((await self.sync())["written"], 1)

    async def test_failed_replace_keeps_old_and_retries(self):
        self.logs([comment()])
        await self.sync()
        self.logs([comment(images=[image()])])
        self.bot.engine.fail_replace = True
        self.assertEqual((await self.sync())["failed"], 1)
        self.assertEqual(len(self.bot.engine.rows()), 1)
        self.bot.engine.fail_replace = False
        self.assertEqual((await self.sync())["updated"], 1)

    async def test_manual_and_auto_parallel_dedup(self):
        self.logs([comment(images=[image()])])
        await asyncio.gather(bridge.sync(self.bot), bridge.sync(self.bot))
        self.assertEqual(len(self.bot.engine.rows()), 1)

    async def test_manual_deleted_memory_not_resurrected(self):
        self.logs([comment(images=[image()])])
        await self.sync()
        self.bot.engine.db_connection.db.execute("DELETE FROM documents")
        self.assertEqual((await self.sync())["written"], 0)

    async def test_target_scope_isolated(self):
        self.logs([comment(images=[image()])])
        await self.sync()
        self.bot.umo = "different:session"
        self.assertEqual((await self.sync())["written"], 1)
        self.assertEqual(len(self.bot.engine.rows()), 2)

    async def test_watch_draft_and_later_published_comment(self):
        watch = {"time": comment()["time"], "bvid": "BV_ISOLATED", "title": "隔离视频",
                 "score": 8, "score_reason": "情节有趣", "comment": "草稿"}
        self.logs(watch=[watch])
        await self.sync()
        self.assertIn("本地评论草稿", self.bot.engine.rows()[0]["text"])
        self.logs([comment(images=[image()])], watch=[watch])
        self.assertEqual((await self.sync())["updated"], 1)
        self.assertEqual(len(self.bot.engine.rows()), 2)

    async def test_legacy_watch_upgraded_once_without_duplication(self):
        watch = {"time": comment()["time"], "bvid": "BV_ISOLATED", "title": "隔离视频", "score": 8}
        self.logs(watch=[watch])
        item = bridge._watch_item(watch)
        content, metadata, _ = Processor().build_memory_from_structured_data(structured_data=item["structured"])
        await self.bot.engine.add_memory(content, self.bot.umo, "test-persona",
                                         metadata={**metadata, **item["metadata"], "memory_origin": "bilibot"})
        self.bot._save_json(bridge.SYNC_STATE_FILE, {"keys": [item["key"]]})
        self.assertEqual((await self.sync())["updated"], 1)
        self.assertEqual(len(self.bot.engine.rows()), 1)
        self.assertEqual((await self.sync())["updated"], 0)

    async def test_identical_legacy_copies_adopt_earliest_without_deleting(self):
        watch = {"time": comment()["time"], "bvid": "BV_ISOLATED", "title": "隔离视频", "score": 8}
        self.logs(watch=[watch])
        item = bridge._watch_item(watch)
        content, metadata, _ = Processor().build_memory_from_structured_data(structured_data=item["structured"])
        for _ in range(2):
            await self.bot.engine.add_memory(content, self.bot.umo, "test-persona",
                metadata={**metadata, **item["metadata"], "memory_origin": "bilibot"})
        self.assertEqual((await self.sync())["updated"], 1)
        rows = self.bot.engine.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["metadata"].get("bilibot_sync_key"))
        self.assertFalse(rows[1]["metadata"].get("bilibot_sync_key"))

    async def test_different_legacy_candidates_block_changes(self):
        watch = {"time": comment()["time"], "bvid": "BV_ISOLATED", "title": "隔离视频", "score": 8}
        self.logs(watch=[watch])
        item = bridge._watch_item(watch)
        content, metadata, _ = Processor().build_memory_from_structured_data(structured_data=item["structured"])
        for text in (content, content + " 不同历史事实"):
            await self.bot.engine.add_memory(text, self.bot.umo, "test-persona",
                metadata={**metadata, **item["metadata"], "memory_origin": "bilibot"})
        self.assertEqual((await self.sync())["failed"], 1)
        self.assertEqual(self.bot.engine.updates, 0)
        self.assertEqual(self.bot.engine.replaces, 0)

    async def test_unrelated_reply_and_fields_preserved(self):
        self.logs([comment()])
        await self.sync()
        row = self.bot.engine.rows()[0]
        await self.bot.engine.update_memory(row["id"], {"metadata": {"custom_note": "keep"}})
        await self.bot.engine.add_memory("独立回复", self.bot.umo, "test-persona",
            metadata={"memory_origin": "bilibot", "bilibot_kind": "reply", "bilibot_bvid": "other"})
        self.logs([comment(images=[image()])])
        await self.sync()
        self.assertTrue(any(r["text"] == "独立回复" for r in self.bot.engine.rows()))
        self.assertTrue(any(r["metadata"].get("custom_note") == "keep" for r in self.bot.engine.rows()))

    async def test_eviction_failure_retries(self):
        self.logs([comment(images=[image()])])
        await self.sync()
        self.bot.engine.fail_update = True
        self.assertTrue((await bridge.mark_asset_evicted(self.bot, "liked-cat"))["pending"])
        self.bot.engine.fail_update = False
        self.assertEqual((await self.sync())["evictions_updated"], 1)

    async def test_status_pending_after_sync_is_zero(self):
        self.logs([comment(images=[image()])])
        await self.sync()
        self.assertEqual((await bridge.status(self.bot))["pending"], 0)

    async def test_dry_run_no_database_or_state_writes(self):
        self.logs([comment(images=[image()])])
        result = await bridge.sync(self.bot, dry_run=True)
        self.assertEqual(result["written"], 1)
        self.assertEqual(self.bot.engine.rows(), [])
        self.assertFalse((self.bot.path / Path(bridge.SYNC_STATE_FILE).name).exists())

    async def test_confirmed_publication_cannot_be_downgraded(self):
        self.logs([comment(images=[image()])])
        await self.sync()
        self.logs([comment(send_status="unknown", images=[])])
        self.assertEqual((await self.sync())["updated"], 0)
        self.assertEqual(self.bot.engine.rows()[0]["metadata"]["bilibot_send_status"], "published")


class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.unknown_error = load("runtime").WriteResultUnknownError
        tree = ast.parse((CORE / "bilibili.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_comment")
        module = ast.Module(body=[method], type_ignores=[])
        namespace = {
            "json": json, "logger": api.logger, "BILI_REPLY_URL": "isolated:no-network",
            "WriteResultUnknownError": self.unknown_error,
        }
        exec(compile(ast.fix_missing_locations(module), "<isolated sender>", "exec"), namespace)
        self.send = namespace["_send_comment"]
        self.bot = types.SimpleNamespace(config={}, response={"code": 0, "data": {"reply": {"rpid": 987}}})

        async def post(*args, **kwargs):
            if isinstance(self.bot.response, Exception):
                raise self.bot.response
            return self.bot.response, None

        self.bot._http_post = post

    async def test_published_only_after_success(self):
        sink = {}
        self.assertTrue(await self.send(self.bot, 1, "测试", result_sink=sink))
        self.assertEqual(sink, {"send_status": "published", "rpid": "987"})

    async def test_failed_not_published(self):
        self.bot.response = {"code": -403}
        sink = {}
        self.assertFalse(await self.send(self.bot, 1, "测试", result_sink=sink))
        self.assertEqual(sink["send_status"], "failed")

    async def test_timeout_unknown_not_failed_or_published(self):
        self.bot.response = TimeoutError("injected timeout")
        sink = {}
        self.assertFalse(await self.send(self.bot, 1, "测试", result_sink=sink))
        self.assertEqual(sink["send_status"], "unknown")

    async def test_runtime_unknown_error_is_propagated(self):
        self.bot.response = self.unknown_error("injected transport uncertainty")
        sink = {}
        with self.assertRaises(self.unknown_error):
            await self.send(self.bot, 1, "测试", result_sink=sink)
        self.assertEqual(sink["send_status"], "unknown")

    async def test_legacy_bool_call_unchanged(self):
        self.assertIs(await self.send(self.bot, 1, "测试"), True)

    async def test_success_without_optional_rpid_not_turned_into_failure(self):
        self.bot.response = {"code": 0, "data": "malformed optional metadata"}
        self.assertIs(await self.send(self.bot, 1, "测试", result_sink={}), True)


if __name__ == "__main__":
    unittest.main()
