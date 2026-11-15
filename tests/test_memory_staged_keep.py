"""按分数分阶段留：视频 15/90/180 天三次筛、聊天和动态 7/14/+90 天、自己的内容天数加倍、番剧按季总结。"""
import asyncio
import json
import unittest
from datetime import datetime, timedelta

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  先装上假的 astrbot
from test_video_memory_forgetting import _Bot as _VideoBot, _video
from test_video_memory_lifecycle import _load_consolidation_module

NOW = datetime.now()
BOT_MID = "999"


def _stamp(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M")


def _chat(rpid, days_ago, importance, *, level="recent", scored=True, **extra):
    record = {
        "rpid": rpid, "memory_type": "chat", "level": level, "importance": importance,
        "time": _stamp(days_ago), "promoted_at": _stamp(days_ago), "text": f"用户说了 {rpid}", **extra,
    }
    if scored:
        record["evaluation_version"] = 2
    return record


def _dynamic(rpid, days_ago, importance, **extra):
    return {
        "rpid": rpid, "memory_type": "dynamic", "level": "recent", "importance": importance,
        "time": _stamp(days_ago), "promoted_at": _stamp(days_ago), "author_mid": "5",
        "scored_by": "reply_model", "text": f"动态 {rpid}", **extra,
    }


def _episode(rpid, days_ago, season_id, score, **extra):
    return {
        "rpid": rpid, "memory_type": "bangumi", "level": "recent", "season_id": season_id,
        "thread_id": f"bangumi:{season_id}", "time": _stamp(days_ago), "score": score,
        "text": f"Bot看了番剧《番{season_id}》第{rpid}话 评分:{score}/10 感想:还行", **extra,
    }


class _Bot(_VideoBot):
    def __init__(self, memories, reply="", **settings):
        super().__init__(memories, reply, DEDE_USER_ID=BOT_MID, **settings)
        self.vision = self

    @staticmethod
    def _is_derived_memory(memory):
        return bool(memory.get("summary_kind") or memory.get("derived_from_rpids"))

    async def _get_embedding(self, text):
        return [0.5, 0.5]


class StagedKeepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_consolidation_module()

    def engine(self, bot):
        return self.module.ConsolidationEngine(bot)

    def kept(self, bot):
        return [m["rpid"] for m in bot.records]

    def test_videos_are_cut_at_15_90_and_180_days(self):
        memories = [
            _video("low_14", 14, score=2.0),      # 低分也至少留 15 天
            _video("low_16", 16, score=4.0),      # 第 15 天删低分
            _video("mid_80", 80, score=6.0),
            _video("mid_91", 91, score=6.0),      # 第 90 天删中分
            _video("good_91", 91, score=7.0),
            _video("good_181", 181, score=7.0),   # 第 180 天只留高分
            _video("high_181", 181, score=8.0),
            _video("unrated_181", 181),           # 没评分的不按分数删，等补评
        ]
        bot = _Bot(memories, VIDEO_LOW_SCORE_FORGET_DAYS=15)
        bot._video_memory_windows = lambda: (15, 180)
        self.assertEqual(self.engine(bot)._forget_low_score_videos(), 3)
        self.assertEqual(self.kept(bot), ["low_14", "mid_80", "good_91", "high_181", "unrated_181"])

    def test_old_video_without_score_field_uses_the_score_in_its_text(self):
        memories = [_video("legacy", 181, text="[2026-01-01 10:00] Bot看了视频《x》 评分:9/10 感想:好")]
        bot = _Bot(memories, VIDEO_LOW_SCORE_FORGET_DAYS=15)
        bot._video_memory_windows = lambda: (15, 180)
        self.assertEqual(self.engine(bot)._forget_low_score_videos(), 0)

    def test_own_videos_stay_twice_as_long(self):
        memories = [_video("mine", 20, score=2.0, owner_mid=BOT_MID), _video("mine_old", 31, score=2.0, owner_mid=BOT_MID)]
        bot = _Bot(memories, VIDEO_LOW_SCORE_FORGET_DAYS=15)
        bot._video_memory_windows = lambda: (15, 180)
        self.engine(bot)._forget_low_score_videos()
        self.assertEqual(self.kept(bot), ["mine"])

    def test_survivors_fade_to_one_line_at_the_fade_day(self):
        memories = [_video("high", 181, score=9.0, impression="一镜到底的城市夜跑"), _video("fresh", 100, score=9.0)]
        bot = _Bot(memories)
        bot._video_memory_windows = lambda: (15, 180)
        self.engine(bot)._apply_video_lifecycle()
        self.assertEqual(memories[0]["video_memory_stage"], "faded")
        self.assertTrue(memories[0]["text"].startswith("[看过·已淡忘] BV1high：一镜到底的城市夜跑"))
        self.assertEqual(memories[1]["video_memory_stage"], "long_term")

    def test_chats_and_dynamics_are_cut_at_7_14_and_104_days(self):
        memories = [
            _chat("low_6", 6, 3),                       # 低分留满 7 天
            _chat("low_8", 8, 3),
            _chat("ok_8", 8, 4),
            _chat("ok_15", 15, 5),                      # 近期满 14 天，不够进长期的删
            _chat("long_80", 80, 6, level="long_term"),
            _chat("long_91", 91, 7, level="long_term"),  # 进长期满 90 天只留高分
            _chat("high_91", 91, 8, level="long_term"),
            _chat("summary_91", 91, 6, level="long_term", summary_kind="thread"),
            _chat("unscored_200", 200, 2, scored=False),  # 没评过分的不按分数删
            _dynamic("dyn_8", 8, 2),
            _dynamic("dyn_15", 15, 6),
            _dynamic("dyn_105", 105, 7),
            _dynamic("dyn_high", 105, 9),
            {**_dynamic("dyn_unscored", 300, 2), "scored_by": ""},
            {"rpid": "person", "memory_type": "user_summary", "level": "long_term", "importance": 4,
             "time": _stamp(400), "promoted_at": _stamp(400), "evaluation_version": 2, "text": "[记忆压缩] 这个人"},
        ]
        bot = _Bot(memories)
        self.assertEqual(self.engine(bot)._forget_by_score(), 6)
        self.assertEqual(self.kept(bot), [
            "low_6", "ok_8", "long_80", "high_91", "unscored_200",
            "dyn_15", "dyn_high", "dyn_unscored", "person",
        ])
        self.assertEqual(len([k for k in bot.files if "memory_forgotten_backup_" in k]), 1)

    def test_chat_and_dynamic_days_follow_config(self):
        memories = [
            _chat("low_8", 8, 3),                        # 低分保留改成 10 天，第 8 天还在
            _chat("low_11", 11, 3),
            _chat("long_40", 40, 7, level="long_term"),  # 只留高分改成 30 天
            _chat("long_20", 20, 7, level="long_term"),
            _dynamic("dyn_45", 45, 7),                   # 动态：14 + 30 天
            _dynamic("dyn_40", 40, 7),
        ]
        bot = _Bot(memories, MEMORY_LOW_SCORE_KEEP_DAYS=10, MEMORY_LONG_TERM_KEEP_DAYS=30)
        self.assertEqual(self.engine(bot)._forget_by_score(), 3)
        self.assertEqual(self.kept(bot), ["low_8", "long_20", "dyn_40"])

    def test_own_content_gets_double_the_days(self):
        memories = [
            _chat("own_low_8", 8, 3, own_content=True),
            _chat("own_low_15", 15, 3, own_content=True),
            _dynamic("own_post", 20, 4, author_mid="", thread_id="dynamic"),
            _dynamic("own_post_old", 29, 4, author_mid=BOT_MID),
        ]
        bot = _Bot(memories)
        self.engine(bot)._forget_by_score()
        self.assertEqual(self.kept(bot), ["own_low_8", "own_post"])

    def test_tonight_only_very_low_dynamics_are_dropped_and_bangumi_is_promoted(self):
        memories = [
            {**_dynamic("junk", 0, 2), "level": "today"},
            {**_dynamic("fine", 0, 3), "level": "today"},
            {**_dynamic("own_junk", 0, 1, own_content=True), "level": "today"},
            {**_episode("1", 0, "7", 3), "level": "today", "importance": 3, "scored_by": "reply_model"},
        ]
        bot = _Bot(memories)
        self.assertEqual(self.engine(bot)._auto_promote_non_chat(), 3)
        self.assertEqual(self.kept(bot), ["fine", "own_junk", "1"])
        self.assertEqual([m["level"] for m in bot.records], ["recent"] * 3)
        self.assertEqual([m["importance"] for m in bot.records], [3, 1, 3])  # 打过的分不被抬到 6


class BacklogScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_consolidation_module()

    def test_old_unscored_dynamics_get_scored_oldest_first_by_the_reply_model(self):
        memories = [
            {**_dynamic(f"d{i}", 300 - i, 6), "scored_by": ""} for i in range(17)
        ] + [_dynamic("already", 400, 9), {**_dynamic("today", 0, 6), "level": "today", "scored_by": ""}]
        calls = []

        class Bot(_Bot):
            async def _generation_llm_call(self, prompt, max_tokens=0):
                calls.append(prompt)
                ids = [f"d{i}" for i in range(15)]
                return json.dumps({"items": [{"rpid": rpid, "importance": 2} for rpid in ids]})

        bot = Bot(memories)
        result = asyncio.run(self.module.ConsolidationEngine(bot)._review_dynamic_backlog())
        self.assertIn("本次评分 15", result)
        self.assertEqual(len(calls), 1)
        self.assertIn('<memories trust="untrusted">', calls[0])
        self.assertEqual(bot.prompts, [])  # 不走总结模型
        scored = [m["rpid"] for m in bot.records if m.get("scored_by") == "reply_model"]
        self.assertEqual(scored, [f"d{i}" for i in range(15)] + ["already"])
        self.assertEqual(len(bot.records), 19)  # 这一步只写分，不删
        # 下一晚按分数删：满 7 天的 2 分动态就删了
        self.assertEqual(self.module.ConsolidationEngine(bot)._forget_by_score(), 15)

    def test_bad_dynamic_scores_change_nothing(self):
        memories = [{**_dynamic("d", 300, 6), "scored_by": ""}]

        class Bot(_Bot):
            async def _generation_llm_call(self, prompt, max_tokens=0):
                return '{"items": [{"rpid": "d", "importance": 11}]}'

        bot = Bot(memories)
        asyncio.run(self.module.ConsolidationEngine(bot)._review_dynamic_backlog())
        self.assertEqual(bot.records[0]["scored_by"], "")
        self.assertEqual(bot.records[0]["importance"], 6)

    def test_unscored_videos_get_scored_by_the_reply_model(self):
        memories = [_video("old", 300), _video("new", 20), _video("rated", 400, score=7.0),
                    _video("legacy_text", 500, text="Bot看了视频《x》 评分:6/10 感想:还行")]
        calls = []

        class Bot(_Bot):
            async def _generation_llm_call(self, prompt, max_tokens=0):
                calls.append(prompt)
                return json.dumps({"items": [{"rpid": "old", "importance": 3}, {"rpid": "new", "importance": 9}]})

        bot = Bot(memories)
        result = asyncio.run(self.module.ConsolidationEngine(bot)._review_video_backlog())
        self.assertIn("本次评分 2", result)
        self.assertEqual(len(calls), 1)
        self.assertIn('"rpid": "old"', calls[0].replace("\n", ""))
        self.assertNotIn("rated", calls[0])
        self.assertNotIn("legacy_text", calls[0])
        self.assertEqual([m.get("score") for m in bot.records], [3, 9, 7.0, None])

    def test_unscored_recent_chats_join_the_nightly_backlog(self):
        memories = [
            _chat("recent_old", 30, 5, scored=False),
            _chat("long_old", 200, 5, level="long_term", scored=False),
            _chat("today", 0, 5, level="today", scored=False),
            _chat("done", 30, 5),
        ]
        engine = self.module.ConsolidationEngine(_Bot(memories))
        self.assertEqual([m["rpid"] for m in engine._long_term_review_candidates()], ["long_old", "recent_old"])


class BangumiSeasonSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_consolidation_module()

    def run_engine(self, bot):
        return asyncio.run(self.module.ConsolidationEngine(bot)._summarize_bangumi_seasons())

    def test_each_season_gets_its_own_summary_in_series_order(self):
        memories = [
            _episode("s2e1", 20, "12", 9, series_key="11", series_index=1, season_title="番 第二季"),
            _episode("s1e1", 40, "11", 4, series_key="11", series_index=0, season_title="番 第一季"),
            _episode("s1e2", 39, "11", 4, series_key="11", series_index=0, season_title="番 第一季"),
            _episode("new", 3, "12", 9, series_key="11", series_index=1, season_title="番 第二季"),
        ]
        bot = _Bot(memories, lambda prompt: json.dumps({"summary": "第二季很燃" if "第二季" in prompt else "第一季平平"}, ensure_ascii=False))
        self.assertEqual(self.run_engine(bot), 2)
        self.assertEqual(len(bot.prompts), 2)  # 一季一次，不把两季揉成一段
        self.assertEqual([m["rpid"] for m in bot.records], ["new", "bangumi_series_11"])
        series = bot.records[1]
        self.assertEqual(series["level"], "long_term")
        self.assertEqual([s["season_id"] for s in series["seasons"]], ["11", "12"])
        self.assertEqual([s["episodes"] for s in series["seasons"]], [2, 1])
        self.assertEqual(series["text"], "[番剧总结] 《番 第一季》（4.0/10，看了2集）：第一季平平\n《番 第二季》（9.0/10，看了1集）：第二季很燃")
        self.assertEqual(series["embedding"], [0.5, 0.5])
        # 低分少写、高分多写
        low_prompt = next(p for p in bot.prompts if "第一季" in p)
        high_prompt = next(p for p in bot.prompts if "第二季" in p)
        self.assertIn("写30字以内", low_prompt)
        self.assertIn("写160字以内", high_prompt)
        self.assertIn('<bangumi_episodes trust="untrusted">', low_prompt)

    def test_later_episodes_are_merged_into_the_same_season(self):
        existing = {
            "rpid": "bangumi_series_11", "memory_type": "bangumi", "summary_kind": "bangumi_series", "level": "long_term",
            "time": _stamp(30), "text": "旧", "seasons": [
                {"season_id": "11", "title": "番 第一季", "index": 0, "score": 6.0, "episodes": 2, "summary": "前两集铺垫"},
            ],
        }
        memories = [existing, _episode("e3", 15, "11", 9, series_key="11", series_index=0, season_title="番 第一季")]
        bot = _Bot(memories, json.dumps({"summary": "铺垫之后第三集爆发"}, ensure_ascii=False))
        self.assertEqual(self.run_engine(bot), 1)
        (series,) = bot.records
        self.assertEqual(series["seasons"], [
            {"season_id": "11", "title": "番 第一季", "index": 0, "score": 7.0, "episodes": 3, "summary": "铺垫之后第三集爆发"},
        ])
        self.assertIn("前两集铺垫", bot.prompts[0])
        self.assertEqual(series["time"], existing["time"])

    def test_bad_summary_leaves_the_episodes_for_tomorrow(self):
        memories = [_episode("e1", 20, "11", 2)]
        bot = _Bot(memories, json.dumps({"summary": "字" * 31}, ensure_ascii=False))  # 低分只许 30 字
        self.assertEqual(self.run_engine(bot), 0)
        self.assertEqual([m["rpid"] for m in bot.records], ["e1"])
        self.assertEqual(bot.files, {})

    def test_low_score_episodes_are_never_dropped_by_score(self):
        memories = [{**_episode("e1", 200, "11", 1), "importance": 1, "scored_by": "reply_model"}]
        bot = _Bot(memories, "not json")
        self.assertEqual(self.module.ConsolidationEngine(bot)._forget_by_score(), 0)
        self.assertEqual([m["rpid"] for m in bot.records], ["e1"])


if __name__ == "__main__":
    unittest.main()
