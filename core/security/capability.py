"""写操作授权：action digest + 一次性 capability。

流程（对外可见的每一步都不可跳过）：

1. 模型请求写操作 → ``build_digest`` 把 工具/账号/目标/参数 标准化成摘要。
2. 系统向管理员展示这份摘要，等待确认。
3. 确认必须来自**用户原始消息**（``confirm_from_message``），
   模型自己生成的确认词一律无效——这是与旧实现最关键的差别。
4. 确认通过 → ``issue`` 签发短时一次性 token，绑定 digest/账号/调用者/会话。
5. 执行前 ``consume`` 原子核销；同一 token 不可能用两次。

任何环节缺失，写操作就执行不了。工具层不持有绕过入口。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .identity import Caller, Role
from .scopes import is_admin_scope

#: 默认票据有效期。够管理员看清摘要，又短到不适合被囤积。
DEFAULT_TTL = 180.0

#: 确认词。必须整条消息就是这些词之一（或以之开头），避免正文里
#: 偶然出现"确认"两字就被当成授权。
_CONFIRM_WORDS = (
    "确认",
    "确认执行",
    "同意",
    "可以",
    "执行",
    "批准",
    "confirm",
    "yes",
    "approve",
    "ok",
)
_CANCEL_WORDS = ("取消", "算了", "不要", "拒绝", "no", "cancel", "deny")

_CONFIRM_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(w) for w in _CONFIRM_WORDS) + r")\s*[!！。.]*\s*$",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(w) for w in _CANCEL_WORDS) + r")\s*[!！。.]*\s*$",
    re.IGNORECASE,
)


class CapabilityError(Exception):
    """授权失败。消息可直接展示给管理员。"""


def _stable_args(args: dict[str, Any]) -> str:
    """参数标准化：排序 + 去除空值 + 限长，保证同一意图得到同一摘要。"""
    cleaned: dict[str, Any] = {}
    for key in sorted(args or {}):
        value = args[key]
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        cleaned[str(key)] = str(value)[:200]
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True)


def args_hash(args: dict[str, Any]) -> str:
    return hashlib.sha256(_stable_args(args).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ActionDigest:
    """标准化写动作摘要。展示给管理员的就是这个，签发绑定的也是这个。"""

    tool: str
    account_id: str
    target: str
    args: dict[str, Any] = field(default_factory=dict)
    caller_id: str = ""
    session_id: str = ""
    summary: str = ""

    @property
    def args_hash(self) -> str:
        return args_hash(self.args)

    @property
    def digest(self) -> str:
        raw = "|".join(
            (
                self.tool,
                self.account_id,
                self.target,
                self.args_hash,
                self.caller_id,
                self.session_id,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]

    def render(self) -> str:
        """给人看的确认卡片。不含任何凭据。"""
        lines = [
            "请确认以下写操作：",
            f"· 动作：{self.tool}",
            f"· 账号：{self.account_id}",
            f"· 目标：{self.target or '（无）'}",
        ]
        if self.summary:
            lines.append(f"· 说明：{self.summary}")
        for key in sorted(self.args or {}):
            lines.append(f"· {key}：{str(self.args[key])[:80]}")
        lines.append(f"· 摘要：{self.digest[:12]}")
        lines.append("回复「确认」执行，回复「取消」放弃。")
        return "\n".join(lines)


def build_digest(
    tool: str,
    caller: Caller,
    target: str = "",
    args: dict[str, Any] | None = None,
    summary: str = "",
) -> ActionDigest:
    return ActionDigest(
        tool=str(tool or "").strip(),
        account_id=caller.account_id,
        target=str(target or "")[:120],
        args=dict(args or {}),
        caller_id=caller.actor_id,
        session_id=caller.session_id,
        summary=str(summary or "")[:200],
    )


def confirm_from_message(raw_user_message: str) -> bool:
    """判断这条**用户原始消息**是否构成确认。

    只接受整条消息就是确认词的情况。模型输出、工具返回、记忆内容
    都不会走到这里——调用方必须传入平台原始消息文本。
    """
    return bool(_CONFIRM_RE.match(str(raw_user_message or "")))


def cancel_from_message(raw_user_message: str) -> bool:
    return bool(_CANCEL_RE.match(str(raw_user_message or "")))


@dataclass
class Capability:
    """一次性写票据。"""

    token: str
    digest: str
    tool: str
    account_id: str
    caller_id: str
    session_id: str
    args_hash: str
    issued_at: float
    expires_at: float

    def is_expired(self, at: float | None = None) -> bool:
        return (at if at is not None else time.time()) >= self.expires_at


class CapabilityStore:
    """票据签发与核销。持久化在 SQLite，重启后未用的票据依然有效直到过期。"""

    def __init__(self, db, clock=time.time) -> None:
        self._db = db
        self._clock = clock

    async def issue(
        self,
        digest: ActionDigest,
        caller: Caller,
        ttl: float = DEFAULT_TTL,
        confirmed_message: str | None = None,
    ) -> Capability:
        """签发票据。

        ``confirmed_message`` 必须是用户原始消息；缺失或不是确认词则拒签。
        管理员身份与受信通道两个条件都要成立。
        """
        if caller.role < Role.ADMIN:
            raise CapabilityError("写操作需要管理员权限")
        if not is_admin_scope(caller.scope):
            raise CapabilityError(
                f"通道 {caller.scope.value} 不允许发起写操作，请在管理员通道确认"
            )
        if confirmed_message is None or not confirm_from_message(confirmed_message):
            raise CapabilityError("需要管理员在原始消息中回复「确认」")

        now = self._clock()
        cap = Capability(
            token=secrets.token_urlsafe(24),
            digest=digest.digest,
            tool=digest.tool,
            account_id=digest.account_id,
            caller_id=caller.actor_id,
            session_id=caller.session_id,
            args_hash=digest.args_hash,
            issued_at=now,
            expires_at=now + max(30.0, float(ttl)),
        )
        await self._db.execute(
            "INSERT INTO capabilities(token,digest,tool,account_id,caller_id,"
            "session_id,args_hash,issued_at,expires_at,state) "
            "VALUES(?,?,?,?,?,?,?,?,?,'issued')",
            (
                cap.token,
                cap.digest,
                cap.tool,
                cap.account_id,
                cap.caller_id,
                cap.session_id,
                cap.args_hash,
                cap.issued_at,
                cap.expires_at,
            ),
        )
        return cap

    async def consume(self, token: str, digest: ActionDigest) -> Capability:
        """原子核销。token 与 digest 必须完全对得上，且只能用一次。"""
        now = self._clock()
        expected = digest.digest

        def _consume(conn):
            row = conn.execute(
                "SELECT * FROM capabilities WHERE token=?", (str(token or ""),)
            ).fetchone()
            if row is None:
                raise CapabilityError("授权票据不存在")
            if row["state"] != "issued":
                raise CapabilityError(
                    f"授权票据已{'使用' if row['state'] == 'consumed' else '失效'}"
                )
            if row["expires_at"] <= now:
                conn.execute(
                    "UPDATE capabilities SET state='expired' WHERE token=?",
                    (row["token"],),
                )
                raise CapabilityError("授权票据已过期，请重新确认")
            if row["digest"] != expected:
                raise CapabilityError("授权内容与请求不一致，已拒绝")
            conn.execute(
                "UPDATE capabilities SET state='consumed', consumed_at=? WHERE token=?",
                (now, row["token"]),
            )
            return dict(row)

        row = await self._db.run(_consume)
        return Capability(
            token=row["token"],
            digest=row["digest"],
            tool=row["tool"],
            account_id=row["account_id"],
            caller_id=row["caller_id"],
            session_id=row["session_id"],
            args_hash=row["args_hash"],
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
        )

    async def revoke_all(self, reason: str = "manual") -> int:
        """紧急停止用：作废所有未使用票据。"""
        return await self._db.execute(
            "UPDATE capabilities SET state='revoked' WHERE state='issued'"
        )

    async def purge_expired(self) -> int:
        return await self._db.execute(
            "DELETE FROM capabilities WHERE expires_at < ? AND state != 'issued'",
            (self._clock() - 86400,),
        )

    async def pending(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT token,tool,account_id,caller_id,issued_at,expires_at "
            "FROM capabilities WHERE state='issued' AND expires_at > ? "
            "ORDER BY issued_at DESC LIMIT ?",
            (self._clock(), int(limit)),
        )
        return [dict(r) for r in rows]


class PendingConfirmations:
    """等待确认的写请求。按会话保存，确认词只在同一会话生效。"""

    def __init__(self, db, ttl: float = DEFAULT_TTL, clock=time.time) -> None:
        self._db = db
        self._ttl = ttl
        self._clock = clock

    @staticmethod
    def _key(session_id: str) -> str:
        return f"pending_write:{session_id}"

    async def put(self, digest: ActionDigest) -> None:
        await self._db.kv_set(
            self._key(digest.session_id),
            {
                "tool": digest.tool,
                "account_id": digest.account_id,
                "target": digest.target,
                "args": digest.args,
                "caller_id": digest.caller_id,
                "session_id": digest.session_id,
                "summary": digest.summary,
                "at": self._clock(),
            },
            ttl=self._ttl,
        )

    async def take(self, session_id: str) -> ActionDigest | None:
        data = await self._db.kv_get(self._key(session_id))
        if not data:
            return None
        await self._db.kv_delete(self._key(session_id))
        return ActionDigest(
            tool=data.get("tool", ""),
            account_id=data.get("account_id", "default"),
            target=data.get("target", ""),
            args=data.get("args", {}) or {},
            caller_id=data.get("caller_id", ""),
            session_id=data.get("session_id", session_id),
            summary=data.get("summary", ""),
        )

    async def peek(self, session_id: str) -> dict[str, Any] | None:
        return await self._db.kv_get(self._key(session_id))
