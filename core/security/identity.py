"""身份归一化与调用者解析。

两个硬约束：

1. 用户 ID 必须带平台前缀（``bili:123`` / ``qq:456``），由本模块强制生成，
   绝不接受模型或外部输入自带的 ID 串。旧实现直接用裸数字 UID，导致
   同数字的 QQ 号与 B 站 UID 会共享画像和记忆。
2. 管理员身份只能来自平台事件的可信字段，不能来自消息正文。
   任何自称"我是主人"的文本都不会改变 caller 的角色。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

from .scopes import Scope, coerce_scope, is_admin_scope

_ID_SAFE = re.compile(r"[^0-9A-Za-z_\-]")

PLATFORM_BILI = "bili"
PLATFORM_QQ = "qq"
PLATFORM_SYSTEM = "sys"


class Role(IntEnum):
    """调用者角色。数值可比较，越大权限越高。"""

    BLOCKED = 0
    STRANGER = 10
    KNOWN = 20
    TRUSTED = 30
    ADMIN = 40
    SYSTEM = 50


def normalize_id(platform: str, raw: str | int) -> str:
    """生成带 namespace 的稳定 ID。非法字符一律剔除。"""
    plat = _ID_SAFE.sub("", str(platform or "")).lower() or "unknown"
    ident = _ID_SAFE.sub("", str(raw if raw is not None else ""))
    return f"{plat}:{ident}" if ident else f"{plat}:unknown"


def bili_id(uid: str | int) -> str:
    return normalize_id(PLATFORM_BILI, uid)


def qq_id(uid: str | int) -> str:
    return normalize_id(PLATFORM_QQ, uid)


def system_id(name: str) -> str:
    return normalize_id(PLATFORM_SYSTEM, name)


def split_id(actor_id: str) -> tuple[str, str]:
    """拆回 (platform, raw)。给不带前缀的历史数据兜底成 bili。"""
    text = str(actor_id or "")
    if ":" in text:
        platform, _, raw = text.partition(":")
        return platform.lower(), raw
    return PLATFORM_BILI, _ID_SAFE.sub("", text)


def raw_uid(actor_id: str) -> str:
    """取回平台原始 UID，用于调 B 站接口。"""
    return split_id(actor_id)[1]


def is_bili(actor_id: str) -> bool:
    return split_id(actor_id)[0] == PLATFORM_BILI


@dataclass(frozen=True)
class Caller:
    """一次请求的调用者身份。由适配层构造，下游只读。

    ``role`` 与 ``scope`` 共同决定能用哪些工具。两者都来自平台可信字段：
    role 由配置中的 owner/admin 名单比对得出，scope 由事件来源决定。
    """

    actor_id: str
    display_name: str = ""
    role: Role = Role.STRANGER
    scope: Scope = Scope.COMMENT
    account_id: str = "default"
    session_id: str = ""

    @property
    def is_admin(self) -> bool:
        """管理员身份需要角色与通道同时成立。

        只在受信通道（管理员面板、QQ 私聊）里承认管理员权限：
        即使主人在公开评论区留言，也不会在不可信通道获得写权限。
        """
        return self.role >= Role.ADMIN and is_admin_scope(self.scope)

    @property
    def is_blocked(self) -> bool:
        return self.role <= Role.BLOCKED

    @property
    def platform(self) -> str:
        return split_id(self.actor_id)[0]

    def with_scope(self, scope: Scope | str) -> "Caller":
        resolved = coerce_scope(scope) or self.scope
        return Caller(
            actor_id=self.actor_id,
            display_name=self.display_name,
            role=self.role,
            scope=resolved,
            account_id=self.account_id,
            session_id=self.session_id,
        )

    def describe(self) -> str:
        return f"{self.actor_id}({self.role.name}@{self.scope.value})"


def system_caller(name: str = "scheduler", scope: Scope = Scope.BACKGROUND) -> Caller:
    """后台任务的调用者。拥有 SYSTEM 角色但仍受 scope 策略约束。"""
    return Caller(
        actor_id=system_id(name),
        display_name=name,
        role=Role.SYSTEM,
        scope=scope,
        session_id=f"default!{scope.value}!{name}",
    )


class IdentityResolver:
    """按配置解析角色。

    配置来源（都只接受平台侧字段，不看消息正文）：
    - ``OWNER_MID``：B 站主人 UID
    - ``ADMIN_QQ_IDS``：QQ 管理员列表
    - ``TRUSTED_BILI_UIDS``：受信 B 站用户（可用公开只读工具）
    """

    def __init__(self, config_getter) -> None:
        self._get = config_getter

    def _list(self, key: str) -> set[str]:
        raw = self._get(key, []) or []
        if isinstance(raw, str):
            items: list[str] = re.split(r"[,\s，、]+", raw)
        else:
            items = [str(x) for x in raw]
        return {_ID_SAFE.sub("", i) for i in items if str(i).strip()}

    def owner_bili_uid(self) -> str:
        return _ID_SAFE.sub("", str(self._get("OWNER_MID", "") or ""))

    def role_for(self, actor_id: str, scope: Scope) -> Role:
        platform, raw = split_id(actor_id)
        if platform == PLATFORM_SYSTEM:
            return Role.SYSTEM
        if raw and raw in self._list("BLOCKED_UIDS"):
            return Role.BLOCKED
        if platform == PLATFORM_BILI:
            owner = self.owner_bili_uid()
            if owner and raw == owner:
                return Role.ADMIN
            if raw in self._list("TRUSTED_BILI_UIDS"):
                return Role.TRUSTED
        elif platform == PLATFORM_QQ:
            if raw in self._list("ADMIN_QQ_IDS"):
                return Role.ADMIN
            if raw in self._list("TRUSTED_QQ_IDS"):
                return Role.TRUSTED
        return Role.STRANGER

    def resolve(
        self,
        platform: str,
        raw_id: str | int,
        scope: Scope,
        display_name: str = "",
        account_id: str = "default",
        session_id: str = "",
    ) -> Caller:
        actor_id = normalize_id(platform, raw_id)
        return Caller(
            actor_id=actor_id,
            display_name=str(display_name or "")[:64],
            role=self.role_for(actor_id, scope),
            scope=scope,
            account_id=account_id,
            session_id=session_id,
        )
