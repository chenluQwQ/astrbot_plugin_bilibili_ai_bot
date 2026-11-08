"""去重账本：回复过的评论、处理过的 @、评论过的视频、语义去重库四份，存在 sqlite 的 ``processed_keys`` 表。

和 ``activity_log`` 一样同一个 sqlite 文件、单独一个连接；
打不开时退回原来的 json 文件读写，下次打得开再把这段时间的 json 合并进来。

- 一行一个键，``(kind, key)`` 唯一；先来的 ``id`` 小，删旧的按 ``id``
- 评论、@ 每晚只留最近 2000 个（原来 ``/bili清理 all`` 才截评论，@ 一直不截；通知每次只拉 10 条，2000 足够）
- 评论过的视频不删，和原来一样（看片时靠它不重复评论，一个 BV 号十几个字节）
- 语义去重库一条带一个向量，``data`` 里存整条，最多 80 条，记的时候就截
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Iterable

from .json_file import JsonFile
from .migration import Importer, MigrationRunner, record_fingerprint

logger = logging.getLogger("astrbot")

# 种类 → (旧文件名, 留几条；None 不删)
KINDS = {
    "comment": ("replied.json", 2000),
    "at": ("replied_at.json", 2000),
    "commented_video": ("commented_videos.json", None),
    "content": ("replied_content_keys.json", 80),
}
CACHE_KIB = 256

_DDL = (
    "CREATE TABLE IF NOT EXISTS processed_keys ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL,"
    " data TEXT NOT NULL DEFAULT '', UNIQUE (kind, key))",
)


def _create_table(conn: sqlite3.Connection) -> None:
    for statement in _DDL:
        conn.execute(statement)


def _keys(data: Any) -> list[str]:
    """旧文件里的键：字符串列表，个别版本混进过数字。"""
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if isinstance(item, (str, int)) and not isinstance(item, bool) and str(item)]


def _entries(data: Any) -> list[dict]:
    """语义去重库的条目；早期文件里的字符串指纹不要（读的时候本来就滤掉）。"""
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _entry_key(entry: dict) -> str:
    return record_fingerprint("content", entry.get("text"), entry.get("time"))


def _add_keys(conn: sqlite3.Connection, kind: str, keys: Iterable[str]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO processed_keys(kind, key) VALUES(?, ?)", ((kind, key) for key in keys)
    )


def _add_entry(conn: sqlite3.Connection, entry: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO processed_keys(kind, key, data) VALUES('content', ?, ?)",
        (_entry_key(entry), json.dumps(entry, ensure_ascii=False)),
    )


def _trim(conn: sqlite3.Connection, kind: str, keep: int) -> int:
    cursor = conn.execute(
        "DELETE FROM processed_keys WHERE kind=? AND id NOT IN ("
        "  SELECT id FROM processed_keys WHERE kind=? ORDER BY id DESC LIMIT ?)",
        (kind, kind, keep),
    )
    return cursor.rowcount


def _count(conn: sqlite3.Connection, kind: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM processed_keys WHERE kind=?", (kind,)).fetchone()[0]


class _KeysImporter(Importer):
    """一种一个导入器：一份 json 坏了只跳过这一种。键唯一，重复导入不会多出来。"""

    def __init__(self, kind: str):
        self.kind = kind
        self.module = f"processed_keys.{kind}"
        self.sources = (KINDS[kind][0],)
        self.defaults = {KINDS[kind][0]: []}

    def prepare(self, conn: sqlite3.Connection) -> None:
        _create_table(conn)

    def import_data(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        raw = data.get(self.sources[0])
        if self.kind == "content":
            for entry in _entries(raw):
                _add_entry(conn, entry)
        else:
            _add_keys(conn, self.kind, _keys(raw))

    def export_data(self, conn: sqlite3.Connection) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT key, data FROM processed_keys WHERE kind=? ORDER BY id", (self.kind,)
        ).fetchall()
        if self.kind == "content":
            return {self.sources[0]: [json.loads(row[1]) for row in rows]}
        return {self.sources[0]: [row[0] for row in rows]}


class KeySet:
    """一种键。``conn`` 为空时读写原来的 json 文件（sqlite 打不开的退路）。"""

    def __init__(self, kind: str, *, conn: sqlite3.Connection | None = None, file: JsonFile | None = None):
        self.kind = kind
        self.keep = KINDS[kind][1]
        self._conn = conn
        self._file = file

    def all(self) -> set[str]:
        if self._conn is None:
            return set(_keys(self._file.read([])))
        rows = self._conn.execute("SELECT key FROM processed_keys WHERE kind=?", (self.kind,)).fetchall()
        return {row[0] for row in rows}

    def has(self, key: str) -> bool:
        if self._conn is None:
            return str(key) in self.all()
        return self._conn.execute(
            "SELECT 1 FROM processed_keys WHERE kind=? AND key=? LIMIT 1", (self.kind, str(key))
        ).fetchone() is not None

    def add(self, keys: Iterable[str]) -> None:
        """记下这些键；已经有的不动。"""
        keys = [str(key) for key in keys if str(key)]
        if not keys:
            return
        if self._conn is None:
            def merge(data):
                return list(dict.fromkeys(_keys(data) + keys))
            self._file.update(merge, [])
            return
        _add_keys(self._conn, self.kind, keys)
        self._conn.commit()

    def trim(self) -> tuple[int, int] | None:
        """只留最近「留几条」个，返回 (原来, 现在)；没超就 None。"""
        if self.keep is None:
            return None
        if self._conn is None:
            data = self._file.read([])
            if isinstance(data, list) and len(data) > self.keep:
                self._file.update(lambda current: current[-self.keep:], [])
                return len(data), self.keep
            return None
        before = _count(self._conn, self.kind)
        if before <= self.keep:
            return None
        _trim(self._conn, self.kind, self.keep)
        self._conn.commit()
        return before, self.keep


class ContentEntries:
    """语义去重库：一条 ``{text, embedding, time}``，最多 80 条。"""

    kind = "content"

    def __init__(self, *, conn: sqlite3.Connection | None = None, file: JsonFile | None = None):
        self.keep = KINDS["content"][1]
        self._conn = conn
        self._file = file

    def items(self) -> list[dict]:
        """旧的在前。"""
        if self._conn is None:
            return _entries(self._file.read([]))
        rows = self._conn.execute(
            "SELECT data FROM processed_keys WHERE kind='content' ORDER BY id"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def append(self, entry: dict) -> None:
        if self._conn is None:
            self._file.update(lambda data: (_entries(data) + [entry])[-self.keep:], [])
            return
        _add_entry(self._conn, entry)
        _trim(self._conn, "content", self.keep)
        self._conn.commit()


class ProcessedKeys:
    """四份账本。装配时先按 json 建好（各处拿到的就是这些对象），启动时 ``connect`` 接上 sqlite。"""

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._db_path = ""
        self._conn: sqlite3.Connection | None = None

        def file(kind):
            return JsonFile(os.path.join(data_dir, KINDS[kind][0]))

        self.comment = KeySet("comment", file=file("comment"))
        self.at = KeySet("at", file=file("at"))
        self.commented_video = KeySet("commented_video", file=file("commented_video"))
        self.content = ContentEntries(file=file("content"))
        self._parts = (self.comment, self.at, self.commented_video, self.content)

    @property
    def in_database(self) -> bool:
        return self._conn is not None

    def connect(self, db_path: str) -> None:
        """先把旧 json 合并进来（指纹没变就跳过），再把每份切到 sqlite。

        导入失败的那份照样切过去，原 json 不动、下次启动再试；sqlite 打不开就抛出，继续用 json。
        """
        report = MigrationRunner(db_path, self._data_dir).run(_KeysImporter(kind) for kind in KINDS)
        for name in report.disabled_modules:
            logger.warning(f"[BiliBot] {name} 旧账本这次没导入（{report.results[name].reason}），新记录照常写进数据库")
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(f"PRAGMA cache_size=-{CACHE_KIB}")
        _create_table(conn)
        conn.commit()
        self._conn, self._db_path = conn, db_path
        for part in self._parts:
            part._conn = conn

    def export_json(self) -> list[str]:
        """按旧格式写回各自的 json，返回写了哪些文件。手动用。"""
        if self._conn is None:
            return []
        runner = MigrationRunner(self._db_path, self._data_dir)
        return [path for kind in KINDS for path in runner.export(_KeysImporter(kind))]

    def prune(self) -> dict[str, int]:
        """评论、@ 只留最近 2000 个，返回每种删了几个。"""
        removed = {}
        for part in (self.comment, self.at):
            trimmed = part.trim()
            if trimmed:
                removed[part.kind] = trimmed[0] - trimmed[1]
        return removed

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            for part in self._parts:
                part._conn = None
