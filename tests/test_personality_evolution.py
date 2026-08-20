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
from core.personality import PersonalityMixin  # noqa: E402


class PersonalityProbe(PersonalityMixin):
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

        result = await probe._maybe_evolve_personality(force=True)

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(probe.llm_calls, 0)

    async def test_readiness_requires_three_distinct_nonempty_daily_summaries(self):
        probe = PersonalityProbe(daily_days=2)
        waiting = await probe._personality_evolution_readiness()
        probe.files[DAILY_SUMMARY_FILE].append({
            "date": (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"),
            "summary": "第三天",
            "structured": {"counts": {"videos": 0, "conversations": 0}},
        })
        still_waiting = await probe._personality_evolution_readiness()
        probe.files[DAILY_SUMMARY_FILE][-1]["structured"]["counts"]["videos"] = 1
        ready = await probe._personality_evolution_readiness()

        self.assertFalse(waiting["ready"])
        self.assertFalse(still_waiting["ready"])
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["days"], 3)

    async def test_readiness_can_use_privacy_filtered_structures_derived_from_logs(self):
        probe = PersonalityProbe(daily_days=0)
        probe._collect_weekly_data = lambda days=7: {"days": days}
        probe._group_activity_by_day = lambda _data: {
            "2026-08-18": {}, "2026-08-19": {}, "2026-08-20": {},
        }
        probe._build_structured_activity_summary = lambda _data, **kwargs: {
            "period": kwargs["period_key"],
            "counts": {"videos": 1},
            "conversation_highlights": [],
        }

        readiness = await probe._personality_evolution_readiness()

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["days"], 3)
        self.assertTrue(all(
            item["derived_from_activity_logs"] for item in readiness["daily"]
        ))

    async def test_weekly_update_is_bounded_versioned_and_only_runs_once(self):
        probe = PersonalityProbe(daily_days=3, responses=[_evolution_reply()])

        first = await probe._maybe_evolve_personality(force=True)
        second = await probe._maybe_evolve_personality(force=True)
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

        first = await probe._maybe_evolve_personality(force=True)
        second = await probe._maybe_evolve_personality(force=True)

        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "already_attempted")
        self.assertEqual(probe.llm_calls, 1)

    async def test_rollback_restores_previous_dynamic_block(self):
        probe = PersonalityProbe(daily_days=3, responses=[_evolution_reply()])
        await probe._maybe_evolve_personality(force=True)

        success, _message = probe._rollback_personality()
        state = probe.files[PERSONALITY_FILE]

        self.assertTrue(success)
        self.assertEqual(state["dynamic_block"]["recent_state"], "")
        self.assertEqual(state["history"], [])
        self.assertEqual(state["rollback"]["restored_snapshot_version"], 0)

    async def test_reflection_evidence_rejects_single_ordinary_suggestion(self):
        qualified = PersonalityProbe._qualified_feedback([
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

        await probe._maybe_evolve_personality(force=True)

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

        merged = await probe._merge_grounded_meme_candidates(candidates, source)

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

        self.assertEqual(probe._get_personality_prompt(""), "")
        prompts = [probe._get_personality_prompt(f"测试语境{i}") for i in range(200)]
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
            probe._parse_weekly_evolution(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
