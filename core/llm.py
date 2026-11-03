"""LLM 调用、全局熔断和系统提示词获取。"""
import asyncio
import time

from astrbot.api import logger
from .video_diagnostics import set_call_diagnostic, exception_detail, safe_detail


class LLMMixin:
    """封装 AstrBot LLM 调用。"""

    async def _summary_llm_call(self, prompt, **kwargs):
        return await self._task_llm_call("SUMMARY", prompt, **kwargs)

    async def _generation_llm_call(self, prompt, **kwargs):
        return await self._task_llm_call("GENERATION", prompt, **kwargs)

    def _task_llm_circuit(self, purpose):
        circuits = getattr(self, "_task_llm_circuits", None)
        if circuits is None:
            circuits = self._task_llm_circuits = {}
        if purpose not in circuits:
            circuits[purpose] = LLMMixin()
        circuit = circuits[purpose]
        circuit.config = self.config
        return circuit

    async def _task_llm_call(self, purpose, prompt, system_prompt="", max_tokens=600):
        """Separate factual summaries from persona writing; blank settings inherit chat."""
        from .network import openai_proxy_kwargs
        prefix = str(purpose).upper()
        set_call_diagnostic()
        provider = str(self.config.get(f"{prefix}_PROVIDER_ID", "") or "").strip()
        api_key = str(self.config.get(f"{prefix}_API_KEY", "") or "").strip()
        model = str(self.config.get(f"{prefix}_MODEL", "") or "").strip()
        if provider and provider.lower() != "default":
            return await self._llm_call(prompt, system_prompt=system_prompt,
                                        max_tokens=max_tokens, provider_id=provider, _circuit=self._task_llm_circuit(prefix))
        if not api_key:
            return await self._llm_call(prompt, system_prompt=system_prompt,
                                        max_tokens=max_tokens)
        if not model:
            self._last_llm_error = f"{prefix} 独立 API 未填写模型名"
            set_call_diagnostic(reason=self._last_llm_error, route="independent_api", purpose=prefix)
            logger.warning(f"[BiliBot] {self._last_llm_error}")
            return None
        circuit = self._task_llm_circuit(prefix)
        set_call_diagnostic(route="independent_api", model=safe_detail(model, self.config, 100), purpose=prefix)
        mode = await circuit._enter_llm_circuit()
        if mode is None:
            self._last_llm_error = getattr(circuit, "_last_llm_error", "模型暂不可用")
            set_call_diagnostic(reason=self._last_llm_error, stage="circuit")
            return None
        try:
            from openai import AsyncOpenAI
            base = self._normalize_openai_base_url(
                self.config.get(f"{prefix}_API_BASE", "") or "https://api.openai.com/v1"
            )
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            # A short-lived owned client also handles configuration changes safely.
            async with AsyncOpenAI(api_key=api_key, base_url=base, timeout=90,
                                   max_retries=0, **openai_proxy_kwargs(self.config)) as client:
                response = await client.chat.completions.create(
                    model=model, messages=messages, max_tokens=max_tokens
                )
            text = (response.choices[0].message.content or "").strip() if response.choices else ""
            if not text:
                raise ValueError("模型返回空内容")
            self._last_llm_error = ""
            await circuit._record_llm_success(mode)
            return text
        except asyncio.CancelledError:
            if mode == "probe":
                await circuit._release_llm_probe()
            raise
        except Exception as exc:
            # API credentials or credential-bearing endpoints must not reach logs.
            self._last_llm_error = f"{prefix} 独立 API 请求失败：{exception_detail(exc, self.config)}"
            set_call_diagnostic(reason=self._last_llm_error, route="independent_api", model=safe_detail(model, self.config, 100))
            logger.warning(f"[BiliBot] {self._last_llm_error}")
            await circuit._record_llm_failure(mode, self._last_llm_error)
            return None

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

    async def _release_llm_probe(self):
        """Release a half-open probe slot without recording success or failure.

        Used when a probe call is cancelled: the circuit stays open for its
        remaining cooldown, but the next call is allowed to probe again instead
        of being skipped forever.
        """
        async with self._llm_circuit_lock_obj():
            self._llm_circuit_half_open = False

    async def _llm_call(self, prompt, system_prompt="", max_tokens=300, provider_id=None, *, _circuit=None):
        self._last_llm_error = ""
        set_call_diagnostic()
        circuit = _circuit if _circuit is not None else self
        circuit_mode = await circuit._enter_llm_circuit()
        if circuit_mode is None:
            self._last_llm_error = getattr(circuit, "_last_llm_error", "模型暂不可用")
            set_call_diagnostic(reason=self._last_llm_error, stage="circuit")
            return None
        try:
            pid = self._resolve_chat_provider_id(provider_id)
            set_call_diagnostic(route="astrbot_provider", provider=safe_detail(pid, self.config, 100))
            if not pid:
                reason = "未找到可用的默认对话模型"
                self._last_llm_error = reason
                set_call_diagnostic(reason=reason, route="astrbot_provider", provider=safe_detail(pid, self.config, 100))
                logger.error(f"[BiliBot] LLM 调用失败：{reason}，请检查 AstrBot 的默认聊天模型配置")
                await circuit._record_llm_failure(circuit_mode, reason)
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
                set_call_diagnostic(reason=reason, route="astrbot_provider", provider=safe_detail(pid, self.config, 100))
                logger.error(f"[BiliBot] LLM 调用失败：{reason}")
                await circuit._record_llm_failure(circuit_mode, reason)
                return None
            await circuit._record_llm_success(circuit_mode)
            return text
        except asyncio.CancelledError:
            # 被取消的探测既不算成功也不算失败；复位半开标志，
            # 否则后续所有调用会被永久跳过（审计发现 F4）。
            if circuit_mode == "probe":
                await circuit._release_llm_probe()
            raise
        except Exception as e:
            self._last_llm_error = exception_detail(e, self.config)
            set_call_diagnostic(reason=self._last_llm_error, route="astrbot_provider", provider=safe_detail(locals().get("pid", ""), self.config, 100))
            logger.error(f"[BiliBot] LLM 调用失败: {self._last_llm_error}")
            await circuit._record_llm_failure(circuit_mode, self._last_llm_error)
            return None

    def _persona_selection(self):
        """解析「使用 AstrBot 人设」配置。

        兼容三种历史写法：
        - ``True`` / ``False``：旧布尔开关，True 表示跟随 AstrBot 默认人格；
        - ``""``：跟随 AstrBot 默认人格；
        - ``"<persona_id>"``：指定人格，``"__off__"`` 表示关闭。

        Returns:
            (是否启用, 人格 ID；空字符串代表跟随默认人格)
        """
        raw = self.config.get("USE_ASTRBOT_PERSONA", True)
        if isinstance(raw, bool):
            return raw, ""
        value = str(raw or "").strip()
        if value.lower() in {"off", "__off__", "false", "0", "none"}:
            return False, ""
        # 空字符串表示「跟随 AstrBot 默认人格」，即启用但不指定具体人格。
        return True, value

    async def _get_system_prompt(self):
        base_prompt = ""
        enabled, persona_id = self._persona_selection()
        if enabled:
            try:
                manager = self.context.persona_manager
                persona = None
                if persona_id:
                    persona = manager.get_persona_v3_by_id(persona_id)
                    if persona is None:
                        logger.warning(
                            f"[BiliBot] 找不到人格 {persona_id}，回退 AstrBot 默认人设"
                        )
                if persona is None:
                    persona = await manager.get_default_persona_v3()
                if persona and persona.get("prompt"):
                    base_prompt = str(persona["prompt"]).strip()
            except Exception as e:
                logger.warning(f"[BiliBot] 读取AstrBot自带人设失败，将使用B站附加提示词: {e}")
        addon = str(self.config.get("CUSTOM_SYSTEM_PROMPT", "") or "").strip()
        if base_prompt and addon:
            return f"{base_prompt}\n\n【B站活动附加设定】\n{addon}"
        return base_prompt or addon or "你是一个活跃在B站的角色，会回复评论、看视频、发动态。用自然的口语化风格交流。"
