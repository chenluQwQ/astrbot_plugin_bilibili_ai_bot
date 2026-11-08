"""活动状态机：每天每个活动的每个时刻走到哪一步了。

    planned ──→ running ──→ done
       │           ├──────→ failed
       │           └──────→ interrupted   （重启时还在 running 的）
       └──────→ skipped                   （错过了时间，或者被关掉）

只能按上图推进，其他跳转直接报错。状态存在 sqlite 的 ``activity_runs`` 表，
重启不丢；以前散在 ``_xxx_times`` / ``_xxx_triggered`` / ``_xxx_task`` 和 6 个日程 json 里。

被中断的活动不重跑：旧代码在活动开始时就记「今天这个时刻已触发」，重启后也不会再跑，
这里保持一致。

手动叫的那一次（命令、工具）也记在这里，时刻写成 ``manual-14:37:05``，直接从 running 开始。
它不是日程的一部分：不算「今天排过」，改日程时也不动它；时刻解析不出来，日程视图自然看不到。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from enum import Enum


MANUAL_PREFIX = "manual-"
_NOT_MANUAL = f"slot NOT LIKE '{MANUAL_PREFIX}%'"


class ActivityState(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


ALLOWED = {
    ActivityState.PLANNED: {ActivityState.RUNNING, ActivityState.SKIPPED},
    ActivityState.RUNNING: {ActivityState.DONE, ActivityState.FAILED, ActivityState.INTERRUPTED},
}


class IllegalTransition(RuntimeError):
    pass


def _slot_minute(slot: str) -> int:
    try:
        hour, minute = slot.split(":", 1)
        return int(hour) * 60 + int(minute)
    except (AttributeError, ValueError):
        return 24 * 60


@dataclass(frozen=True)
class ActivityRun:
    day: str
    kind: str
    slot: str
    state: ActivityState
    reason: str = ""


_DDL = (
    "CREATE TABLE IF NOT EXISTS activity_runs ("
    " day TEXT NOT NULL, kind TEXT NOT NULL, slot TEXT NOT NULL, state TEXT NOT NULL,"
    " reason TEXT NOT NULL DEFAULT '', planned_at REAL NOT NULL, started_at REAL, finished_at REAL,"
    " PRIMARY KEY (day, kind, slot))"
)


class ActivityRuns:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.execute(_DDL)
        self._conn.commit()

    @classmethod
    def open(cls, path: str) -> "ActivityRuns":
        return cls(sqlite3.connect(path, check_same_thread=False))

    # ── 读 ──
    def runs(self, day: str, kind: str | None = None) -> list[ActivityRun]:
        sql = "SELECT day, kind, slot, state, reason FROM activity_runs WHERE day=?"
        args: tuple = (day,)
        if kind is not None:
            sql += " AND kind=?"
            args += (kind,)
        rows = self._conn.execute(sql, args).fetchall()
        runs = [ActivityRun(r[0], r[1], r[2], ActivityState(r[3]), r[4]) for r in rows]
        # 时刻是 "9:00" 这种不补零的写法，按字符串排 "10:00" 会跑到 "9:00" 前面。
        return sorted(runs, key=lambda run: (_slot_minute(run.slot), run.kind))

    def is_planned(self, day: str, kind: str) -> bool:
        row = self._conn.execute(
            f"SELECT 1 FROM activity_runs WHERE day=? AND kind=? AND {_NOT_MANUAL} LIMIT 1", (day, kind)
        ).fetchone()
        return row is not None

    def running(self) -> list[ActivityRun]:
        rows = self._conn.execute(
            "SELECT day, kind, slot, state, reason FROM activity_runs WHERE state=? ORDER BY started_at",
            (ActivityState.RUNNING.value,),
        ).fetchall()
        return [ActivityRun(r[0], r[1], r[2], ActivityState(r[3]), r[4]) for r in rows]

    # ── 写 ──
    def plan(self, day: str, kind: str, slots: list[str]) -> None:
        """排今天的时刻；已经存在的时刻保持原状态（重复调用是安全的）。"""
        now = time.time()
        self._conn.executemany(
            "INSERT OR IGNORE INTO activity_runs(day, kind, slot, state, planned_at) VALUES(?, ?, ?, ?, ?)",
            [(day, kind, slot, ActivityState.PLANNED.value, now) for slot in slots],
        )
        self._conn.commit()

    def start(self, day: str, kind: str, slot: str) -> None:
        self._move(day, kind, slot, ActivityState.RUNNING, started=True)

    def start_manual(self, day: str, kind: str, clock: str, reason: str) -> str:
        """记下手动叫的一次（``clock`` 是 "HH:MM:SS"），返回它的时刻。同一秒叫两次就加序号。"""
        slot = base = f"{MANUAL_PREFIX}{clock}"
        taken = {r[0] for r in self._conn.execute(
            "SELECT slot FROM activity_runs WHERE day=? AND kind=? AND slot LIKE ?", (day, kind, f"{base}%")
        ).fetchall()}
        serial = 1
        while slot in taken:
            serial += 1
            slot = f"{base}-{serial}"
        now = time.time()
        self._conn.execute(
            "INSERT INTO activity_runs(day, kind, slot, state, reason, planned_at, started_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (day, kind, slot, ActivityState.RUNNING.value, reason, now, now),
        )
        self._conn.commit()
        return slot

    def replan(self, day: str, kind: str, slots: list[str]) -> None:
        """换掉这一天这种活动还没做的时刻。真起过的（running / done / failed / interrupted）留着，
        不在新时刻里也留着：它们是今天真发生过的事。没跑过的 planned / skipped 不在新时刻里就删掉。
        """
        keep = set(slots)
        rows = self._conn.execute(
            "SELECT slot FROM activity_runs WHERE day=? AND kind=? AND state IN (?, ?)",
            (day, kind, ActivityState.PLANNED.value, ActivityState.SKIPPED.value),
        ).fetchall()
        self._conn.executemany(
            "DELETE FROM activity_runs WHERE day=? AND kind=? AND slot=?",
            [(day, kind, r[0]) for r in rows if r[0] not in keep],
        )
        self.plan(day, kind, slots)

    def override(self, day: str, kind: str, slots: list[str], done: set[str], reason: str) -> None:
        """照着给的整体换掉（WebUI 改日程）：不在 ``slots`` 里的全删，``done`` 里的记成做过，
        其余的回到 planned（页面把做过的标回没做 = 让它到点再跑一次）。"""
        keep = set(slots)
        rows = self._conn.execute(
            f"SELECT slot, state FROM activity_runs WHERE day=? AND kind=? AND {_NOT_MANUAL}", (day, kind)
        ).fetchall()
        existing = {r[0]: ActivityState(r[1]) for r in rows}
        drop = [s for s, state in existing.items()
                if s not in keep or (s not in done and state is not ActivityState.PLANNED)]
        self._conn.executemany(
            "DELETE FROM activity_runs WHERE day=? AND kind=? AND slot=?", [(day, kind, s) for s in drop]
        )
        self.plan(day, kind, slots)
        for slot in slots:
            if slot in done and existing.get(slot, ActivityState.PLANNED) is ActivityState.PLANNED:
                self.skip(day, kind, slot, reason)

    def mark_skipped(self, day: str, kind: str, slot: str, reason: str) -> None:
        """没跑过的时刻直接记成「不用再跑」：没排过就先排上；已经不是 planned 的不动。"""
        self.plan(day, kind, [slot])
        row = self._conn.execute(
            "SELECT state FROM activity_runs WHERE day=? AND kind=? AND slot=?", (day, kind, slot)
        ).fetchone()
        if ActivityState(row[0]) is ActivityState.PLANNED:
            self.skip(day, kind, slot, reason)

    def finish(self, day: str, kind: str, slot: str, ok: bool, reason: str = "") -> None:
        self._move(day, kind, slot, ActivityState.DONE if ok else ActivityState.FAILED, reason, finished=True)

    def skip(self, day: str, kind: str, slot: str, reason: str) -> None:
        self._move(day, kind, slot, ActivityState.SKIPPED, reason, finished=True)

    def interrupt(self, day: str, kind: str, slot: str, reason: str) -> None:
        self._move(day, kind, slot, ActivityState.INTERRUPTED, reason, finished=True)

    def close(self) -> None:
        self._conn.close()

    def interrupt_running(self, reason: str = "插件重启时还在运行") -> int:
        cursor = self._conn.execute(
            "UPDATE activity_runs SET state=?, reason=?, finished_at=? WHERE state=?",
            (ActivityState.INTERRUPTED.value, reason, time.time(), ActivityState.RUNNING.value),
        )
        self._conn.commit()
        return cursor.rowcount

    def _move(self, day, kind, slot, target: ActivityState, reason: str = "",
              started: bool = False, finished: bool = False) -> None:
        row = self._conn.execute(
            "SELECT state FROM activity_runs WHERE day=? AND kind=? AND slot=?", (day, kind, slot)
        ).fetchone()
        if row is None:
            raise IllegalTransition(f"{day} {kind} {slot} 没有排过")
        current = ActivityState(row[0])
        if target not in ALLOWED.get(current, set()):
            raise IllegalTransition(f"{day} {kind} {slot}: {current.value} → {target.value} 不允许")
        now = time.time()
        self._conn.execute(
            "UPDATE activity_runs SET state=?, reason=?,"
            " started_at=COALESCE(?, started_at), finished_at=COALESCE(?, finished_at)"
            " WHERE day=? AND kind=? AND slot=?",
            (target.value, reason, now if started else None, now if finished else None, day, kind, slot),
        )
        self._conn.commit()
