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

