"""原 ``VideoMixin`` 的样子：测试里的假插件继承 ``VideoParts``，就挂上看片、视频文件和回复上下文的几个对象。

真插件在 ``core/assembly.py`` 里建好 ``video_files``、``video_watch``、``watch_on_request``、``video_context``、
``dynamic_context``；假插件没走装配，第一次用到时现建。假插件类上写了同名方法的（``_download_video`` 这类），
建对象时挂到对象上（见 ``parts.preferring``）。
"""
from importlib import import_module

from memory_parts import memory_parts
from parts import preferring


def video_parts(package="core"):
    def cls(module, name):
        return getattr(import_module(f"{package}.{module}"), name)

    def lazy(module, name):
        # 有的测试在单独的包名下只加载几个模块，用到时才导入，免得把用不上的模块也拉进来
        load = lambda: cls(module, name)  # noqa: E731
        load.lazy = True
        return load

    class VideoParts(memory_parts(package)):
        video_files = preferring(lazy("infra.video_files", "VideoFiles"))
        video_watch = preferring(lazy("domain.video_watch", "VideoWatch"))
        watch_on_request = preferring(lazy("features.watch_on_request", "WatchOnRequest"))
        video_context = preferring(lazy("features._video_context", "VideoContext"))
        dynamic_context = preferring(lazy("features._dynamic_context", "DynamicContext"))

    return VideoParts


def __getattr__(name):
    # 用到时才导入插件代码：导入本文件时假的 astrbot 可能还没装上
    if name == "VideoParts":
        return video_parts()
    raise AttributeError(name)
