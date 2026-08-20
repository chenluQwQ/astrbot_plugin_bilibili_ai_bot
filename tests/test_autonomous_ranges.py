"""Tests for autonomous quota ranges and owner-share controls."""

import sys
import tempfile
import types
import unittest
import json
from pathlib import Path


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _install_astrbot_stub():
    """Import core.* without a real AstrBot install (matches the other test modules)."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    if isinstance(getattr(sys.modules.get("astrbot"), "api", None), types.ModuleType):
        return
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.logger = _Logger()
    star = types.ModuleType("astrbot.api.star")
    data_dir = Path(tempfile.mkdtemp(prefix="bilibot-test-"))
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda _name: data_dir)
    event = types.ModuleType("astrbot.api.event")

    class _MessageChain:
        def __init__(self, *_args, **_kwargs):
            self.chain = []

        def message(self, *_args, **_kwargs):
            return self

    event.MessageChain = _MessageChain
    api.star = star
    api.event = event
    astrbot.api = api
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": star,
        "astrbot.api.event": event,
    })


_install_astrbot_stub()

from core.proactive import ProactiveMixin
from core.reply import ReplyMixin
from core.schedule_mixin import ScheduleMixin
from core.video import VideoMixin


class ScheduleProbe(ScheduleMixin):
    def __init__(self, config):
        self.config = config


class ProactiveProbe(ProactiveMixin):
    def __init__(self, config):
        self.config = config


class ReplyProbe(ReplyMixin):
    def __init__(self, config):
        self.config = config


class VideoProbe(VideoMixin):
    def __init__(self, config, cache=None):
        self.config = config
        self.cache = cache or {}
        self.analysis_calls = 0
        self.memory_writes = []

    def _load_json(self, _path, default=None):
        return self.cache if isinstance(self.cache, dict) else default

    def _save_json(self, _path, value):
        self.cache = value

    async def _get_video_oid(self, _bvid):
        return 123

    async def _oid_to_bvid(self, _oid):
        return "BV1234567890"

    async def _get_video_info(self, _oid):
        return {
            "bvid": "BV1234567890",
            "title": "测试视频",
            "desc": "",
            "owner_name": "测试UP",
            "owner_mid": "1",
            "tname": "动画",
            "duration": 60,
            "pic": "",
            "cid": 2,
        }

    async def _analyze_video_with_vision(self, _info):
        self.analysis_calls += 1
        return "重新分析后的正确内容"

    async def _evaluate_video(self, _info, _description):
        return {"score": 7, "mood": "平静", "review": "看完了"}

    async def _get_video_tags(self, _bvid):
        return []

    async def _get_hot_comments(self, _oid):
        return []

    async def _save_self_memory_record(self, *args, **kwargs):
        self.memory_writes.append((args, kwargs))


def _check_autonomous_range_honors_new_minimum_and_maximum():
    probe = ScheduleProbe({
        "AUTONOMOUS_PROACTIVE_DAILY_MIN": 2,
        "AUTONOMOUS_PROACTIVE_DAILY_MAX": 5,
        "AUTONOMOUS_PROACTIVE_DAILY_LIMIT": 4,
    })
    assert probe._autonomous_limit_range("proactive") == (2, 5)


def _check_autonomous_range_migrates_custom_legacy_limit_when_new_max_is_default():
    probe = ScheduleProbe({
        "AUTONOMOUS_REPLY_DAILY_MIN": 3,
        "AUTONOMOUS_REPLY_DAILY_MAX": 80,
        "AUTONOMOUS_REPLY_DAILY_LIMIT": 42,
    })
    assert probe._autonomous_limit_range("reply") == (3, 42)


def _check_owner_share_boolean_switch_overrides_delivery_mode():
    disabled = ProactiveProbe({
        "ENABLE_OWNER_RECOMMEND": False,
        "RECOMMEND_OWNER_DELIVERY": "both",
    })
    assert disabled._owner_recommend_delivery() == "off"

    enabled = ProactiveProbe({
        "ENABLE_OWNER_RECOMMEND": True,
        "RECOMMEND_OWNER_DELIVERY": "comment",
    })
    assert enabled._owner_recommend_delivery() == "comment"

from datetime import datetime


def _check_proactive_window_parser_and_fixed_schedule_are_stable():
    probe = ScheduleProbe({
        "SLEEP_START": 2,
        "SLEEP_END": 8,
        "FIXED_PROACTIVE_WINDOWS": ["10:00-11:30", "19:00-21:00"],
        "AUTONOMOUS_PROACTIVE_WINDOW_MINUTES": 90,
    })
    parsed = probe._parse_window_value("19:00-21:00")
    assert parsed["duration_minutes"] == 120
    first = probe._fixed_window_entries()
    second = probe._fixed_window_entries()
    assert first == second
    assert [item["start_time"] for item in first] == ["10:00", "19:00"]
    assert all(item["scheduled_time"] for item in first)


def _check_autonomous_plan_generation_supports_after_sleep_and_fixed_time():
    after_sleep = ScheduleProbe({
        "AUTONOMOUS_PLAN_GENERATION_MODE": "after_sleep",
        "AUTONOMOUS_PLAN_AFTER_SLEEP_MINUTES": 5,
        "SLEEP_END": 8,
    })
    assert not after_sleep._autonomous_generation_due(datetime(2026, 8, 16, 8, 4))
    assert after_sleep._autonomous_generation_due(datetime(2026, 8, 16, 8, 5))

    fixed = ScheduleProbe({
        "AUTONOMOUS_PLAN_GENERATION_MODE": "fixed_time",
        "AUTONOMOUS_PLAN_GENERATION_TIME": "00:10",
    })
    assert not fixed._autonomous_generation_due(datetime(2026, 8, 16, 0, 9))
    assert fixed._autonomous_generation_due(datetime(2026, 8, 16, 0, 10))


def _budget_limits(config, kind):
    from core.behavior_budget import BehaviorBudget

    class _Request:
        def __init__(self, kind):
            self.kind = kind
            self.metadata = {}

    budget = BehaviorBudget(lambda key, default=None: config.get(key, default))
    return {name: limit for name, _window, limit in budget.rules_for(_Request(kind), 0)}


def _check_budget_reads_daily_max_when_only_range_is_configured():
    """面板只写了 *_DAILY_MAX 时，统一行为预算必须按它收口。"""
    limits = _budget_limits({"AUTONOMOUS_REPLY_DAILY_MAX": 20}, "comment_reply")
    assert limits["behavior:comment_reply:day"] == 20


def _check_budget_prefers_daily_max_over_legacy_limit():
    limits = _budget_limits(
        {"AUTONOMOUS_REPLY_DAILY_MAX": 20, "AUTONOMOUS_REPLY_DAILY_LIMIT": 80},
        "comment_reply",
    )
    assert limits["behavior:comment_reply:day"] == 20


def _check_budget_falls_back_to_legacy_limit_for_upgraded_configs():
    """老配置只有 *_DAILY_LIMIT，升级后不能被 MAX 的默认值顶掉。"""
    limits = _budget_limits({"AUTONOMOUS_REPLY_DAILY_LIMIT": 15}, "comment_reply")
    assert limits["behavior:comment_reply:day"] == 15


def _check_budget_keeps_smaller_dynamic_limit():
    limits = _budget_limits(
        {"AUTONOMOUS_DYNAMIC_DAILY_MAX": 1, "DYNAMIC_DAILY_COUNT": 3}, "post_dynamic"
    )
    assert limits["behavior:post_dynamic:day"] == 1


def _check_autonomous_range_preserves_explicit_legacy_zero():
    probe = ScheduleProbe({
        "AUTONOMOUS_REPLY_DAILY_MIN": 0,
        "AUTONOMOUS_REPLY_DAILY_MAX": 80,
        "AUTONOMOUS_REPLY_DAILY_LIMIT": 0,
    })
    assert probe._autonomous_limit_range("reply") == (0, 0)


def _check_bili_private_tool_ceiling_rejects_parse_video_even_from_old_allowlist():
    probe = ReplyProbe({
        "BILI_ALLOW_SEARCH_TOOLS": True,
        "BILI_TOOL_ISOLATION_ENABLED": False,
        "BILI_TOOL_ALLOWLIST": ["bili_parse_video", "watch_video"],
    })
    allowed = probe._allowed_bili_tool_names()
    assert "watch_video" in allowed
    assert "bili_parse_video" not in allowed


def _check_config_schema_has_no_duplicate_keys():
    duplicates = []

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                duplicates.append(key)
            result[key] = value
        return result

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    json.loads(schema_path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    assert duplicates == []


class AsyncRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_proactive_windows_generate_nonempty_schedule(self):
        class PlanProbe(ScheduleProbe):
            def __init__(self):
                super().__init__({
                    "ENABLE_AUTONOMOUS_DAILY_PLAN": True,
                    "AUTONOMOUS_ACTIVITY_LEVEL": 100,
                    "AUTONOMOUS_PROACTIVE_DAILY_MIN": 0,
                    "AUTONOMOUS_PROACTIVE_DAILY_MAX": 4,
                    "AUTONOMOUS_PROACTIVE_DAILY_LIMIT": 4,
                    "PROACTIVE_DAILY_LIMIT": 4,
                    "ENABLE_PROACTIVE": True,
                    "ENABLE_DYNAMIC": False,
                    "ENABLE_BANGUMI": False,
                    "SPECIAL_FOLLOW_ENABLED": False,
                    "ENABLE_DYNAMIC_WATCH": False,
                    "ENABLE_REPLY": False,
                    "ENABLE_PRIVATE_MESSAGES": False,
                    "SLEEP_START": 2,
                    "SLEEP_END": 8,
                    "AUTONOMOUS_MIN_ACTION_GAP_MINUTES": 45,
                    "AUTONOMOUS_PROACTIVE_WINDOW_MINUTES": 90,
                })
                self.saved = {}

            def _load_json(self, _path, default=None):
                return self.saved or default

            def _save_json(self, _path, value):
                self.saved = value

            def _get_today_mood(self):
                return "平静", ""

            async def _get_system_prompt(self):
                return "测试人设"

            async def _llm_call(self, _prompt, **_kwargs):
                return '{"proactive_windows":["10:00-11:30","19:00-20:30"],"rationale":"测试"}'

            def _save_schedule_state(self, *_args):
                pass

            def _save_dynamic_schedule_state(self, *_args):
                pass

            def _save_bangumi_schedule_state(self, *_args):
                pass

            def _save_special_follow_schedule_state(self, *_args):
                pass

            def _save_dynamic_watch_schedule_state(self, *_args):
                pass

        plan = await PlanProbe()._ensure_autonomous_daily_plan(force=True)
        self.assertEqual(len(plan["proactive_windows"]), 2)
        self.assertEqual(len(plan["proactive_times"]), 2)

    async def test_mismatched_summary_is_reanalyzed_and_not_memorized_when_disabled(self):
        probe = VideoProbe(
            {"VIDEO_CACHE_TTL_MINUTES": 30, "ENABLE_VIDEO_LONG_TERM_MEMORY": False},
            cache={
                "BV1234567890": {
                    "bvid": "BV1234567890",
                    "title": "测试视频",
                    "summary": "字幕与本视频不符，需要先说明其他内容",
                    "time": "2000-01-01 00:00",
                }
            },
        )
        result = await probe._watch_video_and_save_memory("BV1234567890")
        self.assertTrue(result["ok"])
        self.assertEqual(probe.analysis_calls, 1)
        self.assertEqual(probe.memory_writes, [])

    async def test_comment_video_context_respects_long_term_memory_switch(self):
        probe = VideoProbe(
            {"VIDEO_CACHE_TTL_MINUTES": 30, "ENABLE_VIDEO_LONG_TERM_MEMORY": False}
        )
        context, cache_entry = await probe._get_video_context(123, 1)
        self.assertIn("测试视频", context)
        self.assertEqual(cache_entry["bvid"], "BV1234567890")
        self.assertEqual(probe.memory_writes, [])


class AutonomousRangeTests(unittest.TestCase):
    test_autonomous_range_honors_new_minimum_and_maximum = staticmethod(_check_autonomous_range_honors_new_minimum_and_maximum)
    test_autonomous_range_migrates_custom_legacy_limit_when_new_max_is_default = staticmethod(_check_autonomous_range_migrates_custom_legacy_limit_when_new_max_is_default)
    test_owner_share_boolean_switch_overrides_delivery_mode = staticmethod(_check_owner_share_boolean_switch_overrides_delivery_mode)
    test_proactive_window_parser_and_fixed_schedule_are_stable = staticmethod(_check_proactive_window_parser_and_fixed_schedule_are_stable)
    test_autonomous_plan_generation_supports_after_sleep_and_fixed_time = staticmethod(_check_autonomous_plan_generation_supports_after_sleep_and_fixed_time)
    test_budget_reads_daily_max_when_only_range_is_configured = staticmethod(_check_budget_reads_daily_max_when_only_range_is_configured)
    test_budget_prefers_daily_max_over_legacy_limit = staticmethod(_check_budget_prefers_daily_max_over_legacy_limit)
    test_budget_falls_back_to_legacy_limit_for_upgraded_configs = staticmethod(_check_budget_falls_back_to_legacy_limit_for_upgraded_configs)
    test_budget_keeps_smaller_dynamic_limit = staticmethod(_check_budget_keeps_smaller_dynamic_limit)
    test_autonomous_range_preserves_explicit_legacy_zero = staticmethod(_check_autonomous_range_preserves_explicit_legacy_zero)
    test_bili_private_tool_ceiling_rejects_parse_video_even_from_old_allowlist = staticmethod(_check_bili_private_tool_ceiling_rejects_parse_video_even_from_old_allowlist)
    test_config_schema_has_no_duplicate_keys = staticmethod(_check_config_schema_has_no_duplicate_keys)
