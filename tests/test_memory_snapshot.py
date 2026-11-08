"""记忆：从旧数据样例出发，把存、找、压缩、删、清算、遗忘跑一遍，逐条比对快照。

重点是隔离：同一批记忆，评论、私信、直播、主动、QQ 群、管理员各自能找到哪些。
假的只有模型、向量和时间；sqlite 是真的（临时目录）。

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_memory_snapshot.py
"""
from base_parts import BaseParts
import hashlib
import itertools
import json
import math
import os
import re
import shutil
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

import core.consolidation as consolidation_module
import core.memory_api as memory_api_module
from vision_fakes import vision_on
from core.consolidation import ConsolidationEngine
from core.domain.user_profiles import UserProfiles
from core.domain.affection import Affection
from core.domain.profile_candidates import ProfileCandidates
from core.layered_runtime import LayeredRuntime
from core.memory_api import BiliBotMemoryAPI
from core.storage.json_file import JsonFile

import core.domain.memory as domain_memory_module
import core.domain.seen_videos as seen_videos_module
import core.domain._memory_entry as memory_entry_module
import core.features.memory_compress as memory_compress_module
import core.features.memory_recall as memory_recall_module
import core.features.memory_startup as memory_startup_module
import core.features.memory_writer as memory_writer_module
import core.features.reply_context as reply_context_module
from memory_parts import MemoryParts
from itself import itself

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "legacy_data"
SNAPSHOT_FILE = ROOT / "tests" / "snapshots" / "memory.json"
READERS = ["bili_comment", "bili_dm", "bili_live", "proactive", "qq_group", "qq_private", "admin"]
NOW = datetime(2026, 11, 3, 9, 30)
FILE_KEYS = {
    "MEMORY_FILE": "memory.json",
    "MEMORY_SYNC_STATE_FILE": "memory_sync_state.json",
    "PERMANENT_MEMORY_FILE": "permanent_memory.json",
    "CONSOLIDATION_STATE_FILE": "consolidation_state.json",
    "USER_PROFILE_FILE": "user_profiles.json",
    "SEEN_VIDEOS_FILE": "seen_videos.json",
    "WATCH_LOG_FILE": "watch_log.json",
    "COMMENTED_FILE": "commented_videos.json",
    "VIDEO_MEMORY_FILE": "video_memory.json",
    "EXTERNAL_MEMORY_FILE": "external_memory.json",
}
# 阈值调小，少量数据就能触发压缩。
SMALL_LIMITS = {
    "THREAD_COMPRESS_THRESHOLD": 3,
    "OID_COMPRESS_THRESHOLD": 4,
    "OID_KEEP_RECENT": 2,
    "USER_MEMORY_COMPRESS_THRESHOLD": 3,
    "USER_MEMORY_KEEP_RECENT": 1,
}


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(NOW.year, NOW.month, NOW.day, NOW.hour, NOW.minute)


def _embed(text):
    """按字符散到 24 维再归一化：字面越像越接近，结果固定。"""
    vec = [0.0] * 24
    for ch in str(text):
        vec[int(hashlib.md5(ch.encode("utf-8")).hexdigest(), 16) % 24] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 6) for v in vec]


def _fake_llm(prompt, **_kwargs):
    if '"users"' in prompt:
        ids = re.findall(r'"id":\s*"(\d+)",\s*"user"', prompt)
        return json.dumps({"users": [
            {"id": gid, "impression": "说话爱定约的新朋友", "facts": ["下周还想再聊"], "tags": ["守约"]} for gid in ids
        ]}, ensure_ascii=False)
    if '"items"' in prompt and "rpid" in prompt:
        items = re.findall(r'"rpid":\s*"([^"]+)",\s*"user":\s*"[^"]*",\s*"text":\s*"([^"]*)"', prompt)
        scored = []
        for rpid, text in items:
            score = 9 if "约定" in text else 2 if "哈哈" in text else 5
            scored.append({"rpid": rpid, "importance": score})
        return json.dumps({"items": scored}, ensure_ascii=False)
    if '"summary"' in prompt:
        return json.dumps({"summary": "常来聊猫的人", "tags": ["猫派"], "user_facts": ["养了一只橘猫"]}, ensure_ascii=False)
    return "总结：大家在聊猫和约定"


class Bot(MemoryParts):
    models = itself
    vision = property(vision_on)

    def __init__(self, temp):
        self.config = {}
        self.temp = temp

    def _load_json(self, path, default=None):
        return JsonFile(path).read(default)

    def _save_json(self, path, data):
        JsonFile(path).write(data)

    async def _get_embedding(self, text):
        return _embed(text) if str(text or "").strip() else None

    @staticmethod
    def _cosine_similarity(a, b):
        if len(a) != len(b):
            return 0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0

    async def _llm_call(self, prompt, system_prompt="", max_tokens=300, provider_id=None):
        return _fake_llm(prompt)

    def _is_owner(self, uid):
        return False


def _clean(record):
    out = {k: v for k, v in record.items() if k not in {"_sqlite_id", "embedding", "created_at"}}
    out["has_embedding"] = bool(record.get("embedding"))
    return out


def _by_rpid(records):
    return {str(r.get("rpid")): _clean(r) for r in records}


class MemorySnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name)
        for name in set(FILE_KEYS.values()):
            if (FIXTURES / name).exists():
                shutil.copy(FIXTURES / name, data / name)
        self.stack = ExitStack()
        modules = [memory_entry_module, memory_writer_module, memory_recall_module, memory_startup_module, reply_context_module, consolidation_module, memory_api_module, domain_memory_module, seen_videos_module, memory_compress_module]
        # 永久记忆、记忆同步、清算这些小状态现在经 bot.states 读写，假插件那份按 core.config 的常量找路径
        import core.config as core_config
        for key, name in FILE_KEYS.items():
            if hasattr(core_config, key):
                self.stack.enter_context(patch.object(core_config, key, str(data / name)))
        for module in modules:
            if module is None:
                continue
            for key, name in FILE_KEYS.items():
                if hasattr(module, key):
                    self.stack.enter_context(patch.object(module, key, str(data / name)))
            for key, value in SMALL_LIMITS.items():
                if hasattr(module, key):
                    self.stack.enter_context(patch.object(module, key, value))
            if hasattr(module, "datetime"):
                self.stack.enter_context(patch.object(module, "datetime", _FrozenDatetime))
        counter = itertools.count()
        self.stack.enter_context(patch.object(
            memory_api_module.uuid, "uuid4", lambda: uuid.UUID(int=next(counter))
        ))
        self.layers = LayeredRuntime({"DEDE_USER_ID": "bot-1"}, data / "bilibot.sqlite3")
        await self.layers.open()
        self.data = data

    async def asyncTearDown(self):
        await self.layers.close()
        self.stack.close()
        self.temp.cleanup()

    def _bot(self):
        bot = Bot(self.temp)
        bot.layered_runtime = self.layers
        bot.user_profiles = UserProfiles(JsonFile(self.data / "user_profiles.json"))
        bot.affection = Affection(
            JsonFile(self.data / "affection.json"), lambda uid: False, JsonFile(self.data / "profile_reached.json"),
        )
        bot.affection.set("u7", 25)
        bot.profile_candidates = ProfileCandidates(
            JsonFile(self.data / "profile_candidates.json"), bot.affection.reached_profile_line, lambda uid: False,
        )
        bot._memory = [
            bot.memory_entry._normalize_memory_entry(m)
            for m in bot.json_store._load_json(str(self.data / "memory.json"), [])
            if isinstance(m, dict)
        ]
        import asyncio
        bot._memory_write_lock = asyncio.Lock()
        bot._consolidation = ConsolidationEngine(bot)
        bot.memory_api = BiliBotMemoryAPI(bot)
        return bot

    async def _reads(self, bot, query):
        result = {}
        for reader in READERS:
            raw = await bot.memory_recall._search_memories_raw(query, limit=50, score_threshold=0, reader_scope=reader)
            result[reader] = {
                "search": [[round(s, 4), str(m.get("rpid"))] for s, m in raw],
                "global": await bot.memory_recall._search_global_relevant(query, current_oid="100", limit=8, reader_scope=reader, user_id="u1"),
                "user_semantic": await bot.memory_recall._get_user_semantic_memories("u1", query, reader_scope=reader),
                "bvid": await bot.memory_recall._get_bvid_memories("BV1snapshot01", reader_scope=reader, user_id="u1"),
                "api_search": [r["rpid"] for r in await bot.memory_api.search(query, limit=50, score_threshold=0, reader_scope=reader)],
                "api_recent": [r["rpid"] for r in bot.memory_api.get_recent_memories(hours=24 * 400, limit=100, reader_scope=reader)],
            }
        result["thread_t1"] = [str(m.get("rpid")) for m in await bot.memory_recall._get_thread_memories("t1")]
        result["thread_private"] = [str(m.get("rpid")) for m in await bot.memory_recall._get_thread_memories("private:u1")]
        result["oid_100"] = [str(m.get("rpid")) for m in await bot.memory_recall._get_oid_memories("100", exclude_thread_id="t1")]
        result["recall_user"] = await bot.memory_api.recall_user("u1", query, reader_scope="bili_live")
        return result

    async def _sqlite(self):
        return _by_rpid(await self.layers.memories.load_legacy())

    async def test_matches_snapshot(self):
        bot = self._bot()
        results = {}
        self.assertTrue(await bot.memory_startup._initialize_unified_memory())
        results["after_init"] = {"count": len(bot._memory), "sqlite": sorted(await self._sqlite())}

        # 存：评论区一条线、同评论区的别人、私信、直播、主动看片、动态
        for i in range(5):
            text = ["今天聊猫", "哈哈哈哈", "我们约定周末一起看番", "猫猫好可爱", "哈哈好的"][i]
            await bot.memory_writer._save_memory_record(f"c{i}", "t1", "u1", "小一", text, f"回复{i}", oid=100, bvid="BV1snapshot01", video_title="猫视频")
        for i in range(4):
            await bot.memory_writer._save_memory_record(f"o{i}", f"t-other{i}", f"u{i + 3}", f"路人{i}", f"路人说猫{i}", "嗯嗯", oid=100)
        for i in range(4):
            await bot.memory_writer._save_memory_record(f"d{i}", "private:u1", "u1", "小一", f"私信里的秘密{i} 猫", f"私信回复{i}", source="bilibili_private")
        await bot.memory_api.record_live_event(user_id="u1", username="小一", event_type="danmaku", content="直播间说猫", session_id="s1", event_id="e1")
        await bot.memory_writer._save_self_memory_record("video:BV1snapshot01", "Bot看了视频《猫视频》，很可爱", memory_type="video",
                                           extra={"bvid": "BV1snapshot01", "video_title": "猫视频", "owner_mid": "u9", "owner_name": "猫UP"})
        await bot.memory_writer._save_self_memory_record("dynamic", "Bot发了一条动态：今天看了猫", memory_type="dynamic")
        await bot.memory_api.record("外部插件写的一条猫记忆", user_id="u1", source="qq_private", memory_type="chat")
        results["after_save"] = await self._sqlite()
        results["reads_before_compress"] = await self._reads(bot, "猫 约定")

        # 压缩
        await bot.memory_compress.compress_thread("t1")
        await bot.memory_compress.compress_oid("100")
        await bot.memory_compress.compress_user("u1", "小一", "bili_comment")
        await bot.memory_compress.compress_user("u1", "小一", "bili_dm")
        results["after_compress"] = await self._sqlite()
        results["reads_after_compress"] = await self._reads(bot, "猫 约定")

        # 删：同评论区还留着的一条、旧数据里的私信、直播记忆（画像里有引用）、已经被压缩掉的、不存在的
        live_rpid = next(str(m["rpid"]) for m in bot._memory if m.get("memory_type") == "live" and m.get("user_id") == "u1")
        legacy_dm = next(str(m["rpid"]) for m in bot._memory if str(m.get("thread_id", "")).startswith("private:345170725"))
        results["delete"] = {
            rpid: await bot.memory_writer._delete_memory_by_rpid(rpid)
            for rpid in ("o3", legacy_dm, live_rpid, "c4", "no-such-rpid")
        }

        # 给清算和遗忘准备的：今天新聊的（会被打分、低分丢弃）、半年多以前的长期闲聊（会老化）、
        # 没按当前标准评过的近期闲聊（手动重评会删）
        for i, text in enumerate(["哈哈哈哈哈", "我们约定下周再聊", "普通的一句话"]):
            await bot.memory_writer._save_memory_record(f"n{i}", f"t-new{i}", "u7", "新人", text, "嗯", oid=200)
        await bot.memory_api.record("很久以前的闲聊", user_id="u8", source="bilibili", memory_type="chat", level="long_term",
                                    extra={"time": "2025-01-02 10:00", "promoted_at": "2025-01-02 10:00"})
        await bot.memory_api.record("哈哈哈", user_id="u8", source="bilibili", memory_type="chat", level="recent", importance=5)

        # 回复时攒下的画像候选（真回复走 ReplyAftermath，这里直接记）；u8 没到门槛，记不下
        bot.profile_candidates.note("u7", "bili_comment", "爱约定", ["下周还想再聊"], "新人")
        bot.profile_candidates.note("u8", "bili_comment", "", ["记不下"])
        # 每晚清算、清理 aged、手动重评
        summary = await bot._consolidation.run_daily()
        results["daily"] = [line for line in summary.splitlines() if "耗时" not in line]
        results["after_daily"] = await self._sqlite()
        results["cleanup_aged"] = await bot._consolidation.cleanup_aged()
        cleanup = await bot._consolidation.cleanup_irrelevant()
        results["cleanup_irrelevant"] = {k: v for k, v in cleanup.items() if k != "backup"} if isinstance(cleanup, dict) else cleanup
        results["stats"] = bot.memory_api.stats()
        results["reads_final"] = await self._reads(bot, "猫 约定")

        results["final_sqlite"] = await self._sqlite()
        results["final_json"] = _by_rpid(json.loads((self.data / "memory.json").read_text(encoding="utf-8")))
        results["final_working"] = _by_rpid(bot._memory)
        results["sync_state"] = {
            k: v for k, v in json.loads((self.data / "memory_sync_state.json").read_text(encoding="utf-8")).items()
            if k != "updated_at"
        }
        results["profile_u1"] = bot.user_profiles.get("u1")
        results["profile_u7"] = bot.user_profiles.get("u7")

        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS") or not SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True, default=str), encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(
            json.loads(json.dumps(results, ensure_ascii=False, sort_keys=True, default=str)),
            json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")),
        )


class MemoryIsolationTests(MemorySnapshotTests):
    """私信、QQ 私聊里说的，不能出现在会变成公开内容的地方。"""

    async def test_matches_snapshot(self):  # 只跑下面的隔离检查
        pass

    async def _owner_bot(self):
        bot = self._bot()
        await bot.memory_startup._initialize_unified_memory()
        await bot.memory_writer._save_memory_record("pub", "t-pub", "owner", "主人", "公开说喜欢猫", "好", oid=300)
        await bot.memory_writer._save_memory_record("dm", "private:owner", "owner", "主人", "私信里说喜欢猫的秘密", "嗯", source="bilibili_private")
        await bot.memory_api.record("QQ 私聊说喜欢猫", user_id="owner", source="qq_private", memory_type="chat")
        return bot

    async def test_video_evaluation_reads_only_public_owner_memories(self):
        from core.features._video_evaluate import VideoEvaluate

        bot = await self._owner_bot()
        bot.config = {"OWNER_MID": "owner"}
        context = await VideoEvaluate(bot)._owner_recommendation_context("喜欢猫")
        self.assertIn("公开说喜欢猫", context)
        self.assertNotIn("秘密", context)
        self.assertNotIn("QQ 私聊", context)

    async def test_search_requires_reader_scope(self):
        bot = await self._owner_bot()
        with self.assertRaises(TypeError):
            await bot.memory_recall._search_memories("喜欢猫")

    async def test_thread_and_comment_area_lookups_filter_by_scope(self):
        bot = await self._owner_bot()
        # 故意把一条私信放进公开评论线和评论区（模拟编号撞了），评论读者不该看到它。
        await bot.memory_writer._save_memory_record("leak", "t-pub", "owner", "主人", "私信内容混进来", "嗯",
                                      source="bilibili_private", oid=300)
        public = [m["rpid"] for m in await bot.memory_recall._get_thread_memories("t-pub", "bili_comment")]
        self.assertEqual(public, ["pub"])
        area = [m["rpid"] for m in await bot.memory_recall._get_oid_memories(300, reader_scope="bili_comment")]
        self.assertNotIn("leak", area)
        dm = [m["rpid"] for m in await bot.memory_recall._get_thread_memories("t-pub", "bili_dm")]
        self.assertEqual(sorted(dm), ["leak", "pub"])


if __name__ == "__main__":
    unittest.main()
