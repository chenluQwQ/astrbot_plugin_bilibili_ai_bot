"""会话域与记忆域定义。

这是安全层最基础的词汇表：所有事件、记忆、工具调用都必须落在某个 scope 上，
跨 scope 读取必须显式授权。旧实现把所有来源塞进一个 memory.json 并用无过滤的
全局语义检索召回，导致 B 站公开评论能读到主人私信内容——本模块就是为了从
类型层面消灭那种可能。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Scope(str, Enum):
    """会话/记忆域。值同时用作数据库字段，改名需要迁移。"""

    COMMENT = "bili_comment"  # B 站公开评论区（不可信）
    DM = "bili_dm"  # B 站私信（半可信，仍是外部输入）
    LIVE = "bili_live"  # 直播弹幕（不可信）
    QQ_GROUP = "qq_group"  # QQ 群聊
    QQ_PRIVATE = "qq_private"  # QQ 私聊
    ADMIN = "admin"  # 管理员控制通道
    BACKGROUND = "background"  # 后台任务：日报、清算
    PROACTIVE = "proactive"  # 主动行为：看片、动态
    SELF = "self"  # 角色自我认知（永久记忆）
    ANALYTICS = "analytics"  # 评论洞察、趋势分析

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return self.value


#: 外部不可信来源。这些 scope 的内容永远视为潜在注入载荷。
UNTRUSTED_SCOPES: frozenset[Scope] = frozenset(
    {Scope.COMMENT, Scope.DM, Scope.LIVE, Scope.QQ_GROUP}
)

#: 只有这些 scope 能触发写操作审批流程。
ADMIN_SCOPES: frozenset[Scope] = frozenset({Scope.ADMIN, Scope.QQ_PRIVATE})


@dataclass(frozen=True)
class ScopePolicy:
    """单个 scope 的读写策略。

    ``readable`` 是该 scope 生成回复时允许检索的记忆域白名单。
    注意它不是对称关系：私信可以读评论区留下的公开印象，
    但评论区读不到私信内容。
    """

    scope: Scope
    readable: frozenset[Scope]
    writable: frozenset[Scope]
    allow_tools: bool = False
    allow_private_read: bool = False
    allow_write_tools: bool = False
    #: 该 scope 的原始正文在 WebUI 是否默认隐藏
    redact_in_ui: bool = True


def _policy(
    scope: Scope,
    readable: set[Scope],
    writable: set[Scope],
    **kwargs: bool,
) -> ScopePolicy:
    return ScopePolicy(
        scope=scope,
        readable=frozenset(readable),
        writable=frozenset(writable),
        **kwargs,
    )


#: 默认策略表。核心原则：
#: - 不可信 scope 默认不能用工具、不能读账号私密数据、不能写。
#: - 每个 scope 只能写自己的记忆域，SELF（自我认知）只有管理员和后台能写。
#: - 读权限是有向的，外部 scope 读不到彼此，也读不到 ADMIN/SELF 私密内容。
SCOPE_POLICIES: dict[Scope, ScopePolicy] = {
    Scope.COMMENT: _policy(
        Scope.COMMENT,
        readable={Scope.COMMENT},
        writable={Scope.COMMENT},
    ),
    Scope.DM: _policy(
        Scope.DM,
        # 私信可以读该用户在评论区的公开互动，帮助保持人物连续性。
        readable={Scope.DM, Scope.COMMENT},
        writable={Scope.DM},
    ),
    Scope.LIVE: _policy(
        Scope.LIVE,
        readable={Scope.LIVE},
        writable={Scope.LIVE},
    ),
    Scope.QQ_GROUP: _policy(
        Scope.QQ_GROUP,
        readable={Scope.QQ_GROUP},
        writable={Scope.QQ_GROUP},
        allow_tools=True,
    ),
    # QQ 私聊是 bot 与主人交流的现实通道，因此与管理面板同为受信通道。
    # 注意"受信"只解除通道限制，不解除身份限制：调用者仍须是
    # ADMIN_QQ_IDS 里的管理员（见 identity.Caller.is_admin）。
    Scope.QQ_PRIVATE: _policy(
        Scope.QQ_PRIVATE,
        readable={Scope.QQ_PRIVATE, Scope.QQ_GROUP, Scope.SELF, Scope.ANALYTICS},
        writable={Scope.QQ_PRIVATE},
        allow_tools=True,
        allow_private_read=True,
        allow_write_tools=True,
        redact_in_ui=False,
    ),
    Scope.ADMIN: _policy(
        Scope.ADMIN,
        readable=set(Scope),
        writable={Scope.ADMIN, Scope.SELF},
        allow_tools=True,
        allow_private_read=True,
        allow_write_tools=True,
        redact_in_ui=False,
    ),
    Scope.BACKGROUND: _policy(
        Scope.BACKGROUND,
        readable=set(Scope) - {Scope.QQ_PRIVATE},
        writable={Scope.BACKGROUND, Scope.ANALYTICS, Scope.SELF},
        allow_private_read=True,
        redact_in_ui=False,
    ),
    Scope.PROACTIVE: _policy(
        Scope.PROACTIVE,
        readable={Scope.PROACTIVE, Scope.SELF, Scope.ANALYTICS, Scope.COMMENT},
        writable={Scope.PROACTIVE},
        allow_private_read=True,
        redact_in_ui=False,
    ),
    Scope.SELF: _policy(
        Scope.SELF,
        readable={Scope.SELF},
        writable=set(),
        redact_in_ui=False,
    ),
    Scope.ANALYTICS: _policy(
        Scope.ANALYTICS,
        readable={Scope.ANALYTICS, Scope.COMMENT, Scope.LIVE},
        writable={Scope.ANALYTICS},
        redact_in_ui=False,
    ),
}


def policy_for(scope: Scope | str) -> ScopePolicy:
    """取 scope 策略；未知 scope 退化为最严格策略（什么都不许）。"""
    resolved = coerce_scope(scope)
    if resolved is None:
        return ScopePolicy(
            scope=Scope.COMMENT, readable=frozenset(), writable=frozenset()
        )
    return SCOPE_POLICIES[resolved]


def coerce_scope(scope: Scope | str | None) -> Scope | None:
    if isinstance(scope, Scope):
        return scope
    if not scope:
        return None
    try:
        return Scope(str(scope))
    except ValueError:
        return None


def readable_scopes(scope: Scope | str) -> frozenset[Scope]:
    """该 scope 检索记忆时允许命中的域。"""
    return policy_for(scope).readable


def can_read(reader: Scope | str, target: Scope | str) -> bool:
    resolved = coerce_scope(target)
    return resolved is not None and resolved in readable_scopes(reader)


def can_write(writer: Scope | str, target: Scope | str) -> bool:
    resolved = coerce_scope(target)
    return resolved is not None and resolved in policy_for(writer).writable


def is_untrusted(scope: Scope | str) -> bool:
    resolved = coerce_scope(scope)
    return resolved is None or resolved in UNTRUSTED_SCOPES


def is_admin_scope(scope: Scope | str) -> bool:
    return coerce_scope(scope) in ADMIN_SCOPES


#: 事件来源 → scope 映射。适配层用它把平台事件放进正确的域。
SOURCE_SCOPE_MAP: dict[str, Scope] = {
    "comment": Scope.COMMENT,
    "at": Scope.COMMENT,
    "reply": Scope.COMMENT,
    "dm": Scope.DM,
    "private": Scope.DM,
    "danmaku": Scope.LIVE,
    "live": Scope.LIVE,
    "qq_share": Scope.QQ_GROUP,
    "qq_group": Scope.QQ_GROUP,
    "qq_private": Scope.QQ_PRIVATE,
    "admin": Scope.ADMIN,
    "proactive": Scope.PROACTIVE,
    "background": Scope.BACKGROUND,
}


def scope_for_source(source: str) -> Scope:
    return SOURCE_SCOPE_MAP.get((source or "").strip().lower(), Scope.COMMENT)


@dataclass
class SessionKey:
    """会话标识。域 + 域内键，避免不同来源的数字 ID 撞车。"""

    scope: Scope
    key: str
    account_id: str = "default"
    extra: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        return f"{self.account_id}!{self.scope.value}!{self.key}"

    @staticmethod
    def parse(raw: str) -> "SessionKey | None":
        parts = (raw or "").split("!", 2)
        if len(parts) != 3:
            return None
        scope = coerce_scope(parts[1])
        if scope is None:
            return None
        return SessionKey(scope=scope, key=parts[2], account_id=parts[0])


def comment_session(bvid: str, root_rpid: str, account_id: str = "default") -> str:
    return SessionKey(
        Scope.COMMENT, f"video:{bvid}:thread:{root_rpid}", account_id
    ).render()


def dm_session(uid: str, account_id: str = "default") -> str:
    return SessionKey(Scope.DM, f"uid:{uid}", account_id).render()


def live_session(room_id: str, account_id: str = "default") -> str:
    return SessionKey(Scope.LIVE, f"room:{room_id}", account_id).render()


def media_session(kind: str, ref: str, account_id: str = "default") -> str:
    """媒体理解专用会话。每个视频/图文独立，任务结束即弃，避免视觉上下文叠加。"""
    return SessionKey(Scope.BACKGROUND, f"media:{kind}:{ref}", account_id).render()
