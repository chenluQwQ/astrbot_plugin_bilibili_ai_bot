"""B4a 视频记忆：一句话印象、淡忘只留 BV 号和印象、低分 30 天后删、旧记忆每晚补印象。"""
import asyncio
import json
import types
import unittest
from activity_parts import ListLog
from datetime import datetime, timedelta

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  先装上假的 astrbot
from test_video_memory_lifecycle import _Bot as _LifecycleBot, _load_consolidation_module
from core.content_protocol import ContentProtocolError, parse_video_impressions
from core.domain.seen_videos import SeenVideos

NOW = datetime.now()


def _video(rpid, days_ago, **extra):
    return {
        "rpid": rpid, "memory_type": "video", "bvid": f"BV1{rpid}",
        "created_at": (NOW - timedelta(days=days_ago)).timestamp(),
        "level": "recent", "text": f"{rpid} 的完整视频记忆", "video_title": f"{rpid} 的视频",
        **extra,
    }


class _Settings:
    def __init__(self, **values):
        self.values = {"VIDEO_LOW_SCORE_FORGET_BELOW": 5, "VIDEO_LOW_SCORE_FORGET_DAYS": 30,
                       "MEMORY_LOW_SCORE_KEEP_DAYS": 7, "MEMORY_LONG_TERM_KEEP_DAYS": 90, **values}

    def get(self, key):
        return self.values.get(key, "")

    def int(self, key, *, minimum=None, maximum=None):
        value = int(self.values[key])
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value


class _Bot(_LifecycleBot):
    def __init__(self, memories, reply="", **settings):
        super().__init__(memories)
        self.settings = _Settings(**settings)
        self.files = {}
        self.json_store = self
        self.reply = reply
        self.prompts = []
        self.models = self
        self.removed_refs = set()
        self.user_profiles = types.SimpleNamespace(remove_memory_refs=self.removed_refs.update)

    async def find(self, types=None, **_filters):
        return [m for m in self.records if types is None or m.get("memory_type") in types]

    def _load_json(self, path, default):
        return json.loads(json.dumps(self.files.get(path, default)))

    def _save_json(self, path, value):
        self.files[path] = json.loads(json.dumps(value))

    def _video_memory_stage_at(self, memory):
        if memory.get("video_memory_stage") == "faded":
            return "faded"
        return "faded" if NOW.timestamp() - memory["created_at"] >= 90 * 86400 else "detail"

    async def _summary_llm_call(self, prompt, max_tokens=0):
        self.prompts.append(prompt)
        return self.reply(prompt) if callable(self.reply) else self.reply


class VideoMemoryForgettingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_consolidation_module()

    def engine(self, bot):
        return self.module.ConsolidationEngine(bot)

    def test_faded_memory_keeps_only_bv_and_impression(self):
        memories = [
            _video("plain", 100, score=6.0, impression="讲猫咪绝育的科普，讲得很耐心", review="挺实用的。下次再看。"),
            _video("loved", 100, score=9.0, impression="一镜到底的城市夜跑", review="镜头太稳了！看得我也想去跑。"),
        ]
        self.engine(_Bot(memories))._apply_video_lifecycle()
        self.assertEqual(memories[0]["text"], "[看过·已淡忘] BV1plain：讲猫咪绝育的科普，讲得很耐心")
        self.assertEqual(memories[0]["video_summary"], "讲猫咪绝育的科普，讲得很耐心")
        self.assertEqual(memories[1]["text"], "[看过·已淡忘] BV1loved：一镜到底的城市夜跑 当时很喜欢：镜头太稳了！")

    def test_old_memory_without_impression_fades_the_old_way(self):
        memories = [_video("old", 100, video_summary="讲的是一场关于记忆设计的讨论")]
        self.engine(_Bot(memories))._apply_video_lifecycle()
        self.assertIn("《old 的视频》", memories[0]["text"])
        self.assertIn("大概是：讲的是一场关于记忆设计的讨论", memories[0]["text"])

    def test_low_score_videos_go_after_thirty_days_with_a_backup(self):
        memories = [
            _video("low_old", 31, score=4.5),
            _video("low_new", 20, score=3.0),
            _video("five", 60, score=5.0),
            _video("no_score", 60),
            {"rpid": "chat", "memory_type": "chat", "created_at": 0, "score": 1, "text": "闲聊"},
        ]
        bot = _Bot(memories)
        self.assertEqual(self.engine(bot)._forget_low_score_videos(), 1)
        self.assertEqual([m["rpid"] for m in bot.records], ["low_new", "five", "no_score", "chat"])
        self.assertEqual(bot.removed_refs, {"low_old"})
        (backup,) = [v for k, v in bot.files.items() if "memory_forgotten_backup_" in k]
        self.assertEqual([m["rpid"] for m in backup], ["low_old"])

    def test_both_numbers_are_configurable_and_zero_turns_it_off(self):
        memories = [_video("low", 10, score=6.0)]
        bot = _Bot(memories, VIDEO_LOW_SCORE_FORGET_BELOW=7, VIDEO_LOW_SCORE_FORGET_DAYS=7)
        self.assertEqual(self.engine(bot)._forget_low_score_videos(), 1)
        memories = [_video("low", 400, score=1.0)]
        bot = _Bot(memories, VIDEO_LOW_SCORE_FORGET_BELOW=0)
        self.assertEqual(self.engine(bot)._forget_low_score_videos(), 0)

    def test_deleted_low_score_video_still_counts_as_seen(self):
        memories = [_video("low", 31, score=2.0)]
        bot = _Bot(memories)
        seen = SeenVideos(types.SimpleNamespace(
            json_store=bot, layered_runtime=None, memories=bot, memory_entry=bot,
            activity_logs=types.SimpleNamespace(watch=ListLog()),
            processed_keys=types.SimpleNamespace(commented_video=types.SimpleNamespace(all=set)),
            memory_docs=types.SimpleNamespace(
                video_memory=types.SimpleNamespace(read=lambda default: default),
                external_memory=types.SimpleNamespace(read=lambda default: default),
            ),
        ))

        async def scenario():
            await seen.mark("BV1low", {"title": "low 的视频"}, source="proactive")
            self.engine(bot)._forget_low_score_videos()
            return await seen.has_seen("BV1low")

        self.assertTrue(asyncio.run(scenario()))
        self.assertEqual(bot.records, [])

    def test_backfill_writes_impressions_for_due_memories_first(self):
        memories = [
            _video("already", 200, video_memory_stage="faded", video_summary="早就淡忘的"),
            _video("due", 95, review="很好笑"),
            _video("fresh", 10),
            _video("has", 95, impression="已经有了"),
        ]

        def reply(prompt):
            self.assertNotIn("fresh", prompt)
            self.assertNotIn('"has"', prompt)
            self.assertLess(prompt.index('"due"'), prompt.index('"already"'))
            return json.dumps({"items": [
                {"rpid": "due", "impression": "一只猫在弹钢琴，很好笑"},
                {"rpid": "already", "impression": "关于记忆设计的讨论"},
            ]}, ensure_ascii=False)

        bot = _Bot(memories, reply)
        engine = self.engine(bot)
        self.assertEqual(asyncio.run(engine._backfill_video_impressions()), 2)
        engine._apply_video_lifecycle()
        self.assertEqual(memories[0]["text"], "[看过·已淡忘] BV1already：关于记忆设计的讨论")
        self.assertEqual(memories[1]["text"], "[看过·已淡忘] BV1due：一只猫在弹钢琴，很好笑")
        self.assertNotIn("impression", memories[2])

    def test_backfill_is_capped_per_night(self):
        memories = [_video(f"v{i}", 95 + i) for i in range(12)]
        bot = _Bot(memories, lambda prompt: json.dumps({"items": []}))
        asyncio.run(self.engine(bot)._backfill_video_impressions())
        self.assertEqual(bot.prompts[0].count('"rpid": "v'), 10)

    def test_malformed_backfill_changes_nothing(self):
        memories = [_video("due", 95)]
        bot = _Bot(memories, '{"items": [{"rpid": "due", "impression": "a", "extra": 1}]}')
        self.assertEqual(asyncio.run(self.engine(bot)._backfill_video_impressions()), 0)
        self.assertNotIn("impression", memories[0])

    def test_no_model_call_when_nothing_is_due(self):
        bot = _Bot([_video("fresh", 10)], "unused")
        asyncio.run(self.engine(bot)._backfill_video_impressions())
        self.assertEqual(bot.prompts, [])


class VideoImpressionParserTests(unittest.TestCase):
    def test_accepts_known_ids(self):
        raw = json.dumps({"items": [{"rpid": "a", "impression": " 很好看 "}]}, ensure_ascii=False)
        self.assertEqual(parse_video_impressions(raw, ["a", "b"]), {"a": "很好看"})

    def test_rejects_bad_batches(self):
        for raw in (
            '{"items": [{"rpid": "x", "impression": "好"}]}',
            '{"items": [{"rpid": "a", "impression": ""}]}',
            '{"items": [{"rpid": "a", "impression": "好"}, {"rpid": "a", "impression": "坏"}]}',
            json.dumps({"items": [{"rpid": "a", "impression": "长" * 61}]}),
            '{"items": [], "note": 1}',
            "不是JSON",
        ):
            with self.subTest(raw=raw[:30]), self.assertRaises(ContentProtocolError):
                parse_video_impressions(raw, ["a"])


if __name__ == "__main__":
    unittest.main()
