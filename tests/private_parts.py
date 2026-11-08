"""原 ``PrivateMessageMixin`` 的样子：测试里的假插件继承 ``PrivateParts``，就挂上私信的几个对象。

真插件在 ``core/assembly.py`` 里建好 ``private_poll``、``private_reply``、``private_lookup``、
``private_safety``、``bili_messages``；假插件没走装配，第一次用到时现建（见 ``parts.py``）。
``test_event_runtime`` 在单独的包名下加载插件代码，用 ``private_parts(包名)`` 拿那个包里的版本。
"""
from importlib import import_module

from bili_parts import bili_parts
from video_parts import video_parts

from core.settings import SettingsAccessor
from parts import part
from people_fakes import FakeProfileCandidates


def private_parts(package="core"):
    def cls(module, name):
        return getattr(import_module(f"{package}.{module}"), name)

    class PrivateParts(bili_parts(package), video_parts(package)):
        settings = SettingsAccessor()
        private_poll = part(cls("features.private_poll", "PrivatePoll"))
        private_reply = part(cls("features.private_reply", "PrivateReply"))
        private_lookup = part(cls("features._private_lookup", "PrivateLookup"))
        bili_lookups = part(cls("features._bili_lookups", "BiliLookups"))
        private_safety = part(cls("features._private_safety", "PrivateSafety"))
        bili_messages = part(cls("infra.bili.messages", "BiliMessages"))
        profile_candidates = part(FakeProfileCandidates)

    return PrivateParts



def __getattr__(name):
    # 用到时才导入插件代码：导入本文件时假的 astrbot 可能还没装上
    if name == "PrivateParts":
        return private_parts()
    raise AttributeError(name)
