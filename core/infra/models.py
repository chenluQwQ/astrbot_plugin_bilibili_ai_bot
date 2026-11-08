"""插件上的 ``models``：调模型（经 ``LLMGateway``）和取系统提示词。

方法名沿用早期版本（``_llm_call`` 这些）。``host`` 提供 ``config``、``context``、``settings``，就是插件对象本身。
"""
from __future__ import annotations

from astrbot.api import logger

from .llm import LLMGateway


class Models:
    def __init__(self, host):
        self._host = host
        self._gateway = LLMGateway(lambda: host.config, lambda: host.context)

    def _llm_gateway(self):
        return self._gateway

    @property
    def _last_llm_error(self):
        return self._gateway.last_error

    @_last_llm_error.setter
    def _last_llm_error(self, value):
        self._gateway.last_error = value

    async def _llm_call(self, prompt, system_prompt="", max_tokens=300, provider_id=None):
        return await self._gateway.call(
            prompt, system_prompt=system_prompt, max_tokens=max_tokens, provider_id=provider_id
        )

    async def tool_loop(self, prompt, system_prompt="", **kwargs):
        """回复前先让模型调工具（AstrBot 的工具循环），见 ``LLMGateway.tool_loop``。"""
        return await self._gateway.tool_loop(prompt, system_prompt, **kwargs)

    async def _summary_llm_call(self, prompt, **kwargs):
        return await self._gateway.purpose_call("SUMMARY", prompt, **kwargs)

    async def _generation_llm_call(self, prompt, **kwargs):
        return await self._gateway.purpose_call("GENERATION", prompt, **kwargs)

    async def _get_system_prompt(self):
        settings = self._host.settings
        base_prompt = ""
        if settings.get("USE_ASTRBOT_PERSONA"):
            try:
                persona = await self._host.context.persona_manager.get_default_persona_v3()
                if persona and persona.get("prompt"):
                    base_prompt = str(persona["prompt"]).strip()
            except Exception as e:
                logger.warning(f"[BiliBot] 读取AstrBot自带人设失败，将使用B站附加提示词: {e}")
        addon = str(settings.get("CUSTOM_SYSTEM_PROMPT") or "").strip()
        if base_prompt and addon:
            return f"{base_prompt}\n\n【B站活动附加设定】\n{addon}"
        return base_prompt or addon or "你是一个活跃在B站的角色，会回复评论、看视频、发动态。用自然的口语化风格交流。"
