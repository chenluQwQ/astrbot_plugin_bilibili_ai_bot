"""原 ``ScheduleMixin`` 的样子：测试里的假插件继承 ``ScheduleParts``，就挂上日程的几个对象。

真插件在 ``core/assembly.py`` 里建好 ``schedule_rules``、``day_schedules``、``schedule_files``、``daily_plan``、
``activities``、``now_doing``；假插件没走装配，第一次用到时现建。假插件类上写了同名方法的
（``_save_schedule_state``、``_is_awake_minute`` 这类），建对象时挂到对象上（见 ``parts.preferring``）。

``SimpleNamespace`` 做的假插件用 ``attach_schedule``。原来 ``patch("core.schedule_mixin.datetime", ...)``
一处就够，现在日期散在几个模块里，用 ``schedule_clock``。
"""
from contextlib import ExitStack
from importlib import import_module
from unittest.mock import patch

from base_parts import BaseParts
from parts import part, preferring

# 用到 datetime 的日程模块
CLOCK_MODULES = (
    "core.day_schedules", "core.schedule_files", "core.features.daily_plan", "core.activities",
    "core.lifecycle.schedule_rules",
)


def _lazy(module, name):
    # 导入本文件时假的 astrbot 可能还没装上，用到时才导入
    load = lambda: getattr(import_module(f"core.{module}"), name)  # noqa: E731
    load.lazy = True
    return load


def _rules():
    real = getattr(import_module("core.lifecycle.schedule_rules"), "ScheduleRules")

    def __init__(self, bot):
        real.__init__(self, lambda: bot.settings)
    return type("ScheduleRules", (real,), {"__init__": __init__})


_rules.lazy = True


def _now_doing(bot):
    return getattr(import_module("core.lifecycle.now_doing"), "NowDoing")()


class ScheduleParts(BaseParts):
    schedule_rules = preferring(_rules, inherited=True)
    day_schedules = preferring(_lazy("day_schedules", "DaySchedules"))
    schedule_files = preferring(_lazy("schedule_files", "ScheduleFiles"))
    daily_plan = preferring(_lazy("features.daily_plan", "DailyPlan"))
    activities = preferring(_lazy("activities", "Activities"))
    now_doing = part(_now_doing)


def attach_schedule(fake):
    """给 ``SimpleNamespace`` 做的假插件挂上真的日程对象（要先 ``attach_base``，日程要读写 json）。"""
    from core.activities import Activities
    from core.day_schedules import DaySchedules
    from core.features.daily_plan import DailyPlan
    from core.lifecycle.now_doing import NowDoing
    from core.lifecycle.schedule_rules import ScheduleRules
    from core.schedule_files import ScheduleFiles

    fake.schedule_rules = ScheduleRules(lambda: fake.settings)
    fake.day_schedules = DaySchedules(fake)
    fake.schedule_files = ScheduleFiles(fake)
    if not hasattr(fake, "daily_plan"):
        fake.daily_plan = DailyPlan(fake)
    fake.activities = Activities(fake)
    if not hasattr(fake, "now_doing"):
        fake.now_doing = NowDoing()
    return fake


def schedule_clock(fake_datetime):
    """把日程几个模块里的 ``datetime`` 一起换掉。"""
    stack = ExitStack()
    for module in CLOCK_MODULES:
        stack.enter_context(patch(f"{module}.datetime", fake_datetime))
    return stack


class LegacyNowDoing:
    """假插件类上自己写了 ``_set_cross_platform_activity`` / ``_clear_cross_platform_activity``（记录调用）的，
    挂 ``now_doing = part(LegacyNowDoing)``：``now_doing.report / finish`` 转回那两个旧名字。"""

    def __init__(self, bot):
        self._bot = bot

    def report(self, kind, stage, **details):
        self._bot._set_cross_platform_activity(kind, stage, **details)

    def finish(self, kind):
        self._bot._clear_cross_platform_activity(kind)
