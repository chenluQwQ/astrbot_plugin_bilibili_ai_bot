"""活动日志：看片、回复、评论、动态、番剧、巡视关注动态、安全、触发、资料调用、看片查证十份流水，存在 sqlite 的 ``activity_log`` 表。

和 ``memos`` 一样同一个 sqlite 文件、单独一个连接；
打不开时退回原来的 json 文件读写，下次打得开再把这段时间的 json 合并进来。

- ``recent()`` 只给最近「旧上限」条、顺序同原来的 json，原来那些读法的行为不变
- ``on_day()`` / ``days()`` 按日期查，能看到更早的
- 每晚 ``prune()``：删 30 天前的，但每种至少留「旧上限」条；每种最多 3000 条（整体只有几 MB）
- 连接的页缓存限在 512 KB，不多占内存；读的时候只取要的那几行
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .json_file import JsonFile
from .migration import Importer, MigrationRunner, record_fingerprint

logger = logging.getLogger("astrbot")

# 种类 → (旧文件名, 旧上限)
KINDS = {
    "watch": ("watch_log.json", 200),
    "reply": ("reply_log.json", 500),
    "proactive": ("proactive_log.json", 100),
    "dynamic": ("dynamic_log.json", 100),
    "bangumi": ("bangumi_watch_log.json", 300),
    "dynamic_watch": ("dynamic_watch_log.json", 500),
    "security": ("security_log.json", 500),
    "trigger": ("proactive_trigger_log.json", 200),
    "comment_context": ("comment_context_log.json", 200),
    "video_inspection": ("video_inspection_log.json", 200),
}
KEEP_DAYS = 30
MAX_ROWS = 3000
CACHE_KIB = 512

_DDL = (
    "CREATE TABLE IF NOT EXISTS activity_log ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, at TEXT NOT NULL DEFAULT '',"
    " data TEXT NOT NULL, fp TEXT NOT NULL DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS idx_activity_log_kind_at ON activity_log (kind, at)",
    "CREATE INDEX IF NOT EXISTS idx_activity_log_fp ON activity_log (kind, fp)",
)


def _stamp(entry: dict) -> str:
    return str(entry.get("time") or "")


def _fingerprint(kind: str, entry: dict) -> str:
    return record_fingerprint(kind, entry)


def _create_table(conn: sqlite3.Connection) -> None:
    for statement in _DDL:
        conn.execute(statement)


def _insert(conn: sqlite3.Connection, kind: str, entry: dict) -> None:
    conn.execute(
        "INSERT INTO activity_log(kind, at, data, fp) VALUES(?, ?, ?, ?)",
        (kind, _stamp(entry), json.dumps(entry, ensure_ascii=False), _fingerprint(kind, entry)),
    )


def _dicts(data: Any) -> list[dict]:
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


class _LogImporter(Importer):
    """一种日志一个导入器：一份 json 坏了只跳过这一种。按指纹去重，重复导入不会多出来。"""

    def __init__(self, kind: str):
        self.kind = kind
        self.module = f"activity_log.{kind}"
        self.sources = (KINDS[kind][0],)
        self.defaults = {KINDS[kind][0]: []}

    def prepare(self, conn: sqlite3.Connection) -> None:
        _create_table(conn)

    def import_data(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        for entry in _dicts(data.get(self.sources[0])):
            exists = conn.execute(
                "SELECT 1 FROM activity_log WHERE kind=? AND fp=? LIMIT 1",
                (self.kind, _fingerprint(self.kind, entry)),
            ).fetchone()
            if not exists:
                _insert(conn, self.kind, entry)

    def export_data(self, conn: sqlite3.Connection) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT data FROM activity_log WHERE kind=? ORDER BY at, id", (self.kind,)
        ).fetchall()
        entries = [json.loads(row[0]) for row in rows]
        return {self.sources[0]: entries[-KINDS[self.kind][1]:]}


class ActivityLog:
    """一种日志。``conn`` 为空时读写原来的 json 文件（sqlite 打不开的退路）。"""

    def __init__(self, kind: str, *, conn: sqlite3.Connection | None = None, file: JsonFile | None = None):
        self.kind = kind
        self.keep = KINDS[kind][1]
        self._conn = conn
        self._file = file

    def recent(self) -> list[dict]:
        """最近「旧上限」条，旧的在前，和原来 json 文件里的一样。"""
        if self._conn is None:
            return _dicts(self._file.read([]))[-self.keep:]
        rows = self._conn.execute(
            "SELECT data FROM activity_log WHERE kind=? ORDER BY at DESC, id DESC LIMIT ?",
            (self.kind, self.keep),
        ).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def append(self, entry: dict) -> list[dict]:
        """记一条，返回记完之后的 ``recent()``。"""
        if self._conn is None:
            def add(data):
                return (_dicts(data) + [entry])[-self.keep:]
            self._file.update(add, [])
        else:
            _insert(self._conn, self.kind, entry)
            self._conn.commit()
        return self.recent()

    def on_day(self, day: str) -> list[dict]:
        """某一天（``YYYY-MM-DD``）的全部，旧的在前。"""
        if self._conn is None:
            return [item for item in self.recent() if _stamp(item).startswith(day)]
        rows = self._conn.execute(
            "SELECT data FROM activity_log WHERE kind=? AND at>=? AND at<? ORDER BY at, id",
            (self.kind, day, day + "￿"),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def days(self) -> set[str]:
        """有记录的日子。"""
        if self._conn is None:
            stamps = [_stamp(item) for item in self.recent()]
        else:
            stamps = [row[0] for row in self._conn.execute(
                "SELECT DISTINCT substr(at, 1, 10) FROM activity_log WHERE kind=?", (self.kind,)
            ).fetchall()]
        return {stamp[:10] for stamp in stamps if len(stamp) >= 10 and stamp[4] == "-" and stamp[7] == "-"}


class ActivityLogs:
    """十种日志。装配时先按 json 建好（各处拿到的就是这些对象），启动时 ``connect`` 接上 sqlite。"""

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._db_path = ""
        self._conn: sqlite3.Connection | None = None
        self._logs = {
            kind: ActivityLog(kind, file=JsonFile(os.path.join(data_dir, name))) for kind, (name, _keep) in KINDS.items()
        }
        self.watch = self._logs["watch"]
        self.reply = self._logs["reply"]
        self.proactive = self._logs["proactive"]
        self.dynamic = self._logs["dynamic"]
        self.bangumi = self._logs["bangumi"]
        self.dynamic_watch = self._logs["dynamic_watch"]
        self.security = self._logs["security"]
        self.trigger = self._logs["trigger"]
        self.comment_context = self._logs["comment_context"]
        self.video_inspection = self._logs["video_inspection"]

    @property
    def in_database(self) -> bool:
        return self._conn is not None

    def connect(self, db_path: str) -> None:
        """先把旧 json 合并进来（指纹没变就跳过），再把每种日志切到 sqlite。

        导入失败的那种照样切过去，原 json 不动、下次启动再试；sqlite 打不开就抛出，日志继续用 json。
        """
        report = MigrationRunner(db_path, self._data_dir).run(_LogImporter(kind) for kind in KINDS)
        for name in report.disabled_modules:
            logger.warning(f"[BiliBot] {name} 旧日志这次没导入（{report.results[name].reason}），新记录照常写进数据库")
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(f"PRAGMA cache_size=-{CACHE_KIB}")
        _create_table(conn)
        conn.commit()
        self._conn, self._db_path = conn, db_path
        for log in self._logs.values():
            log._conn = conn

    def export_json(self) -> list[str]:
        """按旧格式写回各自的 json（每种最近「旧上限」条），返回写了哪些文件。手动用。"""
        if self._conn is None:
            return []
        runner = MigrationRunner(self._db_path, self._data_dir)
        return [path for kind in KINDS for path in runner.export(_LogImporter(kind))]

    def prune(self, now: datetime | None = None) -> dict[str, int]:
        """删 30 天前的（每种至少留「旧上限」条）和超过 3000 条的部分，返回每种删了几条。"""
        if self._conn is None:
            return {}
        cutoff = ((now or datetime.now()) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        removed = {}
        for kind, (_name, keep) in KINDS.items():
            cursor = self._conn.execute(
                "DELETE FROM activity_log WHERE kind=? AND id NOT IN ("
                "  SELECT id FROM activity_log WHERE kind=? ORDER BY at DESC, id DESC LIMIT ?)"
                " AND (at<? OR id NOT IN ("
                "  SELECT id FROM activity_log WHERE kind=? ORDER BY at DESC, id DESC LIMIT ?))",
                (kind, kind, keep, cutoff, kind, MAX_ROWS),
            )
            if cursor.rowcount:
                removed[kind] = cursor.rowcount
        self._conn.commit()
        return removed

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            for log in self._logs.values():
                log._conn = None
