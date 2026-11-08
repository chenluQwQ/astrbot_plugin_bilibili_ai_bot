"""原 ``WeeklySummaryMixin`` 的样子：测试里的假插件继承 ``SummaryParts``，就挂上日总结、周总结的几个对象。

真插件在 ``core/assembly.py`` 里建好 ``activity_digest``、``summary_image``、``summary_archive``、``summaries``；
假插件没走装配，第一次用到时现建（见 ``parts.py``）。``test_weekly_summary`` 在单独的包名下加载插件代码，
用 ``summary_parts(包名)`` 拿那个包里的版本。
"""
from importlib import import_module

from bili_parts import bili_parts
from memory_parts import memory_parts

from parts import part


def summary_parts(package="core"):
    def cls(module, name):
        return getattr(import_module(f"{package}.{module}"), name)

    class SummaryParts(bili_parts(package), memory_parts(package)):
        settings = cls("settings", "SettingsAccessor")()
        activity_digest = part(cls("features._activity_digest", "ActivityDigest"))
        summary_image = part(cls("infra.summary_image", "SummaryImage"))
        summary_archive = part(cls("domain.summary_archive", "SummaryArchive"))
        summaries = part(cls("features.summaries", "Summaries"))

    return SummaryParts


def __getattr__(name):
    # 用到时才导入插件代码：导入本文件时假的 astrbot 可能还没装上
    if name == "SummaryParts":
        return summary_parts()
    raise AttributeError(name)
