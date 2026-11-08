"""原 ``LLMMixin`` 的样子，给测模型调用的假插件继承：``class Bot(LegacyModels)``。

``LLMMixin`` 拆掉以前，假插件继承它，测试直接 ``bot._llm_call(...)``，或在假插件上替换
``_get_system_prompt`` 这些。代码现在走 ``bot.models``：这里的 ``models`` 先看假插件自己有没有
替换过同名方法，有就用替换的，没有就走真的 ``Models``；假插件上的 ``_llm_call`` 等直接转给真的。
"""
from core.infra.models import Models
from core.settings import SettingsAccessor

_NAMES = ("_llm_call", "_summary_llm_call", "_generation_llm_call", "_get_system_prompt", "_llm_gateway")


class _ModelsOnBot:
    def __init__(self, bot, real):
        self._bot = bot
        self._real = real

    def __getattr__(self, name):
        if name in vars(self._bot) or getattr(type(self._bot), name, None) is not getattr(LegacyModels, name, None):
            return getattr(self._bot, name)
        return getattr(self._real, name)

    @property
    def _last_llm_error(self):
        return self._real._last_llm_error

    @_last_llm_error.setter
    def _last_llm_error(self, value):
        self._real._last_llm_error = value


class LegacyModels:
    settings = SettingsAccessor()

    def _real_models(self):
        real = self.__dict__.get("_real_models_instance")
        if real is None:
            real = self._real_models_instance = Models(self)
        return real

    @property
    def models(self):
        return _ModelsOnBot(self, self._real_models())

    @property
    def _last_llm_error(self):
        return self._real_models()._last_llm_error

    @_last_llm_error.setter
    def _last_llm_error(self, value):
        self._real_models()._last_llm_error = value


def _forward(name):
    def method(self, *args, **kwargs):
        return getattr(self._real_models(), name)(*args, **kwargs)
    method.__name__ = name
    return method


for _name in _NAMES:
    setattr(LegacyModels, _name, _forward(_name))
