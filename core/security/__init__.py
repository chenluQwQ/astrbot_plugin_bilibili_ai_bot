"""安全层：隔离工具、会话、记忆与账号能力。

对应 issue #7。四条主线：

- ``scopes``：会话域与记忆域，定义谁能读谁、谁能写谁。
- ``identity``：ID namespace 强制与角色解析，管理员身份只来自平台字段。
- ``redact``：入口风险评分、出口凭据脱敏、工具输出限长。
- ``capability`` + ``toolgate``：工具三分、一次性写票据、原始消息确认。
"""

from .capability import (
    ActionDigest,
    Capability,
    CapabilityError,
    CapabilityStore,
    PendingConfirmations,
    build_digest,
    cancel_from_message,
    confirm_from_message,
)
from .identity import (
    Caller,
    IdentityResolver,
    Role,
    bili_id,
    normalize_id,
    qq_id,
    raw_uid,
    system_caller,
)
from .redact import (
    SanitizeResult,
    clip_tool_output,
    contains_credentials,
    redact_for_ui,
    redact_outbound,
    sanitize_inbound,
    wrap_untrusted,
)
from .scopes import (
    Scope,
    SessionKey,
    can_read,
    can_write,
    comment_session,
    dm_session,
    is_untrusted,
    live_session,
    media_session,
    policy_for,
    readable_scopes,
    scope_for_source,
)
from .toolgate import (
    Decision,
    Tier,
    ToolGate,
    ToolSpec,
    persona_admin,
    private_read,
    public_read,
    write_tool,
)

__all__ = [
    "ActionDigest", "Capability", "CapabilityError", "CapabilityStore",
    "PendingConfirmations", "build_digest", "cancel_from_message",
    "confirm_from_message",
    "Caller", "IdentityResolver", "Role", "bili_id", "normalize_id", "qq_id",
    "raw_uid", "system_caller",
    "SanitizeResult", "clip_tool_output", "contains_credentials",
    "redact_for_ui", "redact_outbound", "sanitize_inbound", "wrap_untrusted",
    "Scope", "SessionKey", "can_read", "can_write", "comment_session",
    "dm_session", "is_untrusted", "live_session", "media_session",
    "policy_for", "readable_scopes", "scope_for_source",
    "Decision", "Tier", "ToolGate", "ToolSpec", "persona_admin",
    "private_read", "public_read", "write_tool",
]
