"""LLM 调用和系统提示词获取。"""
from astrbot.api import logger


class LLMMixin:
    """封装 AstrBot LLM 调用。"""

    def _resolve_chat_provider_id(self, provider_id=None):
        """Resolve an explicit, configured, or AstrBot default chat provider.

        AstrBot 4.27+ requires ``chat_provider_id`` for every ``llm_generate``
        call.  Background tasks such as autonomous planning do not have a
        message UMO, so they must ask the context for the globally selected
        default provider instead of relying on the old implicit behavior.
        """
        if provider_id is not None and str(provider_id).strip():
            return str(provider_id).strip()

        configured = self.config.get("LLM_PROVIDER_ID", "")
        if configured and str(configured).strip() and str(configured).strip().lower() != "default":
            return str(configured).strip()

        getter = getattr(self.context, "get_using_provider", None)
        if callable(getter):
            try:
                provider = getter()
                if provider is not None:
                    meta = provider.meta() if callable(getattr(provider, "meta", None)) else None
                    provider_id = getattr(meta, "id", None) if meta is not None else None
                    if provider_id:
                        return str(provider_id)
            except Exception as exc:
                logger.debug(f"[BiliBot] 读取 AstrBot 默认聊天模型失败：{exc}")

        # A small compatibility fallback for older/mock Context objects.
        providers_getter = getattr(self.context, "get_all_providers", None)
        if callable(providers_getter):
            try:
                providers = providers_getter() or []
                if providers:
                    meta = providers[0].meta() if callable(getattr(providers[0], "meta", None)) else None
                    provider_id = getattr(meta, "id", None) if meta is not None else None
                    if provider_id:
                        return str(provider_id)
            except Exception as exc:
                logger.debug(f"[BiliBot] 读取可用聊天模型列表失败：{exc}")
        return ""

    async def _llm_call(self, prompt, system_prompt="", max_tokens=300, provider_id=None):
        try:
            pid = self._resolve_chat_provider_id(provider_id)
            if not pid:
                logger.error("[BiliBot] LLM 调用失败：未找到可用的默认对话模型，请检查 AstrBot 的默认聊天模型配置")
                return None
            # 人设走真正的 system role：① 增强人设遵循 ② 让人设成为稳定前缀，命中提示词缓存
            kwargs = {"prompt": prompt, "max_tokens": max_tokens, "chat_provider_id": pid}
            if system_prompt:
                kwargs["system_prompt"] = system_prompt
            resp = await self.context.llm_generate(**kwargs)
            return resp.completion_text.strip() if resp and resp.completion_text else None
        except Exception as e:
            logger.error(f"[BiliBot] LLM 调用失败: {e}")
            return None

    async def _get_system_prompt(self):
        base_prompt = ""
        if self.config.get("USE_ASTRBOT_PERSONA", True):
            try:
                persona = await self.context.persona_manager.get_default_persona_v3()
                if persona and persona.get("prompt"):
                    base_prompt = str(persona["prompt"]).strip()
            except Exception as e:
                logger.warning(f"[BiliBot] 读取AstrBot自带人设失败，将使用B站附加提示词: {e}")
        addon = str(self.config.get("CUSTOM_SYSTEM_PROMPT", "") or "").strip()
        if base_prompt and addon:
            return f"{base_prompt}\n\n【B站活动附加设定】\n{addon}"
        return base_prompt or addon or "你是一个活跃在B站的角色，会回复评论、看视频、发动态。用自然的口语化风格交流。"
