"""原 ``ShareMixin`` 的样子：测试里的假插件继承 ``ShareParts``，就挂上 QQ 分享的几个对象。

真插件在 ``core/assembly.py`` 里建好 ``share``、``share_links``、``share_session``、``share_delivery``、
``share_reaction``、``qq_chat``；假插件没走装配，第一次用到时现建（见 ``parts.py``）。
"""
from importlib import import_module

from bili_parts import bili_parts
from video_parts import video_parts

from parts import part


def share_parts(package="core"):
    def cls(module, name):
        return getattr(import_module(f"{package}.{module}"), name)

    class ShareParts(bili_parts(package), video_parts(package)):
        settings = cls("settings", "SettingsAccessor")()
        share = part(cls("features.share", "ShareHandler"))
        share_links = part(cls("infra.bili.share_links", "ShareLinks"))
        share_session = part(cls("features._share_session", "ShareSession"))
        share_delivery = part(cls("features._share_delivery", "ShareDelivery"))
        share_reaction = part(cls("features._share_reaction", "ShareReaction"))
        qq_chat = part(cls("infra.qq_chat", "QQChat"))

    return ShareParts


def __getattr__(name):
    # 用到时才导入插件代码：导入本文件时假的 astrbot 可能还没装上
    if name == "ShareParts":
        return share_parts()
    raise AttributeError(name)
