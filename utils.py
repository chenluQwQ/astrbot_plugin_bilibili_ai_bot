"""LLM 调用和系统提示词获取。"""
from astrbot.api import logger


class LLMMixin:
    """封装 AstrBot LLM 调用。"""

    async def _llm_call(self, prompt, system_prompt="", max_tokens=300, provider_id=None):
        try:
            pid = provider_id if provider_id is not None else self.config.get("LLM_PROVIDER_ID", "")
            full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
            kwargs = {"prompt": full_prompt}
            if pid:
                kwargs["chat_provider_id"] = pid
            resp = await self.context.llm_generate(**kwargs)
            return resp.completion_text.strip() if resp and resp.completion_text else None
        except Exception as e:
            logger.error(f"[BiliBot] LLM 调用失败: {e}")
            return None

    def _get_system_prompt(self):
        if self.config.get("USE_ASTRBOT_PERSONA", True):
            try:
                persona = self.context.persona_manager.get_default_persona_v3()
                if persona:
                    return persona["prompt"]
            except Exception:
                pass
        return self.config.get("CUSTOM_SYSTEM_PROMPT", "你是一个B站UP主的AI助手。")
