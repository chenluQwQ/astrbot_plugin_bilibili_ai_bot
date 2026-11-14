"""调用模型：选 provider、发请求、熔断。不懂业务，也不读写 ``data/``。

三种走法，都不重试，失败返回 None，原因放在 ``last_error``：
- 对话模型：走 AstrBot 的 ``context.llm_generate``，共用一个熔断器
- 总结 / 生成配了自己的 provider：同样走 AstrBot，但各用各的熔断器
- 总结 / 生成配了独立 API：插件自己发请求，也各用各的熔断器
"""
from __future__ import annotations

import asyncio
import enum
import time

from astrbot.api import logger

from ..settings import Settings
from ..video_diagnostics import exception_detail, safe_detail, set_call_diagnostic
from .text import normalize_openai_base_url

# ``CircuitBreaker.enter`` 的返回值，也是这次调用结束时要交回去的凭据。None 表示这次不许调。
MODE_DISABLED = "disabled"
MODE_CLOSED = "closed"
MODE_PROBE = "probe"


class CircuitState(enum.Enum):
    CLOSED = "closed"        # 正常
    OPEN = "open"            # 连续失败，冷却中，一律跳过
    HALF_OPEN = "half_open"  # 冷却完了，只放一个探测过去


class _CallTimeout(Exception):
    """整次调用超过我们给的总时限（不是 AstrBot 或 provider 里面自己的超时）。"""


async def _within(awaitable, timeout):
    if not timeout:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except asyncio.TimeoutError as exc:
        raise _CallTimeout() from exc


class CircuitBreaker:
    """连续失败到阈值就停一阵，冷却后只放一个探测。

    不在这里重试：后台任务很多，模型服务恢复时不能一拥而上。
    """

    def __init__(self, settings):
        self._settings = settings
        self._lock = asyncio.Lock()
        self.failures = 0
        self.open_until = 0.0
        self.probing = False
        self.skip_reason = ""
        self._last_skip_log = 0.0

    def state(self, now=None) -> CircuitState:
        now = time.monotonic() if now is None else now
        if self.open_until <= 0:
            return CircuitState.CLOSED
        return CircuitState.OPEN if self.open_until > now else CircuitState.HALF_OPEN

    def _limits(self):
        """失败阈值和冷却秒数；配置填坏了也不能让调用报错。"""
        settings = self._settings()
        try:
            threshold = max(int(settings.get("LLM_CIRCUIT_FAILURE_THRESHOLD") or 0), 0)
        except (TypeError, ValueError):
            threshold = 5
        try:
            cooldown = max(float(settings.get("LLM_CIRCUIT_COOLDOWN_SECONDS") or 120), 1.0)
        except (TypeError, ValueError):
            cooldown = 120.0
        return threshold, cooldown

    async def enter(self):
        """申请调用一次。"""
        threshold, _ = self._limits()
        if threshold <= 0:
            return MODE_DISABLED
        should_log = False
        async with self._lock:
            now = time.monotonic()
            state = self.state(now)
            if state is CircuitState.CLOSED:
                return MODE_CLOSED
            if state is CircuitState.HALF_OPEN:
                if not self.probing:
                    self.probing = True
                    return MODE_PROBE
                self.skip_reason = "模型服务正在进行恢复探测，本次调用已跳过以避免重复请求"
                return None
            if now - self._last_skip_log >= 30:
                self._last_skip_log = now
                should_log = True
            remaining = max(1, int(self.open_until - now))
            self.skip_reason = f"模型服务连续失败后正在冷却，约 {remaining} 秒后自动探测"
        if should_log:
            logger.warning(f"[BiliBot] LLM 熔断中，本次调用已跳过；约 {remaining} 秒后允许一次探测")
        return None

    async def success(self, mode):
        if mode == MODE_DISABLED:
            return
        async with self._lock:
            # 打开之前就发出去的请求现在才成功，不能把熔断关掉；只有探测可以。
            if mode != MODE_PROBE and self.open_until > 0:
                return
            self.failures = 0
            self.open_until = 0.0
            self.probing = False

    async def failure(self, mode, reason):
        threshold, cooldown = self._limits()
        if mode == MODE_DISABLED or threshold <= 0:
            return
        opened = False
        async with self._lock:
            now = time.monotonic()
            self.failures += 1
            failures = self.failures
            if mode == MODE_PROBE or (failures >= threshold and self.open_until <= now):
                self.open_until = now + cooldown
                self.probing = False
                opened = True
        if opened:
            logger.warning(
                f"[BiliBot] LLM 连续失败 {failures} 次，熔断 {int(cooldown)} 秒；"
                f"期间不会重复申请，原因：{reason}"
            )

    async def abandon(self, mode):
        """探测被取消：不算成功也不算失败，下一次调用可以再探测，否则会被永久跳过。"""
        if mode != MODE_PROBE:
            return
        async with self._lock:
            self.probing = False


class LLMGateway:
    """``config`` 和 ``context`` 传函数：AstrBot 的配置对象可能被换掉，每次现取。"""

    def __init__(self, config, context):
        self._config = config
        self._context = context
        self.last_error = ""
        self.circuit = CircuitBreaker(self._settings)
        self._purpose_circuits = {}

    def _settings(self):
        return Settings(self._config())

    def purpose_circuit(self, purpose):
        if purpose not in self._purpose_circuits:
            self._purpose_circuits[purpose] = CircuitBreaker(self._settings)
        return self._purpose_circuits[purpose]

    def resolve_provider(self, provider_id=None):
        """明确指定的 → 插件配置的 → AstrBot 默认的。

        AstrBot 4.27 起 ``llm_generate`` 必须带 ``chat_provider_id``；后台任务没有消息会话，
        只能问 context 要全局默认的那个。
        """
        if provider_id is not None and str(provider_id).strip():
            return str(provider_id).strip()

        configured = self._settings().get("LLM_PROVIDER_ID")
        if configured and str(configured).strip() and str(configured).strip().lower() != "default":
            return str(configured).strip()

        context = self._context()
        getter = getattr(context, "get_using_provider", None)
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

        # 兼容旧版本和测试里的 Context。
        providers_getter = getattr(context, "get_all_providers", None)
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

    async def call(self, prompt, system_prompt="", max_tokens=300, provider_id=None, *, circuit=None, timeout=None):
        """走 AstrBot 的 provider 调一次。"""
        def generate(pid):
            # 人设走真正的 system role：① 增强人设遵循 ② 让人设成为稳定前缀，命中提示词缓存
            kwargs = {"prompt": prompt, "max_tokens": max_tokens, "chat_provider_id": pid}
            if system_prompt:
                kwargs["system_prompt"] = system_prompt
            return self._context().llm_generate(**kwargs)

        return await self._astrbot_call(generate, provider_id, circuit, timeout)

    async def tool_loop(self, prompt, system_prompt="", *, event, tools, max_steps,
                        tool_call_timeout=120, provider_id=None, circuit=None, timeout=None, max_tokens=900):
        """走 AstrBot 的工具循环：模型可以先调 ``tools`` 里的工具，最后给出回复文字。

        只放进传进来的工具；``max_steps`` 轮之后 AstrBot 会拔掉工具逼模型收尾。
        provider 没勾工具调用能力时 AstrBot 自己会拔掉工具，等于直接回复。没有工具就是普通调用。
        ``timeout`` 是整轮的总时限：AstrBot 里面的重试也算在内，到点没完就算一次失败，记进熔断。
        """
        if not tools:
            return await self.call(prompt, system_prompt=system_prompt, max_tokens=max_tokens,
                                   provider_id=provider_id, circuit=circuit, timeout=timeout)
        from astrbot.core.agent.tool import ToolSet

        def generate(pid):
            return self._context().tool_loop_agent(
                event=event, chat_provider_id=pid, prompt=prompt,
                system_prompt=system_prompt or None, tools=ToolSet(tools=list(tools)),
                max_steps=max(1, int(max_steps)), tool_call_timeout=tool_call_timeout,
            )

        return await self._astrbot_call(generate, provider_id, circuit, timeout)

    async def _astrbot_call(self, generate, provider_id, circuit, timeout=None):
        """熔断、选 provider、空回复和异常的处理，普通调用和工具循环共用。"""
        config = self._config()
        self.last_error = ""
        set_call_diagnostic()
        circuit = circuit if circuit is not None else self.circuit
        mode = await circuit.enter()
        if mode is None:
            self.last_error = circuit.skip_reason or "模型暂不可用"
            set_call_diagnostic(reason=self.last_error, stage="circuit")
            return None
        pid = ""
        try:
            pid = self.resolve_provider(provider_id)
            set_call_diagnostic(route="astrbot_provider", provider=safe_detail(pid, config, 100))
            if not pid:
                reason = "未找到可用的默认对话模型"
                self.last_error = reason
                set_call_diagnostic(reason=reason, route="astrbot_provider", provider=safe_detail(pid, config, 100))
                logger.error(f"[BiliBot] LLM 调用失败：{reason}，请检查 AstrBot 的默认聊天模型配置")
                await circuit.failure(mode, reason)
                return None
            resp = await _within(generate(pid), timeout)
            text = resp.completion_text.strip() if resp and resp.completion_text else ""
            if not text:
                reason = "模型返回空内容"
                self.last_error = reason
                set_call_diagnostic(reason=reason, route="astrbot_provider", provider=safe_detail(pid, config, 100))
                logger.error(f"[BiliBot] LLM 调用失败：{reason}")
                await circuit.failure(mode, reason)
                return None
            await circuit.success(mode)
            return text
        except asyncio.CancelledError:
            await circuit.abandon(mode)
            raise
        except _CallTimeout:
            self.last_error = f"模型 {round(timeout)} 秒内没有完成"
            set_call_diagnostic(reason=self.last_error, route="astrbot_provider", provider=safe_detail(pid, config, 100))
            logger.error(f"[BiliBot] LLM 调用失败：{self.last_error}")
            await circuit.failure(mode, self.last_error)
            return None
        except Exception as e:
            self.last_error = exception_detail(e, config)
            set_call_diagnostic(reason=self.last_error, route="astrbot_provider", provider=safe_detail(pid, config, 100))
            logger.error(f"[BiliBot] LLM 调用失败: {self.last_error}")
            await circuit.failure(mode, self.last_error)
            return None

    def purpose_route(self, purpose):
        """这个用途实际走哪个模型：``("provider", ID)``、``("api", 地址, 模型名)``，找不到是 None。
        用来判断视频模型和写评论的模型是不是同一个。"""
        prefix = str(purpose).upper()
        settings = self._settings()
        provider = settings.str(f"{prefix}_PROVIDER_ID")
        if provider and provider.lower() != "default":
            return ("provider", provider)
        api_key = settings.str(f"{prefix}_API_KEY")
        if api_key:
            model = settings.str(f"{prefix}_MODEL")
            base = normalize_openai_base_url(settings.get(f"{prefix}_API_BASE") or "https://api.openai.com/v1")
            return ("api", base, model) if model else None
        pid = self.resolve_provider(None)
        return ("provider", pid) if pid else None

    async def purpose_tool_loop(self, purpose, prompt, system_prompt="", *, event, tools, max_steps,
                                tool_call_timeout=120, timeout=None, max_tokens=900):
        """用某个用途配的模型跑工具循环。配的是独立 API 时 AstrBot 的工具循环用不了，就不给工具、普通调用一次。"""
        prefix = str(purpose).upper()
        route = self.purpose_route(prefix)
        if route and route[0] == "api":
            return await self.purpose_call(prefix, prompt, system_prompt=system_prompt, max_tokens=max_tokens)
        provider = self._settings().str(f"{prefix}_PROVIDER_ID")
        explicit = provider if provider and provider.lower() != "default" else None
        return await self.tool_loop(
            prompt, system_prompt, event=event, tools=tools, max_steps=max_steps,
            tool_call_timeout=tool_call_timeout, provider_id=explicit,
            circuit=self.purpose_circuit(prefix) if explicit else None, timeout=timeout, max_tokens=max_tokens,
        )

    async def purpose_call(self, purpose, prompt, system_prompt="", max_tokens=600):
        """总结和生成：事实和人设写作可以分开配模型，没配就用对话模型。"""
        prefix = str(purpose).upper()
        set_call_diagnostic()
        settings = self._settings()
        provider = settings.str(f"{prefix}_PROVIDER_ID")
        api_key = settings.str(f"{prefix}_API_KEY")
        model = settings.str(f"{prefix}_MODEL")
        if provider and provider.lower() != "default":
            return await self.call(prompt, system_prompt=system_prompt, max_tokens=max_tokens,
                                   provider_id=provider, circuit=self.purpose_circuit(prefix))
        if not api_key:
            return await self.call(prompt, system_prompt=system_prompt, max_tokens=max_tokens)
        if not model:
            self.last_error = f"{prefix} 独立 API 未填写模型名"
            set_call_diagnostic(reason=self.last_error, route="independent_api", purpose=prefix)
            logger.warning(f"[BiliBot] {self.last_error}")
            return None
        return await self._independent_call(prefix, api_key, model, prompt, system_prompt, max_tokens)

    async def _independent_call(self, prefix, api_key, model, prompt, system_prompt, max_tokens):
        from ..network import openai_proxy_kwargs
        config = self._config()
        circuit = self.purpose_circuit(prefix)
        set_call_diagnostic(route="independent_api", model=safe_detail(model, config, 100), purpose=prefix)
        mode = await circuit.enter()
        if mode is None:
            self.last_error = circuit.skip_reason or "模型暂不可用"
            set_call_diagnostic(reason=self.last_error, stage="circuit")
            return None
        try:
            from openai import AsyncOpenAI
            base = normalize_openai_base_url(
                self._settings().get(f"{prefix}_API_BASE") or "https://api.openai.com/v1"
            )
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            # 用完就关的 client：改了配置也不会用到旧的。
            async with AsyncOpenAI(api_key=api_key, base_url=base, timeout=90,
                                   max_retries=0, **openai_proxy_kwargs(config)) as client:
                response = await client.chat.completions.create(
                    model=model, messages=messages, max_tokens=max_tokens
                )
            text = (response.choices[0].message.content or "").strip() if response.choices else ""
            if not text:
                raise ValueError("模型返回空内容")
            self.last_error = ""
            await circuit.success(mode)
            return text
        except asyncio.CancelledError:
            await circuit.abandon(mode)
            raise
        except Exception as exc:
            # 密钥和带密钥的地址不能进日志。
            self.last_error = f"{prefix} 独立 API 请求失败：{exception_detail(exc, config)}"
            set_call_diagnostic(reason=self.last_error, route="independent_api", model=safe_detail(model, config, 100))
            logger.warning(f"[BiliBot] {self.last_error}")
            await circuit.failure(mode, self.last_error)
            return None
