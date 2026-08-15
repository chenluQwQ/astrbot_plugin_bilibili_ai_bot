"""工具三分与注册闸门。

与旧实现的关键差别：

- 工具能力由**声明式 metadata** 决定（``tier`` / ``read_only``），
  不再靠工具名或描述关键词猜测。
- 不该给的工具**不构造、不注册、不出现在 ToolSet**，而不是注册后
  在调用时返回一句"已关闭"。模型看不见的工具才是真的关掉了。
- 每次调用都经过 ``authorize``，拒绝理由写入审计。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable

from .identity import Caller, Role
from .scopes import Scope, policy_for


class Tier(str, Enum):
    """工具能力层级。"""

    PUBLIC_READ = "public_read"  # 公开数据，无账号身份
    PRIVATE_READ = "private_read"  # 需要账号凭据的读取
    WRITE = "write"  # 改变平台状态或发出内容
    PERSONA_ADMIN = "persona_admin"  # 记忆/人格/画像管理，永不对外


#: 各层级要求的最低角色。
_MIN_ROLE: dict[Tier, Role] = {
    Tier.PUBLIC_READ: Role.STRANGER,
    Tier.PRIVATE_READ: Role.ADMIN,
    Tier.WRITE: Role.ADMIN,
    Tier.PERSONA_ADMIN: Role.ADMIN,
}


@dataclass(frozen=True)
class ToolSpec:
    """工具声明。``handler`` 是普通 async 函数，不需要是 FunctionTool 子类。"""

    name: str
    tier: Tier
    description: str
    handler: Callable[..., Any]
    parameters: dict[str, Any] = field(default_factory=dict)
    #: 只读工具可安全并发、可缓存；写工具反之。
    read_only: bool = True
    #: 需要一次性 capability 才能执行。
    needs_capability: bool = False
    #: 允许的 scope 白名单；空集表示按 tier 默认策略判断。
    allowed_scopes: frozenset[Scope] = frozenset()
    #: 单次输出上限，统一由闸门截断。
    output_limit: int = 1500
    #: 预估消耗，供行为预算参考。
    cost_weight: float = 1.0

    def __post_init__(self) -> None:
        if self.tier in (Tier.WRITE,) and self.read_only:
            raise ValueError(f"write tool {self.name} must not be read_only")
        if self.tier is Tier.WRITE and not self.needs_capability:
            raise ValueError(f"write tool {self.name} must require capability")


@dataclass
class Decision:
    """授权结果。"""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - 便于 if 判断
        return self.allowed


class ToolGate:
    """工具注册与授权中心。"""

    def __init__(self, config_getter, audit=None, clock=time.time) -> None:
        self._get = config_getter
        self._audit = audit
        self._clock = clock
        self._specs: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------ 注册
    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def register_all(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(str(name or ""))

    def all_specs(self) -> list[ToolSpec]:
        return sorted(self._specs.values(), key=lambda s: (s.tier.value, s.name))

    # ------------------------------------------------------------ 开关
    def _write_enabled(self) -> bool:
        return bool(self._get("SECURITY_ENABLE_WRITE_TOOLS", False))

    def _private_read_enabled(self) -> bool:
        return bool(self._get("SECURITY_ENABLE_PRIVATE_READ_TOOLS", False))

    def _external_allowlist(self) -> set[str]:
        raw = self._get("SECURITY_EXTERNAL_TOOL_ALLOWLIST", []) or []
        if isinstance(raw, str):
            raw = [x for x in raw.replace("，", ",").split(",")]
        return {str(x).strip() for x in raw if str(x).strip()}

    def _emergency_stop(self) -> bool:
        return bool(self._get("SECURITY_EMERGENCY_STOP", False))

    # ------------------------------------------------------------ 授权
    def authorize(self, spec: ToolSpec, caller: Caller) -> Decision:
        """判断某调用者能否使用某工具。顺序从最硬的条件开始。"""
        if self._emergency_stop() and not spec.read_only:
            return Decision(False, "全局紧急停止已开启，所有写操作已禁用")
        if caller.is_blocked:
            return Decision(False, "调用者在黑名单中")

        policy = policy_for(caller.scope)

        # PERSONA_ADMIN 永不对外开放，即使管理员也必须在受信通道。
        if spec.tier is Tier.PERSONA_ADMIN:
            if not caller.is_admin:
                return Decision(False, "记忆与人格管理工具仅管理员可用")
            return Decision(True)

        if spec.tier is Tier.WRITE:
            if not self._write_enabled():
                return Decision(False, "写工具未启用（SECURITY_ENABLE_WRITE_TOOLS）")
            if not policy.allow_write_tools:
                return Decision(False, f"通道 {caller.scope.value} 不允许写操作")
            if not caller.is_admin:
                return Decision(False, "写操作需要管理员在受信通道确认")
            return Decision(True)

        if spec.tier is Tier.PRIVATE_READ:
            if not self._private_read_enabled():
                return Decision(False, "账号私密读取工具未启用")
            if not policy.allow_private_read:
                return Decision(
                    False, f"通道 {caller.scope.value} 不允许读取账号私密数据"
                )
            if caller.role < _MIN_ROLE[Tier.PRIVATE_READ]:
                return Decision(False, "权限不足")
            return Decision(True)

        # PUBLIC_READ：不可信通道默认关闭，只放开显式 allowlist 中的工具。
        if spec.allowed_scopes and caller.scope not in spec.allowed_scopes:
            return Decision(False, f"该工具未对通道 {caller.scope.value} 开放")
        if not policy.allow_tools:
            if spec.name not in self._external_allowlist():
                return Decision(
                    False,
                    f"外部通道默认禁用工具；如需开放请把 {spec.name} 加入白名单",
                )
        return Decision(True)

    def visible_specs(self, caller: Caller) -> list[ToolSpec]:
        """该调用者实际能看到的工具。ToolSet 只按这个构造。"""
        return [s for s in self.all_specs() if self.authorize(s, caller).allowed]

    async def audit(
        self,
        kind: str,
        caller: Caller,
        tool: str = "",
        decision: str = "",
        detail: str = "",
    ) -> None:
        if self._audit is None:
            return
        await self._audit(
            kind=kind,
            tool=tool,
            caller_id=caller.actor_id,
            session_id=caller.session_id,
            scope=caller.scope.value,
            decision=decision,
            detail=detail,
        )


def public_read(
    name: str,
    description: str,
    handler: Callable[..., Any],
    parameters: dict[str, Any] | None = None,
    **kwargs: Any,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        tier=Tier.PUBLIC_READ,
        description=description,
        handler=handler,
        parameters=parameters or {},
        read_only=True,
        **kwargs,
    )


def private_read(
    name: str,
    description: str,
    handler: Callable[..., Any],
    parameters: dict[str, Any] | None = None,
    **kwargs: Any,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        tier=Tier.PRIVATE_READ,
        description=description,
        handler=handler,
        parameters=parameters or {},
        read_only=True,
        **kwargs,
    )


def write_tool(
    name: str,
    description: str,
    handler: Callable[..., Any],
    parameters: dict[str, Any] | None = None,
    **kwargs: Any,
) -> ToolSpec:
    kwargs.setdefault("cost_weight", 3.0)
    return ToolSpec(
        name=name,
        tier=Tier.WRITE,
        description=description,
        handler=handler,
        parameters=parameters or {},
        read_only=False,
        needs_capability=True,
        **kwargs,
    )


def persona_admin(
    name: str,
    description: str,
    handler: Callable[..., Any],
    parameters: dict[str, Any] | None = None,
    **kwargs: Any,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        tier=Tier.PERSONA_ADMIN,
        description=description,
        handler=handler,
        parameters=parameters or {},
        read_only=True,
        **kwargs,
    )
