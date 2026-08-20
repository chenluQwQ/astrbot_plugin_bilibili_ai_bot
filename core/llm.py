"""LLM 调用、全局熔断和系统提示词获取。"""
import asyncio
import time

from astrbot.api import logger


class LLMMixin:
    """封装 AstrBot LLM 调用。"""

    def _llm_circuit_settings(self):
        """Return the failure threshold and cooldown without trusting config types."""
        try:
            threshold = max(
                int(self.config.get("LLM_CIRCUIT_FAILURE_THRESHOLD", 5) or 0),
                0,
            )
        except (TypeError, ValueError):
            threshold = 5
        try:
            cooldown = max(
                float(self.config.get("LLM_CIRCUIT_COOLDOWN_SECONDS", 120) or 120),
                1.0,
            )
        except (TypeError, ValueError):
            cooldown = 120.0
        return threshold, cooldown

    def _llm_circuit_lock_obj(self):
        lock = getattr(self, "_llm_circuit_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._llm_circuit_lock = lock
        return lock

    async def _enter_llm_circuit(self):
        """Claim one provider attempt.

        A provider call is never retried here.  Once the circuit cools down only
        one half-open probe is allowed, so concurrent background jobs cannot
        create a request burst while the provider is recovering.
        """
        threshold, _ = self._llm_circuit_settings()
        if threshold <= 0:
            return "disabled"

        should_log = False
        skip_reason = ""
        async with self._llm_circuit_lock_obj():
            now = time.monotonic()
            open_until = float(getattr(self, "_llm_circuit_open_until", 0.0) or 0.0)
            if open_until > now:
                last_log = float(getattr(self, "_llm_circuit_last_skip_log", 0.0) or 0.0)
                if now - last_log >= 30:
                    self._llm_circuit_last_skip_log = now
                    should_log = True
                remaining = max(1, int(open_until - now))
                skip_reason = f"模型服务连续失败后正在冷却，约 {remaining} 秒后自动探测"
                mode = None
            elif open_until > 0:
                if getattr(self, "_llm_circuit_half_open", False):
                    skip_reason = "模型服务正在进行恢复探测，本次调用已跳过以避免重复请求"
                    mode = None
                else:
                    self._llm_circuit_half_open = True
                    mode = "probe"
            else:
                mode = "closed"

        if should_log:
            remaining = max(1, int(open_until - time.monotonic()))
            logger.warning(f"[BiliBot] LLM 熔断中，本次调用已跳过；约 {remaining} 秒后允许一次探测")
        if mode is None:
            self._last_llm_error = skip_reason or "模型服务暂时不可用，本次调用已安全跳过"
        return mode

    async def _record_llm_success(self, mode):
        if mode == "disabled":
            return
        async with self._llm_circuit_lock_obj():
            # A request that was already in flight when another request opened
            # the circuit must not close it.  Only a half-open probe may do so.
            is_open = float(getattr(self, "_llm_circuit_open_until", 0.0) or 0.0) > 0
            if mode != "probe" and is_open:
                return
            self._consecutive_llm_failures = 0
            self._llm_circuit_open_until = 0.0
            self._llm_circuit_half_open = False

    async def _record_llm_failure(self, mode, reason):
        threshold, cooldown = self._llm_circuit_settings()
        if mode == "disabled" or threshold <= 0:
            return

        opened = False
        failure_count = 0
        async with self._llm_circuit_lock_obj():
            now = time.monotonic()
            failure_count = int(getattr(self, "_consecutive_llm_failures", 0) or 0) + 1
            self._consecutive_llm_failures = failure_count
            already_open = float(getattr(self, "_llm_circuit_open_until", 0.0) or 0.0) > now
            if mode == "probe" or (failure_count >= threshold and not already_open):
                self._llm_circuit_open_until = now + cooldown
                self._llm_circuit_half_open = False
                opened = True

        if opened:
            logger.warning(
                f"[BiliBot] LLM 连续失败 {failure_count} 次，熔断 {int(cooldown)} 秒；"
                f"期间不会重复申请，原因：{reason}"
            )

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
        self._last_llm_error = ""
        circuit_mode = await self._enter_llm_circuit()
        if circuit_mode is None:
            return None
        try:
            pid = self._resolve_chat_provider_id(provider_id)
            if not pid:
                reason = "未找到可用的默认对话模型"
                self._last_llm_error = reason
                logger.error(f"[BiliBot] LLM 调用失败：{reason}，请检查 AstrBot 的默认聊天模型配置")
                await self._record_llm_failure(circuit_mode, reason)
                return None
            # 人设走真正的 system role：① 增强人设遵循 ② 让人设成为稳定前缀，命中提示词缓存
            kwargs = {"prompt": prompt, "max_tokens": max_tokens, "chat_provider_id": pid}
            if system_prompt:
                kwargs["system_prompt"] = system_prompt
            resp = await self.context.llm_generate(**kwargs)
            text = resp.completion_text.strip() if resp and resp.completion_text else ""
            if not text:
                reason = "模型返回空内容"
                self._last_llm_error = reason
                logger.error(f"[BiliBot] LLM 调用失败：{reason}")
                await self._record_llm_failure(circuit_mode, reason)
                return None
            await self._record_llm_success(circuit_mode)
            return text
        except Exception as e:
            self._last_llm_error = f"模型请求异常：{e}"[:240]
            logger.error(f"[BiliBot] LLM 调用失败: {e}")
            await self._record_llm_failure(circuit_mode, str(e))
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
