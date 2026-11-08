"""Tests for autonomous quota ranges and owner-share controls."""

from base_parts import BaseParts
import sys
import asyncio
import tempfile
import types
import unittest

from bili_parts import BiliParts
from parts import part
from people_fakes import LegacyMood
import json
from unittest.mock import AsyncMock, patch
from pathlib import Path


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _install_astrbot_stub():
    """Import core.* without a real AstrBot install (matches the other test modules)."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    api = getattr(sys.modules.get("astrbot"), "api", None)
    if isinstance(api, types.ModuleType) and not getattr(api, "_logger_only", False):
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

from proactive_parts import ProactiveParts
from core.features._watch_video import WatchVideo
from core.features._reply_gate import ReplyGate
from reply_parts import ReplyParts
from schedule_parts import ScheduleParts, schedule_clock
from video_parts import VideoParts
from itself import itself


# 原来插件上没有心情时，每日计划和找视频都按「平静」写提示词
CALM = types.SimpleNamespace(today=lambda: ("平静", ""))


class ScheduleProbe(ScheduleParts):
    models = itself
    mood = CALM

    def __init__(self, config):
        self.config = config


class FailingPlanProbe(ScheduleProbe, BaseParts):
    def __init__(self):
        super().__init__({
            "ENABLE_AUTONOMOUS_DAILY_PLAN": True,
            "AUTONOMOUS_PLAN_GENERATION_MODE": "fixed_time",
            "AUTONOMOUS_PLAN_GENERATION_TIME": "08:05",
            "AUTONOMOUS_PLAN_RETRY_MINUTES": 15,
            "AUTONOMOUS_ACTIVITY_LEVEL": 50,
            "AUTONOMOUS_PROACTIVE_DAILY_MIN": 0,
            "AUTONOMOUS_PROACTIVE_DAILY_MAX": 0,
            "AUTONOMOUS_DYNAMIC_DAILY_MIN": 0,
            "AUTONOMOUS_DYNAMIC_DAILY_MAX": 0,
            "AUTONOMOUS_REPLY_DAILY_MIN": 0,
            "AUTONOMOUS_REPLY_DAILY_MAX": 0,
            "AUTONOMOUS_PRIVATE_DAILY_MIN": 0,
            "AUTONOMOUS_PRIVATE_DAILY_MAX": 0,
            "ENABLE_PROACTIVE": False,
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
        self.llm_calls = 0

    def _load_json(self, _path, default=None):
        return self.saved or default

    def _save_json(self, _path, value):
        self.saved = value

    mood = part(LegacyMood)

    def _get_today_mood(self):
        return "平静", ""

    async def _get_system_prompt(self):
        return "测试人设"

    async def _llm_call(self, _prompt, **_kwargs):
        self.llm_calls += 1
        self._last_llm_error = "测试模型不可用"
        return None

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


class ProactiveProbe(ProactiveParts):
    models = itself
    mood = CALM

    def __init__(self, config):
        self.config = config


class ReplyProbe(ReplyParts):
    models = itself

    def __init__(self, config):
        self.config = config


class VideoProbe(VideoParts, BiliParts):
    video_evaluate = itself
    models = itself

    def __init__(self, config, cache=None):
        self.config = config
        self.cache = cache or {}
        self.analysis_calls = 0
        self.memory_writes = []
        self._memory = []
        self.seen = {}

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

    async def _seen_video_record(self, bvid):
        return self.seen.get(bvid)

    async def _mark_video_seen(self, bvid, info=None, source="watch", *, increment=True):
        previous = self.seen.get(bvid, {})
        self.seen[bvid] = {
            **previous,
            **(info or {}),
            "bvid": bvid,
            "source": source,
            "watch_count": max(
                1, int(previous.get("watch_count", 0)) + (1 if increment else 0)
            ),
        }
        return True


def _check_autonomous_range_honors_new_minimum_and_maximum():
    probe = ScheduleProbe({
        "AUTONOMOUS_PROACTIVE_DAILY_MIN": 2,
        "AUTONOMOUS_PROACTIVE_DAILY_MAX": 5,
        "AUTONOMOUS_PROACTIVE_DAILY_LIMIT": 4,
    })
    assert probe.schedule_rules._autonomous_limit_range("proactive") == (0, 5)


def _check_autonomous_range_migrates_custom_legacy_limit_when_new_max_is_default():
    probe = ScheduleProbe({
        "AUTONOMOUS_REPLY_DAILY_MIN": 3,
        "AUTONOMOUS_REPLY_DAILY_MAX": 80,
        "AUTONOMOUS_REPLY_DAILY_LIMIT": 42,
    })
    assert probe.schedule_rules._autonomous_limit_range("reply") == (0, 42)


def _check_proactive_comment_count_is_daily_and_action_based():
    today = datetime.now().strftime("%Y-%m-%d")
    history = [
        {"time": f"{today} 10:00", "actions": ["💬评论"]},
        {"time": f"{today} 11:00", "actions": ["👍点赞"]},
        {"time": "2000-01-01 10:00", "actions": ["💬评论"]},
    ]
    proactive_log = [
        # Same action as the watch log: it must not be counted twice.
        {"time": f"{today} 10:00", "bvid": "BV1", "comment": "a"},
        {"time": f"{today} 12:00", "type": "bangumi", "title": "第1话", "comment": "b"},
    ]
    history[0]["bvid"] = "BV1"
    assert WatchVideo._today_proactive_comment_count(history, proactive_log) == 2


def _check_budget_separates_watch_rounds_videos_and_comments():
    watch_limits = _budget_limits({
        "PROACTIVE_DAILY_LIMIT": 5,
        "AUTONOMOUS_PROACTIVE_DAILY_MAX": 1,
    }, "proactive_watch")
    assert watch_limits["behavior:proactive_watch:day"] == 5
    comment_limits = _budget_limits({"PROACTIVE_COMMENT_DAILY_LIMIT": 3}, "proactive_comment")
    assert comment_limits["behavior:proactive_comment:day"] == 3


def _check_owner_share_boolean_switch_overrides_delivery_mode():
    disabled = ProactiveProbe({
        "ENABLE_OWNER_RECOMMEND": False,
        "RECOMMEND_OWNER_DELIVERY": "both",
    })
    assert disabled.owner_recommend._owner_recommend_delivery() == "off"

    enabled = ProactiveProbe({
        "ENABLE_OWNER_RECOMMEND": True,
        "RECOMMEND_OWNER_DELIVERY": "comment",
    })
    assert enabled.owner_recommend._owner_recommend_delivery() == "comment"

from datetime import datetime, timedelta


def _check_proactive_window_parser_and_fixed_schedule_are_stable():
    probe = ScheduleProbe({
        "SLEEP_START": 2,
        "SLEEP_END": 8,
        "PROACTIVE_TIMES_COUNT": 2,
        "FIXED_PROACTIVE_WINDOWS": ["10:00-11:30", "19:00-21:00"],
        "AUTONOMOUS_PROACTIVE_WINDOW_MINUTES": 90,
    })
    parsed = probe.schedule_rules._parse_window_value("19:00-21:00")
    assert parsed["duration_minutes"] == 120
    first = probe.schedule_rules._fixed_window_entries()
    second = probe.schedule_rules._fixed_window_entries()
    assert first == second
    assert [item["start_time"] for item in first] == ["10:00", "19:00"]
    assert all(item["scheduled_time"] for item in first)

    capped = ScheduleProbe({
        "ENABLE_PROACTIVE": True,
        "SLEEP_START": 2,
        "SLEEP_END": 8,
        "PROACTIVE_TIMES_COUNT": 1,
        "AUTONOMOUS_PROACTIVE_DAILY_MAX": 4,
        "FIXED_PROACTIVE_WINDOWS": ["10:00-11:30", "19:00-21:00"],
    })
    capped.schedule_files._save_schedule_state = lambda *_args: None
    times, _triggered = capped.schedule_files._generate_daily_schedule()
    assert len(times) == 1


def _check_autonomous_plan_generation_supports_after_sleep_and_fixed_time():
    after_sleep = ScheduleProbe({
        "AUTONOMOUS_PLAN_GENERATION_MODE": "after_sleep",
        "AUTONOMOUS_PLAN_AFTER_SLEEP_MINUTES": 5,
        "SLEEP_END": 8,
    })
    assert not after_sleep.daily_plan._autonomous_generation_due(datetime(2026, 8, 16, 8, 4))
    assert after_sleep.daily_plan._autonomous_generation_due(datetime(2026, 8, 16, 8, 5))

    fixed = ScheduleProbe({
        "AUTONOMOUS_PLAN_GENERATION_MODE": "fixed_time",
        "AUTONOMOUS_PLAN_GENERATION_TIME": "00:10",
    })
    assert not fixed.daily_plan._autonomous_generation_due(datetime(2026, 8, 16, 0, 9))
    assert fixed.daily_plan._autonomous_generation_due(datetime(2026, 8, 16, 0, 10))
    assert not fixed.daily_plan._autonomous_generation_due(datetime(2026, 8, 16, 0, 26))


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
    assert probe.schedule_rules._autonomous_limit_range("reply") == (0, 0)


def _check_bili_private_tool_ceiling_rejects_parse_video_even_from_old_allowlist():
    probe = ReplyProbe({
        "BILI_ALLOW_SEARCH_TOOLS": True,
        "BILI_TOOL_ISOLATION_ENABLED": False,
        "BILI_TOOL_ALLOWLIST": ["bili_parse_video", "watch_video"],
    })
    allowed = probe.reply_gate._allowed_bili_tool_names()
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


def _check_interaction_ad_filter_handles_plus_and_invalid_patterns():
    probe = ReplyProbe({
        "FILTER_LOW_VALUE_MESSAGES": False,
        "FILTER_AD_MESSAGES": True,
        "FILTER_DUPLICATE_MESSAGES": False,
    })
    assert probe.reply_gate._interaction_filter_reason("+vx abc123", "comment") == "advertisement_or_contact_spam"

    probe_type = ReplyGate
    original_patterns = probe_type._AD_PATTERNS
    original_logged = probe_type._INVALID_AD_PATTERNS_LOGGED
    try:
        probe_type._AD_PATTERNS = (r"(?:加|+)(?:微|vx)", r"https?://")
        probe_type._INVALID_AD_PATTERNS_LOGGED = set()
        assert probe.reply_gate._interaction_filter_reason("普通交流内容", "comment") is None
        assert probe.reply_gate._interaction_filter_reason("https://example.com", "comment") == "advertisement_or_contact_spam"
    finally:
        probe_type._AD_PATTERNS = original_patterns
        probe_type._INVALID_AD_PATTERNS_LOGGED = original_logged


def _check_video_format_fallbacks_include_portrait_short_side():
    formats = VideoProbe({}).video_files._format_fallbacks(480)
    assert any("[width<=360]" in value for value in formats)
    assert any("[width<=480]" in value for value in formats)
    assert formats[-2:] == [
        "bestvideo+bestaudio/best",
        "worst[ext=mp4][vcodec!=none]/worst[vcodec!=none]",
    ]


def _check_video_download_uses_short_timeout_for_each_format():
    probe = VideoProbe({"PROXY_URL": "http://127.0.0.1:7890"})
    observed_timeouts = []
    observed_formats = []

    async def fake_run_process(*args, **kwargs):
        assert args[args.index("--proxy") + 1] == "http://127.0.0.1:7890"
        format_pos = args.index("--format")
        observed_formats.append(args[format_pos + 1])
        observed_timeouts.append(kwargs.get("timeout"))
        return 1, "", "test failure"

    probe.processes._run_process = fake_run_process
    with tempfile.TemporaryDirectory() as temp_dir:
        with patch("core.infra.video_files.TEMP_VIDEO_DIR", temp_dir):
            result = asyncio.run(probe.video_files._download_video("BV1TEST12345", max_height=360))

    expected_formats = probe.video_files._format_fallbacks(360)
    assert result is None
    assert observed_formats == expected_formats
    assert observed_timeouts == [120] * len(expected_formats)


def _check_video_cache_uses_detail_long_term_and_faded_stages():
    now = datetime.now()
    cache = {}
    for label, age_days in (("detail", 10), ("long", 20), ("faded", 100)):
        cache[label] = {
            "bvid": label,
            "title": f"{label}视频",
            "owner_name": "测试UP",
            "owner_mid": "1",
            "tname": "动画",
            "desc": "简介",
            "analysis": "完整分析内容" * 30,
            "review": "详细感想",
            "time": (now - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M"),
        }
    probe = VideoProbe(
        {"VIDEO_MEMORY_DETAIL_DAYS": 15, "VIDEO_MEMORY_FADE_DAYS": 90},
        cache=cache,
    )

    assert probe.video_watch._compact_video_cache(cache)
    assert cache["detail"]["memory_stage"] == "detail"
    assert "analysis" in cache["detail"]
    assert cache["long"]["memory_stage"] == "long_term"
    assert "analysis" not in cache["long"]
    assert cache["long"].get("review") == "详细感想"
    assert cache["long"]["summary"]
    assert cache["faded"]["memory_stage"] == "faded"
    assert set(cache["faded"]) == {
        "bvid", "title", "owner_name", "owner_mid", "tname", "summary",
        "time", "source", "memory_stage", "faded_at",
        "score", "mood", "score_reason", "review", "analysis_basis", "content_analyzed",
    }
    assert not probe.video_watch._compact_video_cache(cache)


def _check_concrete_preference_signals_feed_search_fallback():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    history = [{
        "time": now, "score": 9, "tname": "动画",
        "search_keywords": ["守塔人 人物解析"],
        "preference_signals": [
            {"type": "character", "value": "守塔人父子", "polarity": "like", "strength": 0.9},
            {"type": "theme", "value": "流水线解说", "polarity": "fatigue", "strength": 0.8},
        ],
    }]
    probe = ProactiveProbe({"PROACTIVE_TASTE_WINDOW_DAYS": 7})

    fallback = probe.video_discovery._fallback_proactive_search_queries(history)
    summary = probe.taste._format_recent_preference_summary(history)

    assert "守塔人 人物解析" in fallback
    assert "守塔人父子" in fallback
    assert "流水线解说" in summary
    assert "厌倦" in summary


def _check_interest_report_separates_samples_and_persisted_preferences():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    history = [
        {
            "time": now,
            "score": 9,
            "tname": "动画",
            "up_name": "灯塔研究所",
            "source": "search",
            "source_detail": "守塔人解析",
            "search_keywords": ["守塔人父子"],
            "preference_signals": [
                {
                    "type": "character",
                    "value": "守塔人父子",
                    "polarity": "like",
                    "strength": 0.9,
                }
            ],
        },
        {
            "time": now,
            "score": 8,
            "tname": "动画",
            "up_name": "灯塔研究所",
            "source": "search",
            "source_detail": "守塔人解析",
            "preference_signals": [
                {
                    "type": "character",
                    "value": "守塔人父子",
                    "polarity": "like",
                    "strength": 0.8,
                }
            ],
        },
        {
            "time": now,
            "score": 3,
            "tname": "科技",
            "up_name": "模板视频",
        },
        {"time": now, "score": 0, "up_name": "评价失败样本"},
    ]
    lifecycle = [
        {
            "signal_type": "character",
            "value": "守塔人父子",
            "polarity": "like",
            "stage": "stable",
            "evidence_count": 6,
            "active_weeks": 3,
        }
    ]
    probe = ProactiveProbe({"PROACTIVE_TASTE_WINDOW_DAYS": 7})

    report = probe.taste._format_interest_report(history, lifecycle_items=lifecycle)

    assert "看过4个｜有效评分3个｜待评价1个" in report
    assert "动画：2个，平均8.5/10（偏喜欢）" in report
    assert "灯塔研究所：2个，平均8.5/10" in report
    assert "守塔人父子（近期偏喜欢，证据2次）" in report
    assert "[稳定喜欢] 人物：守塔人父子（证据6次，跨3周）" in report
    assert "守塔人解析×2" in report


class AsyncRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_long_term_memory_is_on_when_config_key_is_missing(self):
        probe = VideoProbe({})

        result = await probe.watch_on_request._watch_video_and_save_memory("BV1234567890")

        self.assertTrue(result["ok"])
        self.assertEqual(len(probe.memory_writes), 1)

    async def test_relevant_feedback_context_is_short_and_explicitly_non_persona(self):
        class Feedback:
            async def relevant(self, query, **_kwargs):
                self.query = query
                return [{
                    "topic": "回复太机械",
                    "examples": ["先回应对方说的具体内容"],
                }]

        feedback = Feedback()
        probe = ReplyProbe({})
        probe.layered_runtime = types.SimpleNamespace(
            is_open=True, feedback=feedback
        )

        context = await probe.reply_writing._relevant_feedback_context("这次回复有点机械")

        self.assertEqual(feedback.query, "这次回复有点机械")
        self.assertIn("回复太机械", context)
        self.assertIn("不是人格改写", context)
        self.assertNotIn("actor_id", context)

    async def test_concurrent_daily_plan_requests_share_one_model_call(self):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 20, 8, 5, 0)

        class SlowFailingPlanProbe(FailingPlanProbe):
            async def _llm_call(self, _prompt, **_kwargs):
                self.llm_calls += 1
                await asyncio.sleep(0.02)
                self._last_llm_error = "测试模型不可用"
                return None

        probe = SlowFailingPlanProbe()
        with schedule_clock(FrozenDateTime):
            first, second = await asyncio.gather(
                probe.daily_plan._ensure_autonomous_daily_plan(),
                probe.daily_plan._ensure_autonomous_daily_plan(),
            )
        self.assertEqual(probe.llm_calls, 1)
        self.assertEqual(first, second)

    async def test_failed_daily_plan_retries_respect_configured_limit(self):
        class FrozenDateTime(datetime):
            current = datetime(2026, 8, 20, 8, 5, 0)

            @classmethod
            def now(cls, tz=None):
                return cls.current

        probe = FailingPlanProbe()
        probe.config["AUTONOMOUS_PLAN_MAX_RETRIES"] = 1
        with schedule_clock(FrozenDateTime):
            first = await probe.daily_plan._ensure_autonomous_daily_plan()
            self.assertEqual(probe.llm_calls, 1)
            self.assertEqual(first["model_attempts"], 1)
            self.assertFalse(first["retry_exhausted"])

            FrozenDateTime.current = datetime(2026, 8, 20, 8, 19, 0)
            await probe.daily_plan._ensure_autonomous_daily_plan()
            self.assertEqual(probe.llm_calls, 1)

            FrozenDateTime.current = datetime(2026, 8, 20, 8, 20, 0)
            second = await probe.daily_plan._ensure_autonomous_daily_plan()
            self.assertEqual(probe.llm_calls, 2)
            self.assertEqual(second["model_attempts"], 2)
            self.assertTrue(second["retry_exhausted"])

            FrozenDateTime.current = datetime(2026, 8, 20, 8, 21, 0)
            await probe.daily_plan._ensure_autonomous_daily_plan()
            self.assertEqual(probe.llm_calls, 2)

    async def test_failed_daily_plan_default_allows_two_retries(self):
        """Issue #34: one unlucky model response must not kill the whole day."""
        class FrozenDateTime(datetime):
            current = datetime(2026, 8, 20, 8, 5, 0)

            @classmethod
            def now(cls, tz=None):
                return cls.current

        probe = FailingPlanProbe()
        with schedule_clock(FrozenDateTime):
            first = await probe.daily_plan._ensure_autonomous_daily_plan()
            self.assertEqual(first["model_attempts"], 1)
            self.assertFalse(first["retry_exhausted"])

            FrozenDateTime.current = datetime(2026, 8, 20, 8, 20, 0)
            second = await probe.daily_plan._ensure_autonomous_daily_plan()
            self.assertEqual(probe.llm_calls, 2)
            self.assertEqual(second["model_attempts"], 2)
            # 默认允许 2 次重试（共 3 次尝试），第二次失败仍不判死刑
            self.assertFalse(second["retry_exhausted"])

            FrozenDateTime.current = datetime(2026, 8, 20, 8, 35, 0)
            third = await probe.daily_plan._ensure_autonomous_daily_plan()
            self.assertEqual(probe.llm_calls, 3)
            self.assertEqual(third["model_attempts"], 3)
            self.assertTrue(third["retry_exhausted"])

            FrozenDateTime.current = datetime(2026, 8, 20, 8, 50, 0)
            await probe.daily_plan._ensure_autonomous_daily_plan()
            self.assertEqual(probe.llm_calls, 3)

    async def test_failed_plan_does_not_invent_proactive_or_dynamic_actions(self):
        probe = FailingPlanProbe()
        probe.config.update({
            "ENABLE_PROACTIVE": True,
            "PROACTIVE_DAILY_LIMIT": 6,
            "AUTONOMOUS_PROACTIVE_DAILY_MAX": 4,
            "ENABLE_DYNAMIC": True,
            "DYNAMIC_TIMES_COUNT": 2,
            "DYNAMIC_DAILY_COUNT": 2,
            "AUTONOMOUS_DYNAMIC_DAILY_MAX": 2,
        })
        plan = await probe.daily_plan._ensure_autonomous_daily_plan(force=True)
        self.assertEqual(plan["proactive_times"], [])
        self.assertEqual(plan["dynamic_times"], [])

    async def test_daily_plan_does_not_start_after_generation_window(self):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 20, 23, 3, 0)

        probe = FailingPlanProbe()
        with schedule_clock(FrozenDateTime):
            plan = await probe.daily_plan._ensure_autonomous_daily_plan()
        self.assertEqual(plan, {})
        self.assertEqual(probe.llm_calls, 0)

    async def test_model_proactive_windows_generate_nonempty_schedule(self):
        class PlanProbe(ScheduleProbe, BaseParts):
            def __init__(self):
                super().__init__({
                    "ENABLE_AUTONOMOUS_DAILY_PLAN": True,
                    "AUTONOMOUS_ACTIVITY_LEVEL": 100,
                    "AUTONOMOUS_PROACTIVE_DAILY_MIN": 0,
                    "AUTONOMOUS_PROACTIVE_DAILY_MAX": 4,
                    "AUTONOMOUS_PROACTIVE_DAILY_LIMIT": 4,
                    "PROACTIVE_TIMES_COUNT": 2,
                    # The video ceiling must not reduce the number of rounds.
                    "PROACTIVE_DAILY_LIMIT": 1,
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

            mood = part(LegacyMood)

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

        class Morning(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 20, 8, 5, 0)

        with schedule_clock(Morning):
            plan = await PlanProbe().daily_plan._ensure_autonomous_daily_plan(force=True)
        self.assertEqual(len(plan["proactive_windows"]), 2)
        self.assertEqual(len(plan["proactive_times"]), 2)

    async def _regenerate_at(self, hour, minute, reply, activity=100):
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 20, hour, minute, 0)

        class PlanProbe(ScheduleProbe, BaseParts):
            def __init__(self):
                super().__init__({
                    "ENABLE_AUTONOMOUS_DAILY_PLAN": True,
                    "AUTONOMOUS_ACTIVITY_LEVEL": activity,
                    "AUTONOMOUS_PROACTIVE_DAILY_MIN": 0,
                    "AUTONOMOUS_PROACTIVE_DAILY_MAX": 4,
                    "AUTONOMOUS_PROACTIVE_DAILY_LIMIT": 4,
                    "PROACTIVE_TIMES_COUNT": 2,
                    "ENABLE_PROACTIVE": True,
                    "ENABLE_DYNAMIC": True,
                    "DYNAMIC_TIMES_COUNT": 1,
                    "DYNAMIC_DAILY_COUNT": 1,
                    "AUTONOMOUS_DYNAMIC_DAILY_MAX": 1,
                    "AUTONOMOUS_DYNAMIC_DAILY_LIMIT": 1,
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
                self.prompts = []

            def _load_json(self, _path, default=None):
                return self.saved or default

            def _save_json(self, _path, value):
                self.saved = value

            mood = part(LegacyMood)

            def _get_today_mood(self):
                return "平静", ""

            async def _get_system_prompt(self):
                return "测试人设"

            async def _llm_call(self, prompt, **_kwargs):
                self.prompts.append(prompt)
                return reply

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

        probe = PlanProbe()
        with schedule_clock(Frozen):
            plan = await probe.daily_plan._ensure_autonomous_daily_plan(force=True)
        return plan, probe.prompts[0]

    async def test_regenerating_at_night_never_plans_the_morning(self):
        morning = '{"proactive_windows":["09:00-10:30","10:30-12:00"],"dynamic_times":["09:30"],"rationale":"测试"}'
        plan, prompt = await self._regenerate_at(20, 0, morning)
        self.assertIn("现在时间：20:00", prompt)
        times = plan["proactive_times"] + plan["dynamic_times"]
        self.assertEqual(len(plan["proactive_times"]), 2)
        self.assertEqual(len(plan["dynamic_times"]), 1)
        self.assertTrue(all(t >= "20:05" for t in times), times)
        for window in plan["proactive_windows"]:
            self.assertGreaterEqual(window["start_time"], "20:05")
            self.assertGreater(window["end_time"], window["start_time"])

    async def test_window_started_earlier_only_keeps_what_is_left(self):
        evening = '{"proactive_windows":["19:30-21:00"],"rationale":"测试"}'
        plan, _ = await self._regenerate_at(20, 0, evening)
        self.assertEqual(plan["proactive_windows"][0]["start_time"], "20:05")
        self.assertGreaterEqual(plan["proactive_times"][0], "20:05")

    async def test_late_time_past_the_active_window_still_runs(self):
        # 活跃度低时活跃窗口 20:15 就结束了，不能把模型要的 23:00 拉回过去。
        late = '{"proactive_windows":["22:30-23:30"],"rationale":"测试"}'
        plan, _ = await self._regenerate_at(22, 0, late, activity=10)
        self.assertEqual(plan["proactive_times"], ["23:00"])

    async def test_nothing_left_today_plans_nothing(self):
        morning = '{"proactive_windows":["09:00-10:30"],"dynamic_times":["09:30"],"rationale":"测试"}'
        plan, _ = await self._regenerate_at(23, 58, morning)
        self.assertEqual(plan["proactive_times"], [])
        self.assertEqual(plan["dynamic_times"], [])

    async def test_mismatched_summary_is_reanalyzed_and_not_memorized_when_disabled(self):
        probe = VideoProbe(
            {"VIDEO_MEMORY_DETAIL_DAYS": 15, "ENABLE_VIDEO_LONG_TERM_MEMORY": False},
            cache={
                "BV1234567890": {
                    "bvid": "BV1234567890",
                    "title": "测试视频",
                    "summary": "字幕与本视频不符，需要先说明其他内容",
                    "time": "2000-01-01 00:00",
                }
            },
        )
        result = await probe.watch_on_request._watch_video_and_save_memory("BV1234567890")
        self.assertTrue(result["ok"])
        self.assertEqual(probe.analysis_calls, 1)
        self.assertEqual(probe.memory_writes, [])

    async def test_comment_video_context_respects_long_term_memory_switch(self):
        probe = VideoProbe(
            {"VIDEO_MEMORY_DETAIL_DAYS": 15, "ENABLE_VIDEO_LONG_TERM_MEMORY": False}
        )
        context, cache_entry = await probe.video_context._get_video_context(123, 1)
        self.assertIn("测试视频", context)
        self.assertEqual(cache_entry["bvid"], "BV1234567890")
        self.assertEqual(probe.memory_writes, [])

    async def test_faded_video_cache_does_not_recreate_semantic_memory(self):
        probe = VideoProbe(
            {
                "VIDEO_MEMORY_DETAIL_DAYS": 15,
                "VIDEO_MEMORY_FADE_DAYS": 90,
                "ENABLE_VIDEO_LONG_TERM_MEMORY": True,
            },
            cache={
                "BV1234567890": {
                    "bvid": "BV1234567890",
                    "title": "很久以前的视频",
                    "owner_name": "测试UP",
                    "owner_mid": "1",
                    "tname": "动画",
                    "analysis": "已经应该淡忘的完整分析",
                    "time": (
                        datetime.now() - timedelta(days=100)
                    ).strftime("%Y-%m-%d %H:%M"),
                }
            },
        )

        context, cache_entry = await probe.video_context._get_video_context(123, 1)

        self.assertIn("很久以前的视频", context)
        self.assertEqual(cache_entry["memory_stage"], "faded")
        self.assertEqual(probe.memory_writes, [])

    async def test_seen_only_video_is_not_reanalyzed_without_force(self):
        probe = VideoProbe({"ENABLE_VIDEO_LONG_TERM_MEMORY": True})
        probe.seen["BV1234567890"] = {
            "bvid": "BV1234567890", "title": "测试视频", "watch_count": 1
        }

        remembered = await probe.watch_on_request._watch_video_and_save_memory("BV1234567890")
        self.assertTrue(remembered["seen_only"])
        self.assertEqual(probe.analysis_calls, 0)

        rewatched = await probe.watch_on_request._watch_video_and_save_memory(
            "BV1234567890", force_rewatch=True
        )
        self.assertTrue(rewatched["ok"])
        self.assertFalse(rewatched["from_cache"])
        self.assertEqual(probe.analysis_calls, 1)


class ProactiveDiscoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_all_directions_and_interleaves_without_seen_videos(self):
        probe = ProactiveProbe({})
        calls = []

        async def search(query, ps):
            calls.append(query)
            return [{"bvid": "seen"}, {"bvid": "shared"}] + [
                {"bvid": f"{query}-{i}", "title": query} for i in range(ps)
            ]

        probe.bili_videos.search_bilibili_videos = search
        with patch("asyncio.sleep", new=AsyncMock()):
            result = await probe.video_discovery._get_proactive_search_videos(
                ["one", "two", "three"], 6, watched_bvids={"seen"},
            )
        self.assertEqual(calls, ["one", "two", "three"])
        self.assertEqual([v["_search_keyword"] for v in result[:3]], calls)
        self.assertEqual(len(result), 6)
        self.assertEqual(len({v["bvid"] for v in result}), 6)
        self.assertNotIn("seen", {v["bvid"] for v in result})

    async def test_failed_direction_does_not_discard_other_search_results(self):
        probe = ProactiveProbe({})
        probe.bili_videos.search_bilibili_videos = AsyncMock(side_effect=[
            RuntimeError("offline"), [{"bvid": "valid"}], [],
        ])
        with patch("asyncio.sleep", new=AsyncMock()):
            result = await probe.video_discovery._get_proactive_search_videos(["one", "two", "three"], 3)
        self.assertEqual([v["bvid"] for v in result], ["valid"])
        self.assertEqual(probe.bili_videos.search_bilibili_videos.await_count, 3)

    async def test_exploration_survives_prefilter_and_full_persona_is_not_used_for_search(self):
        probe = ProactiveProbe({"ENABLE_PROACTIVE_LLM_PREFILTER": True})
        probe.json_store._load_json = lambda _path, default: default
        probe._get_system_prompt = AsyncMock(return_value="只喜欢深海的角色")
        probe._llm_call = AsyncMock(return_value='["数学悖论", "冷门语言学"]')
        queries = await probe.video_discovery._decide_proactive_search_queries([])
        self.assertEqual(len(queries), 3)
        probe._get_system_prompt.assert_not_awaited()
        self.assertEqual(probe._llm_call.await_count, 1)
        allow, _ = await probe.video_discovery._should_watch_video_before_download(
            {"_exploration": True, "title": "陌生题材"}, [], 0, 3,
        )
        self.assertTrue(allow)
        self.assertEqual(probe._llm_call.await_count, 1)

    async def test_share_uses_personal_score_and_keeps_delivery_limits(self):
        probe = ProactiveProbe({
            "RECOMMEND_OWNER_MIN_SCORE": 8, "RECOMMEND_OWNER_DAILY_LIMIT": 1,
        })
        evaluation = {"recommend_owner": True, "recommend_reason": "这段玩法让我笑了半天"}
        self.assertTrue(probe.owner_recommend._can_recommend_owner(evaluation, 9, 0))
        self.assertFalse(probe.owner_recommend._can_recommend_owner(evaluation, 9, 1))
        probe.config["ENABLE_OWNER_RECOMMEND"] = False
        self.assertFalse(probe.owner_recommend._can_recommend_owner(evaluation, 10, 0))
        probe.config["ENABLE_OWNER_RECOMMEND"] = True
        for score in (0, 6, 7, True, "9", float("nan"), float("inf")):
            self.assertFalse(probe.owner_recommend._can_recommend_owner(evaluation, score, 0))
        self.assertFalse(probe.owner_recommend._can_recommend_owner(dict(evaluation, recommend_owner=False), 10, 0))
        self.assertFalse(probe.owner_recommend._can_recommend_owner(dict(evaluation, recommend_reason=""), 10, 0))

    async def test_self_enjoyment_can_share_without_owner_interest_evidence(self):
        probe = ProactiveProbe({})
        probe._get_system_prompt = AsyncMock(return_value="测试人设")
        probe.json_store._load_json = lambda _path, default: default
        probe.mood = types.SimpleNamespace(today=lambda: ("平静", ""))
        probe.video_evaluate._owner_recommendation_context = AsyncMock(return_value="")
        probe._llm_call = AsyncMock(return_value=json.dumps({
            "score": 9, "score_reason": "实拍机关很巧妙", "comment": "", "mood": "震撼",
            "review": "没想到是用这种机关拍的", "want_follow": False,
            "recommend_owner": True,
            "recommend_reason": "实拍机关的做法让我很想分享", "partition": "影视",
            "preference_signals": [], "search_keywords": [],
        }, ensure_ascii=False))
        result = await probe.video_evaluate._evaluate_video({"title": "实拍幕后"}, "机关演示")
        self.assertEqual(result["score"], 9)
        self.assertTrue(result["recommend_owner"])
        self.assertEqual(result["recommend_reason"], "实拍机关的做法让我很想分享")
        self.assertTrue(probe.owner_recommend._can_recommend_owner(result, result["score"], 0))


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
    test_interaction_ad_filter_handles_plus_and_invalid_patterns = staticmethod(_check_interaction_ad_filter_handles_plus_and_invalid_patterns)
    test_video_format_fallbacks_include_portrait_short_side = staticmethod(_check_video_format_fallbacks_include_portrait_short_side)
    test_video_download_uses_short_timeout_for_each_format = staticmethod(_check_video_download_uses_short_timeout_for_each_format)
    test_video_cache_uses_detail_long_term_and_faded_stages = staticmethod(_check_video_cache_uses_detail_long_term_and_faded_stages)
    test_concrete_preference_signals_feed_search_fallback = staticmethod(_check_concrete_preference_signals_feed_search_fallback)
    test_interest_report_separates_samples_and_persisted_preferences = staticmethod(_check_interest_report_separates_samples_and_persisted_preferences)
    test_proactive_comment_count_is_daily_and_action_based = staticmethod(_check_proactive_comment_count_is_daily_and_action_based)
    test_budget_separates_watch_rounds_videos_and_comments = staticmethod(_check_budget_separates_watch_rounds_videos_and_comments)


class DailyPlanPersonaTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_prompt_uses_persona_from_models(self):
        # 真插件上 _get_system_prompt 只在 bot.models 上，插件自己没有这个名字
        prompts = []

        class Models:
            async def _get_system_prompt(self):
                return "爱看深海纪录片的角色"

            async def _llm_call(self, prompt, **_kwargs):
                prompts.append(prompt)
                return None

        def absent(_self):
            raise AttributeError("_get_system_prompt")

        class Probe(ScheduleProbe, BaseParts):
            models = Models()
            _get_system_prompt = property(absent)

            def _load_json(self, _path, default=None):
                return default

            def _save_json(self, _path, _value):
                pass
        probe = Probe({"ENABLE_AUTONOMOUS_DAILY_PLAN": True, "ENABLE_PROACTIVE": True, "SLEEP_START": 2, "SLEEP_END": 8})

        class Morning(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 20, 8, 5, 0)

        with schedule_clock(Morning):
            await probe.daily_plan._ensure_autonomous_daily_plan(force=True)
        self.assertTrue(prompts)
        self.assertIn("爱看深海纪录片的角色", prompts[0])
