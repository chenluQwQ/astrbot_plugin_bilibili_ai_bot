"""Unified, persistent budgets for externally visible Bot behaviour.

The old implementation counted replies, proactive watches and live danmaku in
different JSON files.  This gate turns those limits into atomic SQLite counter
reservations so concurrent pollers cannot all pass the same last free slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class BudgetReservation:
    bucket: str
    window_key: str
    amount: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "window_key": self.window_key,
            "amount": self.amount,
        }


class BehaviorBudget:
    """Build and atomically reserve the budgets that apply to an action."""

    def __init__(self, config_getter) -> None:
        self._get = config_getter

    # WebUI 的 *_DAILY_MAX 是当天硬上限。旧的 *_DAILY_LIMIT
    # 单值键保留兼容：优先读 MAX，缺失或仍为默认值时回退到
    # LIMIT，避免升级后突然放大限额。
    _DAILY_MAX_ALIASES = {
        "AUTONOMOUS_REPLY_DAILY_LIMIT": ("AUTONOMOUS_REPLY_DAILY_MAX", 80),
        "AUTONOMOUS_PRIVATE_DAILY_LIMIT": ("AUTONOMOUS_PRIVATE_DAILY_MAX", 30),
        "AUTONOMOUS_DYNAMIC_DAILY_LIMIT": ("AUTONOMOUS_DYNAMIC_DAILY_MAX", 2),
        "AUTONOMOUS_PROACTIVE_DAILY_LIMIT": ("AUTONOMOUS_PROACTIVE_DAILY_MAX", 4),
    }

    def _int(self, key: str, default: int = 0) -> int:
        alias = self._DAILY_MAX_ALIASES.get(key)
        if alias:
            max_key, schema_default = alias
            max_value = self._raw_int(max_key, schema_default)
            legacy_value = self._raw_int(key, default)
            if max_value != schema_default or legacy_value == default:
                return max_value
            return legacy_value
        return self._raw_int(key, default)

    def _raw_int(self, key: str, default: int = 0) -> int:
        try:
            return max(0, int(self._get(key, default) or 0))
        except (TypeError, ValueError):
            return max(0, int(default))

    @staticmethod
    def window_keys(at: float) -> tuple[str, str]:
        current = datetime.fromtimestamp(at)
        return current.strftime("%Y-%m-%d"), current.strftime("%Y-%m-%dT%H:%M")

    @staticmethod
    def _effective_limit(primary: int, secondary: int) -> int:
        if primary and secondary:
            return min(primary, secondary)
        return primary or secondary

    def rules_for(self, request, at: float) -> list[tuple[str, str, int]]:
        metadata = dict(getattr(request, "metadata", {}) or {})
        if not bool(self._get("BEHAVIOR_BUDGET_ENABLED", True)) or metadata.get(
            "budget_exempt"
        ):
            return []

        day_key, minute_key = self.window_keys(at)
        kind = str(getattr(request, "kind", "") or "").strip().lower()
        rules: list[tuple[str, str, int]] = []

        global_minute = self._int("BEHAVIOR_GLOBAL_MAX_PER_MINUTE", 12)
        global_daily = self._int("BEHAVIOR_GLOBAL_DAILY_LIMIT", 200)
        if global_minute:
            rules.append(("behavior:global:minute", minute_key, global_minute))
        if global_daily:
            rules.append(("behavior:global:day", day_key, global_daily))

        if kind == "comment_reply":
            limit = self._int("AUTONOMOUS_REPLY_DAILY_LIMIT", 80)
            if limit:
                rules.append(("behavior:comment_reply:day", day_key, limit))
        elif kind == "private_reply":
            limit = self._int("AUTONOMOUS_PRIVATE_DAILY_LIMIT", 30)
            if limit:
                rules.append(("behavior:private_reply:day", day_key, limit))
        elif kind == "live_reply":
            limit = self._int("LIVE_DANMAKU_MAX_PER_MINUTE", 4)
            if limit:
                rules.append(("behavior:live_reply:minute", minute_key, limit))
        elif kind == "post_dynamic":
            limit = self._effective_limit(
                self._int("DYNAMIC_DAILY_COUNT", 1),
                self._int("AUTONOMOUS_DYNAMIC_DAILY_LIMIT", 2),
            )
            if limit:
                rules.append(("behavior:post_dynamic:day", day_key, limit))
        elif kind == "proactive_watch":
            # One reservation equals one watched video. Browsing-round limits
            # belong to the scheduler and must not be mixed into this bucket.
            limit = self._int("PROACTIVE_DAILY_LIMIT", 0)
            if limit:
                rules.append(("behavior:proactive_watch:day", day_key, limit))
        elif kind == "proactive_comment":
            limit = self._int("PROACTIVE_COMMENT_DAILY_LIMIT", 2)
            if limit:
                rules.append(("behavior:proactive_comment:day", day_key, limit))
        return rules

    def reserve_in_transaction(
        self, conn, request, at: float
    ) -> tuple[bool, str, list[BudgetReservation]]:
        """Reserve every applicable bucket in the caller's transaction."""

        rules = self.rules_for(request, at)
        reservations: list[BudgetReservation] = []
        for bucket, window_key, limit in rules:
            row = conn.execute(
                "SELECT count FROM counters WHERE bucket=? AND window_key=?",
                (bucket, window_key),
            ).fetchone()
            used = float(row["count"] if row is not None else 0.0)
            if used + 1 > limit:
                return (
                    False,
                    f"budget_exhausted:{bucket}:{int(used)}/{limit}",
                    [],
                )
            reservations.append(BudgetReservation(bucket, window_key))

        for reservation in reservations:
            conn.execute(
                "INSERT INTO counters(bucket,window_key,count,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(bucket,window_key) DO UPDATE SET "
                "count=count+excluded.count,updated_at=excluded.updated_at",
                (
                    reservation.bucket,
                    reservation.window_key,
                    reservation.amount,
                    at,
                ),
            )
        return True, "", reservations

    @staticmethod
    def refund_in_transaction(conn, reservations: list[dict[str, Any]], at: float) -> None:
        """Refund a definitely failed action. Unknown sends deliberately keep budget."""

        for item in reservations:
            bucket = str(item.get("bucket") or "")
            window_key = str(item.get("window_key") or "")
            try:
                amount = max(0.0, float(item.get("amount", 1.0) or 0.0))
            except (TypeError, ValueError):
                amount = 0.0
            if not bucket or not window_key or not amount:
                continue
            conn.execute(
                "UPDATE counters SET count=MAX(0,count-?),updated_at=? "
                "WHERE bucket=? AND window_key=?",
                (amount, at, bucket, window_key),
            )
