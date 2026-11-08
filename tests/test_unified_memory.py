from base_parts import BaseParts
from memory_parts import memory_parts
import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core.domain.user_profiles import UserProfiles
from vision_fakes import vision_on
from core.layered_runtime import LayeredRuntime
from core.storage.json_file import JsonFile


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _load_memory_module(temp_dir):
    package_name = "unified_memory_test_core"
    package = sys.modules.setdefault(package_name, types.ModuleType(package_name))
    package.__path__ = [str(ROOT / "core")]
    # 每个测试换一份假 config：读路径常量的子模块（domain/ 下的）也要重新加载，
    # 否则还拿着上一个测试临时目录里的路径。
    for name in [n for n in sys.modules if n.startswith((f"{package_name}.domain", f"{package_name}.features"))]:
        del sys.modules[name]

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.logger = getattr(api, "logger", _Logger())
    astrbot.api = api

    config_name = f"{package_name}.config"
    config = types.ModuleType(config_name)
    config.MAX_SEMANTIC_RESULTS = 3
    config.MEMORY_FILE = str(Path(temp_dir) / "memory.json")
    config.MEMORY_SYNC_STATE_FILE = str(Path(temp_dir) / "memory_sync_state.json")
    config.PERMANENT_MEMORY_FILE = str(Path(temp_dir) / "permanent_memory.json")
    config.THREAD_COMPRESS_THRESHOLD = 8
    config.OID_COMPRESS_THRESHOLD = 20
    config.OID_KEEP_RECENT = 8
    config.USER_MEMORY_COMPRESS_THRESHOLD = 20
    config.USER_MEMORY_KEEP_RECENT = 5
    config.AFFECTION_FILE = str(Path(temp_dir) / "affection.json")
    config.BLOCK_KEYWORDS = []
    config.INJECTION_PATTERNS = []
    config.LEVEL_NAMES = {}
    config.MILESTONE_FILE = str(Path(temp_dir) / "milestones.json")
    config.MOOD_FILE = str(Path(temp_dir) / "mood.json")
    config.SECURITY_LOG_FILE = str(Path(temp_dir) / "security.json")
    config.USER_PROFILE_FILE = str(Path(temp_dir) / "profiles.json")
    config.WATCH_LOG_FILE = str(Path(temp_dir) / "watch_log.json")
    config.COMMENTED_FILE = str(Path(temp_dir) / "commented_videos.json")
    config.VIDEO_MEMORY_FILE = str(Path(temp_dir) / "video_memory.json")
    config.EXTERNAL_MEMORY_FILE = str(Path(temp_dir) / "external_memory.json")
    config.SEEN_VIDEOS_FILE = str(Path(temp_dir) / "seen_videos.json")
    sys.modules[config_name] = config

    # 原来在这里加载 core/memory.py（MemoryMixin）；现在记忆的几个对象由 MemoryParts 在这个包名下现建
    return types.SimpleNamespace(MemoryParts=memory_parts(package_name)), config


class _MemoryBotBase(BaseParts):
    config = {}

    vision = property(vision_on)

    def _load_json(self, path, default=None):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {} if default is None else default

    def _save_json(self, path, data):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )


class UnifiedMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.module, self.config = _load_memory_module(
            self.temp_dir.name
        )
        self.layers = LayeredRuntime(
            {"DEDE_USER_ID": "bot-1"}, Path(self.temp_dir.name) / "bilibot.sqlite3"
        )
        await self.layers.open()

        class Bot(self.module.MemoryParts, _MemoryBotBase):
            pass

        self.bot_type = Bot

    async def asyncTearDown(self):
        await self.layers.close()
        self.temp_dir.cleanup()

    def bot(self, records):
        bot = self.bot_type()
        bot._memory = list(records)
        bot.layered_runtime = self.layers
        bot.user_profiles = UserProfiles(JsonFile(self.config.USER_PROFILE_FILE))
        return bot

    async def test_first_start_migrates_then_sqlite_becomes_canonical(self):
        original = {
            "rpid": "legacy-1",
            "text": "旧记忆",
            "time": "2026-08-15 20:00",
            "source": "bilibili",
            "user_id": "42",
            "memory_type": "chat",
            "embedding": [0.1, 0.2],
        }
        bot = self.bot([original])
        self.assertTrue(await bot.memory_startup._initialize_unified_memory())
        await bot.memories.save(
            {
                "rpid": "new-2",
                "text": "新记忆",
                "time": "2026-08-16 12:00",
                "source": "bilibili_private",
                "user_id": "42",
                "memory_type": "chat",
            }
        )

        # 兼容备份被外部旧代码改坏时，只要没有待同步标记，下次启动仍以 SQLite 为准。
        Path(self.config.MEMORY_FILE).write_text("[]", encoding="utf-8")
        restarted = self.bot([])
        await restarted.memory_startup._initialize_unified_memory()
        self.assertEqual(
            {item["rpid"] for item in restarted._memory}, {"legacy-1", "new-2"}
        )
        self.assertEqual(
            next(item for item in restarted._memory if item["rpid"] == "new-2")["scope"],
            "bili_dm",
        )

    def test_channel_visibility_blocks_cross_user_and_cross_scope_recall(self):
        bot = self.bot([])
        comment = bot.memory_entry._prepare_memory_entry(
            {"rpid": "c", "text": "公开评论", "source": "bilibili", "user_id": "42"}
        )
        dm = bot.memory_entry._prepare_memory_entry(
            {"rpid": "d", "text": "私信秘密", "source": "bilibili_private", "user_id": "42"}
        )
        other_comment = bot.memory_entry._prepare_memory_entry(
            {"rpid": "o", "text": "别人的评论", "source": "bilibili", "user_id": "99"}
        )
        video = bot.memory_entry._prepare_memory_entry(
            {
                "rpid": "v",
                "text": "公开视频内容",
                "source": "proactive",
                "user_id": "self",
                "memory_type": "video",
            }
        )

        self.assertTrue(bot.memory_recall._memory_visible_to(comment, "bili_comment", "42"))
        self.assertTrue(bot.memory_recall._memory_visible_to(video, "bili_comment", "42"))
        self.assertFalse(bot.memory_recall._memory_visible_to(dm, "bili_comment", "42"))
        self.assertTrue(bot.memory_recall._memory_visible_to(dm, "bili_dm", "42"))
        self.assertTrue(bot.memory_recall._memory_visible_to(comment, "bili_dm", "42"))
        self.assertFalse(bot.memory_recall._memory_visible_to(other_comment, "bili_dm", "42"))

    def test_profile_facts_and_impressions_are_scoped(self):
        bot = self.bot([])
        bot.user_profiles.update(
            "42",
            username="tester",
            impression="公开印象",
            new_facts=["喜欢公开动画"],
            source_scope="bili_comment",
        )
        bot.user_profiles.update(
            "42",
            impression="私信印象",
            new_facts=["私信里的秘密"],
            video_ref={
                "bvid": "BV1PRIVATE",
                "title": "私信分享的视频",
                "relation": "about_user",
            },
            source_scope="bili_dm",
        )
        bot.user_profiles.update(
            "42",
            impression="直播印象",
            new_facts=["直播互动细节"],
            source_scope="bili_live",
        )

        comment = bot.user_profiles.context("42", "bili_comment")
        private = bot.user_profiles.context("42", "bili_dm")
        live = bot.user_profiles.context("42", "bili_live")
        self.assertIn("公开印象", comment)
        self.assertIn("喜欢公开动画", comment)
        self.assertNotIn("私信里的秘密", comment)
        self.assertNotIn("私信分享的视频", comment)
        self.assertNotIn("直播互动细节", comment)
        self.assertIn("私信印象", private)
        self.assertIn("喜欢公开动画", private)
        self.assertIn("私信分享的视频", private)
        self.assertNotIn("直播互动细节", private)
        self.assertIn("直播印象", live)
        self.assertNotIn("私信里的秘密", live)

    async def test_single_delete_invalidates_derived_summary_and_vector_rows(self):
        target = {
            "rpid": "live-raw-1",
            "text": "用户在直播里说过一条稍后应被删除的话",
            "time": "2026-08-20 20:00",
            "source": "bilibili_live",
            "scope": "bili_live",
            "thread_id": "live:room-1:session-1",
            "user_id": "42",
            "memory_type": "live",
            "embedding": [0.1, 0.2],
        }
        derived = {
            "rpid": "compressed-live-42",
            "text": "[记忆压缩] 包含那条稍后应被删除的话",
            "time": "2026-08-20 20:05",
            "source": "bilibili_live",
            "scope": "bili_live",
            "thread_id": "compressed:bili_live",
            "user_id": "42",
            "memory_type": "user_summary",
            "summary_kind": "user",
            "derived_from_rpids": ["live-raw-1"],
            "embedding": [0.2, 0.3],
        }
        unrelated = {
            "rpid": "live-keep-99",
            "text": "另一位用户的独立直播记忆",
            "time": "2026-08-20 20:06",
            "source": "bilibili_live",
            "scope": "bili_live",
            "thread_id": "live:room-1:session-1",
            "user_id": "99",
            "memory_type": "live",
            "embedding": [0.3, 0.4],
        }
        Path(self.config.USER_PROFILE_FILE).write_text(
            json.dumps(
                {
                    "42": {
                        "live": {
                            "memory_refs": [
                                "live-raw-1",
                                "compressed-live-42",
                                "keep-ref",
                            ]
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        bot = self.bot([target, derived, unrelated])
        await bot.memory_startup._initialize_unified_memory()
        self.assertEqual(
            await self.layers.db.fetch_value(
                "SELECT COUNT(*) FROM memory_vectors", default=0
            ),
            3,
        )

        report = await bot.memory_writer._delete_memory_by_rpid("live-raw-1")

        self.assertTrue(report["found"])
        self.assertEqual(report["deleted_count"], 2)
        self.assertEqual(report["invalidated_summary_count"], 1)
        self.assertEqual(report["profile_memory_refs_removed"], 2)
        self.assertEqual(
            {item["rpid"] for item in bot._memory}, {"live-keep-99"}
        )
        self.assertEqual(
            {item["rpid"] for item in await self.layers.memories.load_legacy()},
            {"live-keep-99"},
        )
        self.assertEqual(
            await self.layers.db.fetch_value(
                "SELECT COUNT(*) FROM memory_vectors", default=0
            ),
            1,
        )
        # memory.json 不再每改一次就整份重写（B4d-3）：sqlite 正常时只在启动和每晚清算后导出
        profiles = json.loads(
            Path(self.config.USER_PROFILE_FILE).read_text(encoding="utf-8")
        )
        self.assertEqual(profiles["42"]["live"]["memory_refs"], ["keep-ref"])

    async def test_single_delete_conservatively_invalidates_legacy_summary(self):
        target = {
            "rpid": "comment-raw-1",
            "text": "旧评论原文",
            "source": "bilibili",
            "scope": "bili_comment",
            "thread_id": "thread-1",
            "oid": "100",
            "user_id": "42",
            "memory_type": "chat",
        }
        legacy_summary = {
            "rpid": "thread_compressed_old",
            "text": "[评论线总结] 旧评论原文的摘要",
            "source": "bilibili",
            "scope": "bili_comment",
            "thread_id": "thread-1",
            "oid": "100",
            "user_id": "42",
            "memory_type": "chat",
        }
        independent = {
            "rpid": "comment-keep-2",
            "text": "同一视频下另一条独立评论线",
            "source": "bilibili",
            "scope": "bili_comment",
            "thread_id": "thread-2",
            "oid": "200",
            "user_id": "99",
            "memory_type": "chat",
        }
        other_thread_summary = {
            "rpid": "thread_compressed_other",
            "text": "[评论线总结] 同一用户在其他评论线的独立摘要",
            "source": "bilibili",
            "scope": "bili_comment",
            "thread_id": "thread-3",
            "oid": "300",
            "user_id": "42",
            "memory_type": "chat",
        }
        bot = self.bot(
            [target, legacy_summary, independent, other_thread_summary]
        )
        await bot.memory_startup._initialize_unified_memory()

        report = await bot.memory_writer._delete_memory_by_rpid("comment-raw-1")

        self.assertEqual(
            set(report["removed_rpids"]),
            {"comment-raw-1", "thread_compressed_old"},
        )
        self.assertEqual(
            {item["rpid"] for item in bot._memory},
            {"comment-keep-2", "thread_compressed_other"},
        )

    async def test_single_delete_missing_id_is_a_noop(self):
        bot = self.bot(
            [{"rpid": "keep", "text": "保留", "source": "bilibili"}]
        )
        await bot.memory_startup._initialize_unified_memory()

        report = await bot.memory_writer._delete_memory_by_rpid("missing")

        self.assertFalse(report["found"])
        self.assertEqual({item["rpid"] for item in bot._memory}, {"keep"})

    def test_derived_memory_id_is_stable_and_order_independent(self):
        bot = self.bot([])
        first = bot.memory_entry._derived_memory_id(
            "compressed", [{"rpid": "a"}, {"rpid": "b"}]
        )
        second = bot.memory_entry._derived_memory_id(
            "compressed", [{"rpid": "b"}, {"rpid": "a"}]
        )
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("compressed_"))

    def test_video_memory_recall_weight_decays_by_age(self):
        bot = self.bot([])
        bot.config = {
            "VIDEO_MEMORY_DETAIL_DAYS": 15,
            "VIDEO_MEMORY_FADE_DAYS": 90,
        }
        now = datetime.now()
        detail = bot.memory_entry._prepare_memory_entry(
            {
                "rpid": "detail-video",
                "text": "近期视频",
                "memory_type": "video",
                "time": (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M"),
            }
        )
        long_term = bot.memory_entry._prepare_memory_entry(
            {
                "rpid": "long-video",
                "text": "较早视频",
                "memory_type": "video",
                "time": (now - timedelta(days=20)).strftime("%Y-%m-%d %H:%M"),
            }
        )
        faded = bot.memory_entry._prepare_memory_entry(
            {
                "rpid": "faded-video",
                "text": "很久以前的视频",
                "memory_type": "video",
                "time": (now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M"),
            }
        )

        self.assertEqual(bot.memory_entry._memory_recall_weight(detail), 1.0)
        self.assertEqual(bot.memory_entry._memory_recall_weight(long_term), 0.68)
        self.assertEqual(bot.memory_entry._memory_recall_weight(faded), 0.52)

    async def test_seen_video_migration_survives_capped_activity_log(self):
        entries = [
            {
                "bvid": f"BV{index:010d}",
                "title": f"视频{index}",
                "time": "2026-01-01 12:00",
            }
            for index in range(200)
        ]
        bot = self.bot([])
        bot.json_store._save_json(self.config.WATCH_LOG_FILE, entries)

        self.assertEqual(await bot.memory_startup._initialize_seen_videos(), 200)
        bot.json_store._save_json(self.config.WATCH_LOG_FILE, entries[-150:])

        seen = await bot.seen_videos.bvids()
        self.assertIn("BV0000000000", seen)
        self.assertEqual(await self.layers.seen_videos.count(), 200)

    async def test_new_share_card_is_not_a_watch_record(self):
        bot = self.bot([])

        async def no_embedding(_text):
            return None

        bot._get_embedding = no_embedding
        await bot.memory_writer._save_self_memory_record(
            "group_share:BV1SHARE0001",
            "[2026-10-25 12:00] 群聊有人分享了B站视频《分享》，UP:某人，简介:简介",
            memory_type="video",
            extra={"bvid": "BV1SHARE0001", "content_analyzed": False},
        )
        self.assertEqual(await bot.memory_startup._initialize_seen_videos(), 0)
        self.assertFalse(await bot.seen_videos.has_seen("BV1SHARE0001"))

    async def test_old_share_cards_lose_their_false_seen_marks(self):
        def card(bvid):
            return {
                "rpid": f"group_share:{bvid}_1", "thread_id": f"group_share:{bvid}",
                "text": f"[2026-10-20 12:00] 群聊有人分享了B站视频《{bvid}》",
                "time": "2026-10-20 12:00", "source": "bilibili",
                "memory_type": "video", "bvid": bvid, "user_id": "self",
            }

        watched = {
            "rpid": "qq_share:BV1REALWATCH_1", "thread_id": "qq_share:BV1REALWATCH",
            "text": "视频摘要《真看过》", "time": "2026-10-20 12:05",
            "source": "bilibili", "memory_type": "video",
            "bvid": "BV1REALWATCH", "user_id": "self",
        }
        bot = self.bot([card("BV1ONLYCARD0"), card("BV1REALWATCH"), card("BV1COPIED000"), watched])
        # An earlier plugin version imported every card into the permanent ledger,
        # and a cache hit copied one card into the video cache as a summary.
        await self.layers.seen_videos.import_many(
            [{"bvid": b, "source": "bilibili"} for b in ("BV1ONLYCARD0", "BV1REALWATCH")]
        )
        bot.json_store._save_json(self.config.VIDEO_MEMORY_FILE, {"BV1COPIED000": {
            "bvid": "BV1COPIED000", "source": "semantic_memory",
            "summary": "[2026-10-20 12:00] 群聊有人分享了B站视频《BV1COPIED000》",
            "time": "2026-10-20 12:00",
        }})
        bot.json_store._save_json(self.config.SEEN_VIDEOS_FILE, {"BV1COPIED000": {
            "bvid": "BV1COPIED000", "source": "tool_watch",
        }})

        await bot.memory_startup._initialize_seen_videos()

        self.assertFalse(await bot.seen_videos.has_seen("BV1ONLYCARD0"))
        self.assertFalse(await bot.seen_videos.has_seen("BV1COPIED000"))
        self.assertTrue(await bot.seen_videos.has_seen("BV1REALWATCH"))
        self.assertNotIn("BV1COPIED000", bot.json_store._load_json(self.config.VIDEO_MEMORY_FILE, {}))
        cards = [m for m in bot._memory if str(m.get("thread_id")).startswith("group_share:")]
        self.assertTrue(all(m.get("content_analyzed") is False for m in cards))
        reloaded = await self.layers.memories.load_legacy()
        self.assertTrue(all(
            m.get("content_analyzed") is False
            for m in reloaded if str(m.get("thread_id")).startswith("group_share:")
        ))

    async def test_video_experience_is_vectorized_and_recalled(self):
        bot = self.bot([])

        async def embedding(_text):
            return [1.0, 0.0]

        bot._get_embedding = embedding
        bot._cosine_similarity = lambda left, right: sum(
            a * b for a, b in zip(left, right)
        )
        await bot.memory_writer._save_self_memory_record(
            "proactive_watch",
            "Bot看了《守塔人》，喜欢克制的人物关系，想继续找人物解析。",
            memory_type="video",
            extra={
                "bvid": "BV1VECTOR001",
                "video_title": "守塔人",
                "owner_mid": "100",
                "owner_name": "测试UP",
                "score": 8.6,
                "score_reason": "喜欢克制叙事",
                "preference_signals": [{
                    "type": "work", "value": "守塔人", "polarity": "like",
                    "strength": 0.8, "evidence": "人物关系",
                }],
                "search_keywords": ["守塔人 人物解析"],
            },
        )

        self.assertEqual(list(bot._memory[0]["embedding"]), [1.0, 0.0])
        recalled = await bot.memory_recall._search_memories(
            "守塔人角色关系", memory_types={"video"}, score_threshold=0.5, reader_scope="proactive"
        )
        self.assertEqual(len(recalled), 1)
        self.assertIn("守塔人", recalled[0])


if __name__ == "__main__":
    unittest.main()
