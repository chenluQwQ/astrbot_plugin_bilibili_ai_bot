"""Tests for autonomous quota ranges and owner-share controls."""
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
