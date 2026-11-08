from base_parts import BaseParts
import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _install_astrbot_stub():
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    if "astrbot.api.star" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.logger = _Logger()
    star = types.ModuleType("astrbot.api.star")
    data_dir = Path(tempfile.mkdtemp(prefix="bilibot-personality-test-"))
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda _name: data_dir)
    api.star = star
    astrbot.api = api
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": star,
    })


_install_astrbot_stub()

from core.config import (  # noqa: E402
    DAILY_SUMMARY_FILE,
    PERSONALITY_FILE,
    PREFERENCE_STATE_FILE,
    WEEKLY_SUMMARY_FILE,
)
from core.domain.personality import Personality, parse_weekly_evolution  # noqa: E402
from core.features.personality_evolution import PersonalityEvolution, qualified_feedback  # noqa: E402
from core.settings import SettingsAccessor  # noqa: E402
from itself import itself  # noqa: E402


class _ProbeFile:
    def __init__(self, files, path):
        self._files, self._path = files, path

    def read(self, default=None):
        return self._files.get(self._path, default)

    def write(self, value):
        self._files[self._path] = json.loads(json.dumps(value, ensure_ascii=False))


class PersonalityProbe(BaseParts):
    models = itself
    web_search = itself
    settings = SettingsAccessor()

    def __init__(self, *, daily_days=0, responses=None, config=None):
        self.config = {
            "ENABLE_PERSONALITY_EVOLUTION": True,
            "ENABLE_MEME_LEARNING": False,
            "ENABLE_WEB_SEARCH": False,
            "EVOLVE_MIN_DATA_DAYS": 3,
            **(config or {}),
        }
        self.files = {
            PERSONALITY_FILE: {},
            DAILY_SUMMARY_FILE: self._daily_records(daily_days),
            WEEKLY_SUMMARY_FILE: [],
            PREFERENCE_STATE_FILE: {"current": []},
        }
        self.personality = Personality(_ProbeFile(self.files, PERSONALITY_FILE))
        self.personality_evolution = PersonalityEvolution(self)
        self.responses = list(responses or [])
        self.llm_calls = 0
        self.web_queries = []

    @staticmethod
    def _daily_records(count):
        records = []
        for offset in range(count):
            day = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
            records.append({
                "date": day,
                "summary": f"{day} 看了一段灯塔短片，觉得结尾很安静。",
                "structured": {
                    "counts": {"videos": 1, "conversations": 1},
                    "video_highlights": [{
                        "title": "灯塔", "score": 8.5, "mood": "平静",
                    }],
                    "comment_highlights": ["这个结尾绝绝子", "雾号真的绝绝子"],
                },
            })
        return records

    def _load_json(self, path, default=None):
        return self.files.get(path, default)

    def _save_json(self, path, value):
        self.files[path] = json.loads(json.dumps(value, ensure_ascii=False))

    @staticmethod
    def _repair_llm_json(text):
        return str(text or "").strip()

    async def _get_system_prompt(self):
        return "核心测试人设"

    async def _llm_call(self, _prompt, **_kwargs):
        self.llm_calls += 1
        return self.responses.pop(0) if self.responses else None

    async def _web_search(self, query):
        self.web_queries.append(query)
        return "这是已核验的梗解释"

    async def _get_embedding(self, _text):
        return None


def _evolution_reply(
    *, decision="update", state="最近比较安静", reflections=None
):
    return json.dumps({
        "decision": decision,
        "dynamic_block": {
            "recent_state": state,
            "recent_preferences": ["灯塔题材"],
            "recent_thoughts": ["留白比解释完整更有余味"],
            "recent_reflections": list(reflections or []),
        },
        "reflection": "这周更留意具体片段了",
        "meme_candidates": [],
    }, ensure_ascii=False)


class PersonalityEvolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_switch_never_reads_data_or_calls_model(self):
        probe = PersonalityProbe(
            daily_days=7, config={"ENABLE_PERSONALITY_EVOLUTION": False}
        )

        result = await probe.personality_evolution.maybe_evolve(force=True)

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(probe.llm_calls, 0)

    async def test_readiness_requires_three_distinct_nonempty_daily_summaries(self):
        probe = PersonalityProbe(daily_days=2)
        waiting = await probe.personality_evolution.readiness()
        probe.files[DAILY_SUMMARY_FILE].append({
            "date": (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"),
            "summary": "第三天",
            "structured": {"counts": {"videos": 0, "conversations": 0}},
        })
        still_waiting = await probe.personality_evolution.readiness()
        probe.files[DAILY_SUMMARY_FILE][-1]["structured"]["counts"]["videos"] = 1
        ready = await probe.personality_evolution.readiness()

        self.assertFalse(waiting["ready"])
        self.assertFalse(still_waiting["ready"])
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["days"], 3)

    async def test_readiness_can_use_privacy_filtered_structures_derived_from_logs(self):
        probe = PersonalityProbe(daily_days=0)
        probe.activity_digest = types.SimpleNamespace()
        probe.activity_digest._collect_weekly_data = lambda days=7: {"days": days}
        probe.activity_digest._group_activity_by_day = lambda _data: {
            "2026-08-18": {}, "2026-08-19": {}, "2026-08-20": {},
        }
        probe.activity_digest._build_structured_activity_summary = lambda _data, **kwargs: {
            "period": kwargs["period_key"],
            "counts": {"videos": 1},
            "conversation_highlights": [],
        }

        readiness = await probe.personality_evolution.readiness()

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["days"], 3)
        self.assertTrue(all(
            item["derived_from_activity_logs"] for item in readiness["daily"]
        ))

    async def test_weekly_update_is_bounded_versioned_and_only_runs_once(self):
        probe = PersonalityProbe(daily_days=3, responses=[_evolution_reply()])

        first = await probe.personality_evolution.maybe_evolve(force=True)
        second = await probe.personality_evolution.maybe_evolve(force=True)
        state = probe.files[PERSONALITY_FILE]

        self.assertEqual(first["status"], "updated")
        self.assertEqual(second["status"], "already_done")
        self.assertEqual(probe.llm_calls, 1)
        self.assertEqual(state["version"], 1)
        self.assertEqual(len(state["history"]), 1)
        self.assertEqual(state["dynamic_block"]["recent_state"], "最近比较安静")
        self.assertEqual(state["evolved_traits"], [])

    async def test_failed_weekly_attempt_is_not_repeated(self):
        probe = PersonalityProbe(daily_days=3, responses=["not-json"])

        first = await probe.personality_evolution.maybe_evolve(force=True)
        second = await probe.personality_evolution.maybe_evolve(force=True)

        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "already_attempted")
        self.assertEqual(probe.llm_calls, 1)

    async def test_no_model_reply_keeps_this_weeks_chance(self):
        probe = PersonalityProbe(
            daily_days=3, responses=[None, _evolution_reply()],
            config={"EVOLVE_HOUR": datetime.now().hour},
        )

        first = await probe.personality_evolution.maybe_evolve(force=True)
        state = probe.files[PERSONALITY_FILE]
        self.assertEqual(first["status"], "model_unavailable")
        self.assertNotEqual(state.get("last_attempt_week"), state["model_retry_week"])

        # 还没到重试时间：定时检查不再叫模型
        self.assertEqual((await probe.personality_evolution.maybe_evolve())["status"], "waiting_for_model")
        self.assertEqual(probe.llm_calls, 1)

    async def test_retry_runs_once_a_day_and_counts_for_the_missed_week(self):
        now = datetime.now()
        other_day = (now.weekday() + 1) % 7
        probe = PersonalityProbe(
            daily_days=3, responses=[_evolution_reply()],
            config={"EVOLVE_WEEKDAY": other_day, "EVOLVE_HOUR": (now.hour + 1) % 24},
        )
        probe.files[PERSONALITY_FILE] = {"model_retry_week": "2000-W01", "model_retry_after": "2000-01-01 00:00"}

        # 不是约定的小时：不试
        self.assertEqual((await probe.personality_evolution.maybe_evolve())["status"], "not_due")
        self.assertEqual(probe.llm_calls, 0)

        # 到了约定的小时：试，成功算回错过的那一周，下一次约定时刻照常跑
        probe.config["EVOLVE_HOUR"] = now.hour
        self.assertEqual((await probe.personality_evolution.maybe_evolve())["status"], "updated")
        state = probe.files[PERSONALITY_FILE]
        self.assertEqual(state["last_evolve_week"], "2000-W01")
        self.assertNotIn("model_retry_week", state)

    async def test_rollback_restores_previous_dynamic_block(self):
        probe = PersonalityProbe(daily_days=3, responses=[_evolution_reply()])
        await probe.personality_evolution.maybe_evolve(force=True)

        success, _message = probe.personality.rollback()
        state = probe.files[PERSONALITY_FILE]

        self.assertTrue(success)
        self.assertEqual(state["dynamic_block"]["recent_state"], "")
        self.assertEqual(state["history"], [])
        self.assertEqual(state["rollback"]["restored_snapshot_version"], 0)

    async def test_reflection_evidence_rejects_single_ordinary_suggestion(self):
        qualified = qualified_feedback([
            {
                "feedback_type": "suggestion", "topic": "少反问",
                "count": 1, "distinct_actors": 1, "owner_count": 0,
            },
            {
                "feedback_type": "correction", "topic": "认错视频",
                "count": 1, "distinct_actors": 1, "owner_count": 0,
            },
            {
                "feedback_type": "suggestion", "topic": "少用客服腔",
                "count": 1, "distinct_actors": 1, "owner_count": 1,
            },
        ])

        self.assertEqual(
            [item["topic"] for item in qualified], ["认错视频", "少用客服腔"]
        )

    async def test_model_cannot_invent_reflection_without_qualified_feedback(self):
        probe = PersonalityProbe(
            daily_days=3,
            responses=[_evolution_reply(reflections=["我不该犯这个错误"])],
        )

        await probe.personality_evolution.maybe_evolve(force=True)

        self.assertEqual(
            probe.files[PERSONALITY_FILE]["dynamic_block"]["recent_reflections"],
            [],
        )

    async def test_meme_candidates_need_two_grounded_examples_and_merge_aliases(self):
        probe = PersonalityProbe()
        source = "这个结尾绝绝子。雾号真的绝绝子。只有一次破防了。"
        candidates = [
            {
                "phrase": "绝绝子", "aliases": [],
                "evidence": ["这个结尾绝绝子", "雾号真的绝绝子"],
                "contexts": ["夸张赞叹"],
            },
            {
                "phrase": "绝绝子！", "aliases": ["绝绝子"],
                "evidence": ["这个结尾绝绝子", "雾号真的绝绝子"],
                "contexts": ["熟人玩笑"],
            },
            {
                "phrase": "破防了", "aliases": [],
                "evidence": ["只有一次破防了"], "contexts": ["感动"],
            },
        ]

        merged = await probe.personality_evolution.merge_grounded_meme_candidates(candidates, source)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["phrase"], "绝绝子")
        self.assertGreaterEqual(merged[0]["evidence_count"], 2)

    async def test_meme_prompt_is_expiring_contextual_and_low_frequency(self):
        probe = PersonalityProbe()
        probe.files[PERSONALITY_FILE] = {
            "memes": [{
                "phrase": "绝绝子", "meaning": "夸张赞叹",
                "contexts": ["轻松夸奖"], "avoid": "严肃争论",
                "expires_at": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"),
            }]
        }

        self.assertEqual(probe.personality.prompt(""), "")
        prompts = [probe.personality.prompt(f"测试语境{i}") for i in range(200)]
        exposed = [prompt for prompt in prompts if "绝绝子" in prompt]

        self.assertGreater(len(exposed), 0)
        self.assertLess(len(exposed), 60)
        self.assertTrue(all("不改变核心人设" in prompt for prompt in exposed))

    async def test_evolution_parser_rejects_unbounded_candidate_count(self):
        probe = PersonalityProbe()
        value = json.loads(_evolution_reply())
        value["meme_candidates"] = [{
            "phrase": "只有一个", "aliases": [],
            "evidence": ["a", "b"], "contexts": [],
        }]

        with self.assertRaisesRegex(ValueError, "meme_candidates_count_mismatch"):
            parse_weekly_evolution(json.dumps(value, ensure_ascii=False))


class ManualEditTests(unittest.TestCase):
    def setUp(self):
        self.files = {}
        self.personality = Personality(_ProbeFile(self.files, PERSONALITY_FILE))

    def test_add_keeps_the_last_five_habits_and_bumps_version(self):
        for i in range(6):
            self.personality.add_manual("习惯", f"习惯{i}")
        state = self.files[PERSONALITY_FILE]
        self.assertEqual(state["speech_habits"], [f"习惯{i}" for i in range(1, 6)])
        self.assertEqual(state["version"], 6)
        self.assertIn("手动维护的说话习惯：习惯1", self.personality.prompt())

    def test_unknown_category_writes_nothing(self):
        self.assertTrue(self.personality.add_manual("口头禅", "x").startswith("❌"))
        self.assertNotIn(PERSONALITY_FILE, self.files)

    def test_remove_by_position(self):
        self.assertEqual(self.personality.remove_manual("看法", 0), "🌱 没有演化记录")
        self.personality.add_manual("变化", "更爱看纪录片")
        self.assertTrue(self.personality.remove_manual("变化", 1).startswith("❌ 序号超范围"))
        self.assertEqual(self.personality.remove_manual("变化", 0), "✅ 已删除：更爱看纪录片")
        self.assertEqual(self.files[PERSONALITY_FILE]["evolved_traits"], [])


if __name__ == "__main__":
    unittest.main()
