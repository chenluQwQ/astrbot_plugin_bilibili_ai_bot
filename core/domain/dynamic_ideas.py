"""动态池子：看完视频、番剧时「想在动态里说说」的想法，攒在这里等发。

``dynamic_ideas`` 表只由这里读写，和四层服务同一个 sqlite 文件、单独一个连接（同 ``activity_runs``）。
状态只往前走：待发 → 已发 / 作废 / 过期，只改状态不删行。

规则：

- 3 天没发就过期；待发最多 10 条，满了挤掉最旧的
- 同一个视频（``key``）同时只有一条待发，后来的不进
- 类型是吐槽、段子、分享、转发、转述。转发是把这个视频原样转发到自己的动态、配一句话，只有 B站视频能转发；
  转述是站外内容用自己的话讲一遍。看视频、番剧标的只会是前四种里能用的
- 旧版本叫「搬运」的就是转发，打开时改名
- 站外来的（``source`` 是 ``feed`` / ``search``）带原链接 ``link``，``key`` 是 ``feed:`` / ``search:`` 开头的指纹
- 关注动态巡视来的（``source`` 是 ``dynamic_watch``）只有转发一种，``key`` 是 ``dyn:动态id``
"""
from __future__ import annotations

import sqlite3
import time

KINDS = ("吐槽", "段子", "分享", "转发", "转述")
# 看视频时能标的：转述是站外内容用的。
VIDEO_KINDS = ("吐槽", "段子", "分享", "转发")
REPOST = "转发"
# 旧名字：模型偶尔还会写，旧表里也有。
KIND_ALIASES = {"搬运": REPOST}
DEFAULT_KIND = "分享"
EXPIRE_SECONDS = 3 * 24 * 3600
MAX_PENDING = 10

PENDING, POSTED, DROPPED, EXPIRED = "pending", "posted", "dropped", "expired"

_DDL = (
    "CREATE TABLE IF NOT EXISTS dynamic_ideas ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,"
    " title TEXT NOT NULL DEFAULT '', up_name TEXT NOT NULL DEFAULT '', pic TEXT NOT NULL DEFAULT '',"
    " score REAL, review TEXT NOT NULL DEFAULT '', angle TEXT NOT NULL DEFAULT '',"
    " summary TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',"
    " created_at REAL NOT NULL, state_at REAL NOT NULL, link TEXT NOT NULL DEFAULT '')"
)
_COLUMNS = ("id", "key", "kind", "source", "title", "up_name", "pic", "score", "review", "angle",
            "summary", "state", "reason", "created_at", "state_at", "link")
# 站外来源：订阅地址、搜索关键词。
EXTERNAL_SOURCES = ("feed", "search")
# 关注的人的动态：转发它。
FOLLOWED_DYNAMIC = "dynamic_watch"
DYNAMIC_KEY_PREFIX = "dyn:"


class DynamicIdeas:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._conn.execute(_DDL)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(dynamic_ideas)")}
        if "link" not in columns:  # 早期的表没有原链接这一列
            self._conn.execute("ALTER TABLE dynamic_ideas ADD COLUMN link TEXT NOT NULL DEFAULT ''")
        for old, new in KIND_ALIASES.items():
            self._conn.execute("UPDATE dynamic_ideas SET kind=? WHERE kind=?", (new, old))
        self._conn.commit()

    @classmethod
    def open(cls, path: str) -> "DynamicIdeas":
        return cls(sqlite3.connect(path, check_same_thread=False))

    @classmethod
    def in_memory(cls) -> "DynamicIdeas":
        """sqlite 打不开时用；重启就没了，和丢掉标注的旧行为一样。"""
        return cls(sqlite3.connect(":memory:", check_same_thread=False))

    def close(self) -> None:
        self._conn.close()

    # ── 读 ──
    def pending(self, now: float | None = None) -> list[dict]:
        """待发的，最新的在前；顺手把过期的标掉。"""
        now = time.time() if now is None else now
        self._expire(now)
        rows = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM dynamic_ideas WHERE state=? ORDER BY created_at DESC, id DESC",
            (PENDING,),
        ).fetchall()
        return [dict(zip(_COLUMNS, row)) for row in rows]

    def touched(self, start: float, end: float, *, with_pending: bool = False, now: float | None = None) -> list[dict]:
        """这段时间里进池、或者状态变了（发了、作废、过期）的，最新进池的在前；``with_pending`` 时还带上所有待发的。
        给 WebUI「今天」页看。"""
        self._expire(time.time() if now is None else now)
        rows = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM dynamic_ideas"
            " WHERE (created_at>=? AND created_at<?) OR (state_at>=? AND state_at<?) OR (? AND state=?)"
            " ORDER BY created_at DESC, id DESC",
            (start, end, start, end, 1 if with_pending else 0, PENDING),
        ).fetchall()
        return [dict(zip(_COLUMNS, row)) for row in rows]

    def all(self) -> list[dict]:
        rows = self._conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM dynamic_ideas ORDER BY id").fetchall()
        return [dict(zip(_COLUMNS, row)) for row in rows]

    # ── 写 ──
    def add(self, *, key: str, kind: str, source: str, title: str = "", up_name: str = "", pic: str = "",
            score=None, review: str = "", angle: str = "", summary: str = "", link: str = "",
            now: float | None = None) -> int | None:
        """放进池子，返回编号；同一个 key 已经在等着发了就不进，返回 None。"""
        key = str(key or "").strip()
        if not key:
            return None
        if kind not in KINDS:
            raise ValueError(f"动态类型不对: {kind}")
        now = time.time() if now is None else now
        self._expire(now)
        if self._conn.execute(
            "SELECT 1 FROM dynamic_ideas WHERE key=? AND state=? LIMIT 1", (key, PENDING)
        ).fetchone():
            return None
        cursor = self._conn.execute(
            "INSERT INTO dynamic_ideas (key, kind, source, title, up_name, pic, score, review, angle, summary,"
            " state, created_at, state_at, link) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, kind, source, str(title or ""), str(up_name or ""), str(pic or ""),
             None if score is None else float(score), str(review or ""), str(angle or ""),
             str(summary or ""), PENDING, now, now, str(link or "")),
        )
        idea_id = cursor.lastrowid
        overflow = self._conn.execute(
            "SELECT id FROM dynamic_ideas WHERE state=? ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?",
            (PENDING, MAX_PENDING),
        ).fetchall()
        for (old_id,) in overflow:
            self._move(old_id, EXPIRED, "池子满了，挤掉最旧的", now)
        self._conn.commit()
        return idea_id

    def mark_posted(self, idea_id: int, now: float | None = None) -> None:
        self._move(idea_id, POSTED, "", time.time() if now is None else now)
        self._conn.commit()

    def drop(self, idea_id: int, reason: str, now: float | None = None) -> None:
        self._move(idea_id, DROPPED, reason, time.time() if now is None else now)
        self._conn.commit()

    def _expire(self, now: float) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE dynamic_ideas SET state=?, reason=?, state_at=? WHERE state=? AND created_at<?",
                (EXPIRED, "3 天没发", now, PENDING, now - EXPIRE_SECONDS),
            )

    def _move(self, idea_id: int, state: str, reason: str, now: float) -> None:
        # 只有待发的能走：发过、作废、过期的不会再变回来。
        self._conn.execute(
            "UPDATE dynamic_ideas SET state=?, reason=?, state_at=? WHERE id=? AND state=?",
            (state, reason, now, idea_id, PENDING),
        )
