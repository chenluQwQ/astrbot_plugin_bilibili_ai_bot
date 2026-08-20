"""Tests for autonomous quota ranges and owner-share controls."""

import sys
import tempfile
import types
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
from core.schedule_mixin import ScheduleMixin


class ScheduleProbe(ScheduleMixin):
    def __init__(self, config):
        self.config = config


class ProactiveProbe(ProactiveMixin):
    def __init__(self, config):
        self.config = config


def test_autonomous_range_honors_new_minimum_and_maximum():
    probe = ScheduleProbe({
        "AUTONOMOUS_PROACTIVE_DAILY_MIN": 2,
        "AUTONOMOUS_PROACTIVE_DAILY_MAX": 5,
        "AUTONOMOUS_PROACTIVE_DAILY_LIMIT": 4,
    })
    assert probe._autonomous_limit_range("proactive") == (2, 5)


def test_autonomous_range_migrates_custom_legacy_limit_when_new_max_is_default():
    probe = ScheduleProbe({
        "AUTONOMOUS_REPLY_DAILY_MIN": 3,
        "AUTONOMOUS_REPLY_DAILY_MAX": 80,
        "AUTONOMOUS_REPLY_DAILY_LIMIT": 42,
    })
    assert probe._autonomous_limit_range("reply") == (3, 42)


def test_owner_share_boolean_switch_overrides_delivery_mode():
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


def test_proactive_window_parser_and_fixed_schedule_are_stable():
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


def test_autonomous_plan_generation_supports_after_sleep_and_fixed_time():
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


def test_budget_reads_daily_max_when_only_range_is_configured():
    """面板只写了 *_DAILY_MAX 时，统一行为预算必须按它收口。"""
    limits = _budget_limits({"AUTONOMOUS_REPLY_DAILY_MAX": 20}, "comment_reply")
    assert limits["behavior:comment_reply:day"] == 20


def test_budget_prefers_daily_max_over_legacy_limit():
    limits = _budget_limits(
        {"AUTONOMOUS_REPLY_DAILY_MAX": 20, "AUTONOMOUS_REPLY_DAILY_LIMIT": 80},
        "comment_reply",
    )
    assert limits["behavior:comment_reply:day"] == 20


def test_budget_falls_back_to_legacy_limit_for_upgraded_configs():
    """老配置只有 *_DAILY_LIMIT，升级后不能被 MAX 的默认值顶掉。"""
    limits = _budget_limits({"AUTONOMOUS_REPLY_DAILY_LIMIT": 15}, "comment_reply")
    assert limits["behavior:comment_reply:day"] == 15


def test_budget_keeps_smaller_dynamic_limit():
    limits = _budget_limits(
        {"AUTONOMOUS_DYNAMIC_DAILY_MAX": 1, "DYNAMIC_DAILY_COUNT": 3}, "post_dynamic"
    )
    assert limits["behavior:post_dynamic:day"] == 1
