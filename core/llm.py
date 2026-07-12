"""LLM 调用和系统提示词获取。"""
from astrbot.api import logger


class LLMMixin:
    """封装 AstrBot LLM 调用。"""

    async def _llm_call(self, prompt, system_prompt="", max_tokens=300, provider_id=None):
        try:
            pid = provider_id if provider_id is not None else self.config.get("LLM_PROVIDER_ID", "")
            # 人设走真正的 system role：① 增强人设遵循 ② 让人设成为稳定前缀，命中提示词缓存
            kwargs = {"prompt": prompt}
            if system_prompt:
                kwargs["system_prompt"] = system_prompt
            if pid:
                kwargs["chat_provider_id"] = pid
            resp = await self.context.llm_generate(**kwargs)
            return resp.completion_text.strip() if resp and resp.completion_text else None
        except Exception as e:
            logger.error(f"[BiliBot] LLM 调用失败: {e}")
            return None

    async def _get_system_prompt(self):
        if self.config.get("USE_ASTRBOT_PERSONA", True):
            try:
                persona = await self.context.persona_manager.get_default_persona_v3()
                if persona and persona.get("prompt"):
                    return persona["prompt"]
            except Exception as e:
                logger.warning(f"[BiliBot] 读取AstrBot自带人设失败，将使用自定义提示词: {e}")
        return self.config.get("CUSTOM_SYSTEM_PROMPT", "你是一个活跃在B站的角色，会回复评论、看视频、发动态。用自然的口语化风格交流。")
