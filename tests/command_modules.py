"""测试里用 ``core/commands/``：没装 AstrBot 时先装最小的替身，再 import。

命令函数的第一个参数是 ``BotApp``；测试用 ``app_for(假插件)`` 包一层，假插件上只需要
``BotApp`` 背后实际用到的那几个名字。
"""
import importlib
import sys
import types

from test_autonomous_ranges import _install_astrbot_stub


def _ensure_message_components():
    if "astrbot.api.message_components" in sys.modules:
        return
    module = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text):
            self.text = text

    class Image:
        @classmethod
        def fromFileSystem(cls, path):
            image = cls()
            image.path = path
            return image

    module.Plain, module.Image = Plain, Image
    sys.modules[module.__name__] = module


def load(name):
    """``core.commands.<name>`` 模块。"""
    _install_astrbot_stub()
    _ensure_message_components()
    return importlib.import_module(f"core.commands.{name}")


def app_for(plugin):
    from core.app import BotApp
    return BotApp(plugin)
