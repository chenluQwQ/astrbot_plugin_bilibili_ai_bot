"""备忘录：回复评论、私信时 Bot 想之后去看的东西，记在这里等每日计划排。

``memos`` 表只由这里读写，和四层服务同一个 sqlite 文件、单独一个连接（同 ``dynamic_ideas``）。
状态：待办 → 已排 → 做完，或者作废 / 过期。已排的当天没做成，第二天放回待办（只有这一步往回走）。
做完、作废、过期的关掉一天后由夜里那一步删掉；看过的视频另有视频记忆和看过账本。

规则：

- 待办最多 ``max_pending`` 条（默认 20），满了先挤掉最旧的「以后再说」，没有就挤最旧的
- 同一个东西（``kind`` + ``key``）同时只有一条待办；别人再推荐只记上推荐人
- 同一个人每天最多让 Bot 记 2 条，主人不限；好感为负的人推荐的不记（门槛在调用方）
- 从记下那天算 3 天还没做就过期（排上了没做成也一样）；夜里模型可以自己挑着丢（``features/memo_review.py``）
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta

KINDS = ("video", "bangumi", "up", "other")
WHENS = ("today", "tomorrow", "someday")
SOURCES = ("comment", "private", "self", "owner")
MAX_PENDING = 20
PER_PERSON_PER_DAY = 2
EXPIRE_SECONDS = 3 * 24 * 3600
PURGE_AFTER_SECONDS = 24 * 3600
TARGET_LIMIT = 60
NOTE_LIMIT = 80

PENDING, SCHEDULED, DONE, DROPPED, EXPIRED = "pending", "scheduled", "done", "dropped", "expired"
OPEN_STATES = (PENDING, SCHEDULED)

_BV = re.compile(r"BV[0-9A-Za-z]{10}")

_DDL = (
    "CREATE TABLE IF NOT EXISTS memos ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL, target TEXT NOT NULL,"
    " note TEXT NOT NULL DEFAULT '', due_date TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,"
    " from_uid TEXT NOT NULL DEFAULT '', recommenders TEXT NOT NULL DEFAULT '[]',"
    " state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',"
    " created_at REAL NOT NULL, state_at REAL NOT NULL, plan_date TEXT NOT NULL DEFAULT '')"
)
_COLUMNS = ("id", "kind", "key", "target", "note", "due_date", "source", "from_uid", "recommenders",
            "state", "result", "reason", "created_at", "state_at", "plan_date")


class MemoRejected(ValueError):
    """这条不记，原因可以原样告诉模型。"""


def memo_key(kind: str, target: str) -> str:
    """去重用：视频按 BV 号，其余按去掉空白、小写后的名字。"""
    if kind == "video":
        match = _BV.search(target)
        return match.group(0) if match else ""
    return re.sub(r"\s+", "", target).lower()


def due_date(when: str, now: float) -> str:
    """today / tomorrow 换成日期；someday 不定日子。"""
    day = datetime.fromtimestamp(now)
    if when == "today":
        return day.strftime("%Y-%m-%d")
    if when == "tomorrow":
        return (day + timedelta(days=1)).strftime("%Y-%m-%d")
    return ""


class Memos:
    def __init__(self, conn: sqlite3.Connection, *, max_pending: int = MAX_PENDING):
        self._conn = conn
        self.max_pending = max(1, int(max_pending))
        self._conn.execute(_DDL)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(memos)").fetchall()}
        if "plan_date" not in columns:  # 早期建的表没有这一列
            self._conn.execute("ALTER TABLE memos ADD COLUMN plan_date TEXT NOT NULL DEFAULT ''")
        self._conn.commit()

    @classmethod
    def open(cls, path: str, **kwargs) -> "Memos":
        return cls(sqlite3.connect(path, check_same_thread=False), **kwargs)

    @classmethod
    def in_memory(cls, **kwargs) -> "Memos":
        """sqlite 打不开时用；重启就没了。"""
        return cls(sqlite3.connect(":memory:", check_same_thread=False), **kwargs)

    def close(self) -> None:
        self._conn.close()

    # ── 读 ──
    def open_items(self, now: float | None = None) -> list[dict]:
        """待办和已排的，最新的在前；顺手把过期的标掉。"""
        now = time.time() if now is None else now
        self._expire(now)
        return self._rows(f"state IN ({', '.join('?' * len(OPEN_STATES))}) ORDER BY created_at DESC, id DESC",
                          OPEN_STATES)

    def all(self) -> list[dict]:
        return self._rows("1 ORDER BY id")

    def pending(self, now: float | None = None) -> list[dict]:
        """待办的，最旧的在前（排计划、夜里挑着丢都用它）。"""
        now = time.time() if now is None else now
        self._expire(now)
        return self._rows("state=? ORDER BY created_at, id", (PENDING,))

    def get(self, memo_id) -> dict | None:
        try:
            rows = self._rows("id=?", (int(memo_id),))
        except (TypeError, ValueError):
            return None
        return rows[0] if rows else None

    def _rows(self, where: str, params=()) -> list[dict]:
        rows = self._conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM memos WHERE {where}", params).fetchall()
        items = [dict(zip(_COLUMNS, row)) for row in rows]
        for item in items:
            item["recommenders"] = json.loads(item["recommenders"] or "[]")
        return items

    # ── 写 ──
    def add(self, *, kind: str, target: str, note: str = "", when: str = "someday", source: str,
            from_uid: str = "", from_name: str = "", unlimited: bool = False,
            now: float | None = None) -> tuple[int, bool]:
        """记一条，返回 (编号, 是不是新记的)。已经有同一个东西在待办就只加推荐人。

        不合格、超过当天次数时抛 ``MemoRejected``。``unlimited``：主人和 Bot 自己不受每天次数限制。
        """
        kind, when, source = str(kind or "").strip(), str(when or "").strip(), str(source or "").strip()
        target = re.sub(r"\s+", " ", str(target or "")).strip()
        note = re.sub(r"\s+", " ", str(note or "")).strip()
        if kind not in KINDS:
            raise MemoRejected(f"kind 只能是 {' / '.join(KINDS)}。")
        if when not in WHENS:
            raise MemoRejected(f"when 只能是 {' / '.join(WHENS)}。")
        if source not in SOURCES:
            raise ValueError(f"备忘录来源不对: {source}")
        if not target or len(target) > TARGET_LIMIT:
            raise MemoRejected(f"target 不能为空，最多 {TARGET_LIMIT} 字。")
        if len(note) > NOTE_LIMIT:
            raise MemoRejected(f"note 最多 {NOTE_LIMIT} 字。")
        key = memo_key(kind, target)
        if not key:
            raise MemoRejected("video 的 target 要填完整的 BV 号。")
        now = time.time() if now is None else now
        self._expire(now)
        from_uid = str(from_uid or "").strip()
        recommender = {"uid": from_uid, "name": str(from_name or "")[:40]} if from_uid else None

        existing = self._conn.execute(
            f"SELECT id, recommenders FROM memos WHERE kind=? AND key=? AND state IN ({', '.join('?' * len(OPEN_STATES))})"
            " ORDER BY id DESC LIMIT 1", (kind, key, *OPEN_STATES),
        ).fetchone()
        if existing:
            memo_id, raw = existing
            people = json.loads(raw or "[]")
            if recommender and all(person.get("uid") != from_uid for person in people):
                people.append(recommender)
                self._conn.execute("UPDATE memos SET recommenders=? WHERE id=?",
                                   (json.dumps(people, ensure_ascii=False), memo_id))
                self._conn.commit()
            return memo_id, False

        if from_uid and not unlimited and self.count_from_today(from_uid, now) >= PER_PERSON_PER_DAY:
            raise MemoRejected(f"今天已经替这个人记了 {PER_PERSON_PER_DAY} 条，不再记了。")
        cursor = self._conn.execute(
            "INSERT INTO memos (kind, key, target, note, due_date, source, from_uid, recommenders, state,"
            " created_at, state_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, key, target, note, due_date(when, now), source, from_uid,
             json.dumps([recommender] if recommender else [], ensure_ascii=False), PENDING, now, now),
        )
        memo_id = cursor.lastrowid
        self._trim(now)
        self._conn.commit()
        return memo_id, True

    def count_from_today(self, uid: str, now: float) -> int:
        start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self._conn.execute(
            "SELECT COUNT(*) FROM memos WHERE from_uid=? AND created_at>=?", (str(uid), start),
        ).fetchone()[0]

    def drop(self, memo_id: int, reason: str, now: float | None = None) -> bool:
        return self._move(memo_id, DROPPED, reason, time.time() if now is None else now)

    def schedule(self, memo_id: int, plan_date: str, now: float | None = None) -> bool:
        """排进某天的计划：只有待办的能排。"""
        cursor = self._conn.execute(
            "UPDATE memos SET state=?, plan_date=?, state_at=? WHERE id=? AND state=?",
            (SCHEDULED, plan_date, time.time() if now is None else now, int(memo_id), PENDING),
        )
        self._conn.commit()
        return bool(cursor.rowcount)

    def finish(self, memo_id: int, result: str = "", now: float | None = None) -> bool:
        """做完了，记一句结果。"""
        cursor = self._conn.execute(
            f"UPDATE memos SET state=?, result=?, state_at=? WHERE id=? AND state IN ({', '.join('?' * len(OPEN_STATES))})",
            (DONE, str(result or "")[:200], time.time() if now is None else now, int(memo_id), *OPEN_STATES),
        )
        self._conn.commit()
        return bool(cursor.rowcount)

    def purge_closed(self, now: float | None = None) -> int:
        """删掉关掉超过一天的（做完、作废、过期）；还开着的不动。"""
        now = time.time() if now is None else now
        cursor = self._conn.execute(
            f"DELETE FROM memos WHERE state NOT IN ({', '.join('?' * len(OPEN_STATES))}) AND state_at<?",
            (*OPEN_STATES, now - PURGE_AFTER_SECONDS),
        )
        self._conn.commit()
        return cursor.rowcount

    def release_stale(self, today: str, now: float | None = None, *, including_today: bool = False) -> int:
        """排在以前某天、没做成的放回待办（今天和以后的不动）。

        ``including_today``：今天重新排计划时，今天排了还没做的也放回去，让新计划重新挑。
        """
        cursor = self._conn.execute(
            f"UPDATE memos SET state=?, plan_date='', state_at=? WHERE state=? AND plan_date{'<=' if including_today else '<'}?",
            (PENDING, time.time() if now is None else now, SCHEDULED, today),
        )
        self._conn.commit()
        return cursor.rowcount

    def _trim(self, now: float) -> None:
        """待办超过上限：先挤最旧的「以后再说」，没有就挤最旧的。"""
        while True:
            count = self._conn.execute("SELECT COUNT(*) FROM memos WHERE state=?", (PENDING,)).fetchone()[0]
            if count <= self.max_pending:
                return
            row = self._conn.execute(
                "SELECT id FROM memos WHERE state=? ORDER BY due_date!='' , created_at, id LIMIT 1", (PENDING,),
            ).fetchone()
            self._move(row[0], EXPIRED, "待办满了，挤掉最旧的", now, commit=False)

    def _expire(self, now: float) -> None:
        cursor = self._conn.execute(
            "UPDATE memos SET state=?, reason=?, state_at=? WHERE state=? AND created_at<?",
            (EXPIRED, "3 天没做", now, PENDING, now - EXPIRE_SECONDS),
        )
        if cursor.rowcount:
            self._conn.commit()

    def _move(self, memo_id: int, state: str, reason: str, now: float, *, commit: bool = True) -> bool:
        # 只有还开着的能走：做完、作废、过期的不会再变回来。
        cursor = self._conn.execute(
            f"UPDATE memos SET state=?, reason=?, state_at=? WHERE id=? AND state IN ({', '.join('?' * len(OPEN_STATES))})",
            (state, reason, now, int(memo_id), *OPEN_STATES),
        )
        if commit:
            self._conn.commit()
        return bool(cursor.rowcount)
