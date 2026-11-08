"""原 ``ProactiveMixin`` 的样子：测试里的假插件继承 ``ProactiveParts``，就挂上主动看片的几个对象。

真插件在 ``core/assembly.py`` 里建好 ``video_pools``、``taste``、``preferences``、``video_discovery``、
``video_evaluate``、``outward_actions``、``owner_recommend``、``watch_video``、``proactive_browse``、
``special_follow``、``dynamic_watch``、``up_skip``；假插件没走装配，第一次用到时现建（见 ``parts.py``）。

原来假插件在自己类上写 ``_evaluate_video`` 这类方法就能替掉 mixin 上的同名方法；现在这些方法在对象上，
所以对象建好时把假插件类上写的同名方法挂到对象上（见 ``parts.preferring``），原来的写法照样生效。
"""
from importlib import import_module

from bili_parts import bili_parts
from video_parts import video_parts

from parts import part, preferring


def _cls(module, name):
    return getattr(import_module(f"core.{module}"), name)


class _HostFile:
    """``JsonFile`` 的样子，读写走假插件的 ``json_store``（快照测试从这里记录文件）。"""

    def __init__(self, bot, path):
        self._bot, self._path = bot, path

    def read(self, default=None):
        return self._bot.json_store._load_json(self._path, default)

    def update(self, change, default=None):
        data = self.read(default)
        result = change(data)
        result = data if result is None else result
        self._bot.json_store._save_json(self._path, result)
        return result


class _NoFeedback:
    async def maybe_ask(self, *_args, **_kwargs):
        return False


def _up_skip(bot):
    config, domain = import_module("core.config"), import_module("core.domain.up_skip")
    return domain.UpSkip(_HostFile(bot, config.UP_SKIP_FILE))


def proactive_parts():
    class ProactiveParts(bili_parts("core"), video_parts("core")):
        settings = getattr(import_module("core.settings"), "SettingsAccessor")()
        video_pools = preferring(_cls("infra.bili.video_pools", "BiliVideoPools"))
        taste = preferring(_cls("domain.taste", "Taste"))
        preferences = preferring(_cls("domain.preferences", "Preferences"))
        video_discovery = preferring(_cls("domain.video_discovery", "VideoDiscovery"))
        video_evaluate = preferring(_cls("features._video_evaluate", "VideoEvaluate"))
        outward_actions = preferring(_cls("features._outward_actions", "OutwardActions"))
        owner_recommend = preferring(_cls("features._owner_recommend", "OwnerRecommend"))
        watch_video = preferring(_cls("features._watch_video", "WatchVideo"))
        proactive_browse = preferring(_cls("features.proactive_browse", "ProactiveBrowse"))
        special_follow = preferring(_cls("features.special_follow", "SpecialFollow"))
        dynamic_watch = preferring(_cls("features.dynamic_watch", "DynamicWatch"))
        up_skip = part(_up_skip)
        # 分享后追问有概率、要调 QQ 会话的模型；流程测试里不问（单独测在 test_owner_feedback.py）
        owner_feedback = part(lambda bot: _NoFeedback())

    return ProactiveParts


def __getattr__(name):
    # 用到时才导入插件代码：导入本文件时假的 astrbot 可能还没装上
    if name == "ProactiveParts":
        return proactive_parts()
    raise AttributeError(name)
