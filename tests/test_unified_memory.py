import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core.layered_runtime import LayeredRuntime


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _load_memory_module(temp_dir):
    package_name = "unified_memory_test_core"
    package = sys.modules.setdefault(package_name, types.ModuleType(package_name))
    package.__path__ = [str(ROOT / "core")]

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

    module_name = f"{package_name}.memory"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "core" / "memory.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    affection_name = f"{package_name}.affection"
    affection_spec = importlib.util.spec_from_file_location(
        affection_name, ROOT / "core" / "affection.py"
    )
    affection = importlib.util.module_from_spec(affection_spec)
    sys.modules[affection_name] = affection
    affection_spec.loader.exec_module(affection)
    return module, affection, config


class _MemoryBotBase:
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
        self.module, self.affection, self.config = _load_memory_module(
            self.temp_dir.name
        )
        self.layers = LayeredRuntime(
            {"DEDE_USER_ID": "bot-1"}, Path(self.temp_dir.name) / "bilibot.sqlite3"
        )
        await self.layers.open()

        memory_mixin = self.module.MemoryMixin

        class Bot(memory_mixin, self.affection.AffectionMixin, _MemoryBotBase):
            pass

        self.bot_type = Bot

    async def asyncTearDown(self):
        await self.layers.close()
        self.temp_dir.cleanup()

    def bot(self, records):
        bot = self.bot_type()
        bot._memory = list(records)
        bot.layered_runtime = self.layers
        bot._memory_write_lock = asyncio.Lock()
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
        self.assertTrue(await bot._initialize_unified_memory())
        await bot._save_memory_entry(
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
        await restarted._initialize_unified_memory()
        self.assertEqual(
            {item["rpid"] for item in restarted._memory}, {"legacy-1", "new-2"}
        )
        self.assertEqual(
            next(item for item in restarted._memory if item["rpid"] == "new-2")["scope"],
            "bili_dm",
        )

    def test_channel_visibility_blocks_cross_user_and_cross_scope_recall(self):
        bot = self.bot([])
        comment = bot._prepare_memory_entry(
            {"rpid": "c", "text": "公开评论", "source": "bilibili", "user_id": "42"}
        )
        dm = bot._prepare_memory_entry(
            {"rpid": "d", "text": "私信秘密", "source": "bilibili_private", "user_id": "42"}
        )
        other_comment = bot._prepare_memory_entry(
            {"rpid": "o", "text": "别人的评论", "source": "bilibili", "user_id": "99"}
        )
        video = bot._prepare_memory_entry(
            {
                "rpid": "v",
                "text": "公开视频内容",
                "source": "proactive",
                "user_id": "self",
                "memory_type": "video",
            }
        )

        self.assertTrue(bot._memory_visible_to(comment, "bili_comment", "42"))
        self.assertTrue(bot._memory_visible_to(video, "bili_comment", "42"))
        self.assertFalse(bot._memory_visible_to(dm, "bili_comment", "42"))
        self.assertTrue(bot._memory_visible_to(dm, "bili_dm", "42"))
        self.assertTrue(bot._memory_visible_to(comment, "bili_dm", "42"))
        self.assertFalse(bot._memory_visible_to(other_comment, "bili_dm", "42"))

    def test_profile_facts_and_impressions_are_scoped(self):
        bot = self.bot([])
        bot._update_user_profile(
            "42",
            username="tester",
            impression="公开印象",
            new_facts=["喜欢公开动画"],
            source_scope="bili_comment",
        )
        bot._update_user_profile(
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
        bot._update_user_profile(
            "42",
            impression="直播印象",
            new_facts=["直播互动细节"],
            source_scope="bili_live",
        )

        comment = bot._get_user_profile_context("42", "bili_comment")
        private = bot._get_user_profile_context("42", "bili_dm")
        live = bot._get_user_profile_context("42", "bili_live")
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
        await bot._initialize_unified_memory()
        self.assertEqual(
            await self.layers.db.fetch_value(
                "SELECT COUNT(*) FROM memory_vectors", default=0
            ),
            3,
        )

        report = await bot._delete_memory_by_rpid("live-raw-1")

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
        backup = json.loads(
            Path(self.config.MEMORY_FILE).read_text(encoding="utf-8")
        )
        self.assertEqual({item["rpid"] for item in backup}, {"live-keep-99"})
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
        await bot._initialize_unified_memory()

        report = await bot._delete_memory_by_rpid("comment-raw-1")

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
        await bot._initialize_unified_memory()

        report = await bot._delete_memory_by_rpid("missing")

        self.assertFalse(report["found"])
        self.assertEqual({item["rpid"] for item in bot._memory}, {"keep"})

    def test_derived_memory_id_is_stable_and_order_independent(self):
        bot = self.bot([])
        first = bot._derived_memory_id(
            "compressed", [{"rpid": "a"}, {"rpid": "b"}]
        )
        second = bot._derived_memory_id(
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
        detail = bot._prepare_memory_entry(
            {
                "rpid": "detail-video",
                "text": "近期视频",
                "memory_type": "video",
                "time": (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M"),
            }
        )
        long_term = bot._prepare_memory_entry(
            {
                "rpid": "long-video",
                "text": "较早视频",
                "memory_type": "video",
                "time": (now - timedelta(days=20)).strftime("%Y-%m-%d %H:%M"),
            }
        )
        faded = bot._prepare_memory_entry(
            {
                "rpid": "faded-video",
                "text": "很久以前的视频",
                "memory_type": "video",
                "time": (now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M"),
            }
        )

        self.assertEqual(bot._memory_recall_weight(detail), 1.0)
        self.assertEqual(bot._memory_recall_weight(long_term), 0.68)
        self.assertEqual(bot._memory_recall_weight(faded), 0.52)

    async def test_seen_video_migration_survives_capped_activity_log(self):
        entries = [
            {
                "bvid": f"BV{index:010d}",
                "title": f"视频{index}",
                "time": "2026-01-01 12:00",
            }
            for index in range(205)
        ]
        bot = self.bot([])
        bot._save_json(self.config.WATCH_LOG_FILE, entries)

        self.assertEqual(await bot._initialize_seen_videos(), 205)
        bot._save_json(self.config.WATCH_LOG_FILE, entries[-200:])

        seen = await bot._seen_video_bvids()
        self.assertIn("BV0000000000", seen)
        self.assertEqual(await self.layers.seen_videos.count(), 205)

    async def test_video_experience_is_vectorized_and_recalled(self):
        bot = self.bot([])

        async def embedding(_text):
            return [1.0, 0.0]

        bot._get_embedding = embedding
        bot._cosine_similarity = lambda left, right: sum(
            a * b for a, b in zip(left, right)
        )
        await bot._save_self_memory_record(
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

        self.assertEqual(bot._memory[0]["embedding"], [1.0, 0.0])
        recalled = await bot._search_memories(
            "守塔人角色关系", memory_types={"video"}, score_threshold=0.5
        )
        self.assertEqual(len(recalled), 1)
        self.assertIn("守塔人", recalled[0])


if __name__ == "__main__":
    unittest.main()
