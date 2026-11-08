"""测试里的 ``bot.vision``：假插件上替换过的识图、向量方法照样生效，没替换的走真的 ``Vision``。

``VisionMixin`` 拆掉以前，测试直接在假插件上替换 ``_get_embedding`` 这些；代码现在走
``bot.vision``，给假插件挂 ``vision_on(bot)`` 就不用逐个改替换的写法。
"""
from core.infra.vision import Vision


class _VisionOnBot:
    def __init__(self, bot):
        self._bot = bot
        self._real = Vision(bot)

    def __getattr__(self, name):
        try:
            return getattr(self._bot, name)
        except AttributeError:
            return getattr(self._real, name)


def vision_on(bot):
    return _VisionOnBot(bot)
