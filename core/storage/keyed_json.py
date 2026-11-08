"""「编号 → 一份数据」的 json（好感度、画像、里程碑……），存在 sqlite 的 ``keyed_json`` 表里，一行一个编号。

和 ``activity_log`` 一样同一个 sqlite 文件、单独一个连接；
打不开时退回原来的 json 文件读写，下次打得开再把这段时间的 json 合并进来。

``KeyedDoc`` 的读写和 ``JsonFile`` 一样（``read`` / ``write`` / ``update`` / ``locked``），用它的数据主人不用改：

- ``read()`` 拼回整份 dict（和原来读 json 一样，顺序也一样：改了的编号留在原位，新的排在最后）
- ``write()`` 只改变了的那几行：没变的不动，没了的删掉
- 合并旧 json：同一个编号以 json 里的为准（旧版本后来写的），json 里没有的编号保留
"""
from __future__ import annotations

import json
from astrbot.api import logger
import os
import sqlite3
import threading
from typing import Any, Callable, Iterable

from .json_file import JsonFile
from .migration import Importer, MigrationRunner


CACHE_KIB = 512

_DDL = (
    "CREATE TABLE IF NOT EXISTS keyed_json ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL,"
    " UNIQUE (kind, key))",
)


def _create_table(conn: sqlite3.Connection) -> None:
    for statement in _DDL:
        conn.execute(statement)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _rows(conn: sqlite3.Connection, kind: str) -> dict[str, str]:
    # 按先来后到：和原来 json 里的顺序一样（按昵称找人只列前几个、同分排名都看这个）
    return {key: data for key, data in conn.execute("SELECT key, data FROM keyed_json WHERE kind=? ORDER BY id", (kind,))}


def _apply(conn: sqlite3.Connection, kind: str, new: dict, old: dict[str, str], *, drop_missing: bool) -> None:
    """把 ``new`` 写成 ``kind`` 的行：只写变了的；``drop_missing`` 时删掉 ``new`` 里没有的编号。"""
    changed = []
    for key, value in new.items():
        text = _dump(value)
        if old.get(str(key)) != text:
            changed.append((kind, str(key), text))
    if changed:
        conn.executemany(
            "INSERT INTO keyed_json(kind, key, data) VALUES(?, ?, ?) "
            "ON CONFLICT(kind, key) DO UPDATE SET data=excluded.data",
            changed,
        )
    if drop_missing:
        keep = {str(key) for key in new}
        gone = [(kind, key) for key in old if key not in keep]
        if gone:
            conn.executemany("DELETE FROM keyed_json WHERE kind=? AND key=?", gone)


class _KeyedImporter(Importer):
    """一种一个导入器：一份 json 坏了只跳过这一种。根不是 dict 的当空的（数据主人本来也这么读）。"""

    def __init__(self, kind: str, filename: str):
        self.kind = kind
        self.module = f"keyed_json.{kind}"
        self.sources = (filename,)
        self.defaults = {filename: {}}

    def prepare(self, conn: sqlite3.Connection) -> None:
        _create_table(conn)

    def import_data(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        raw = data.get(self.sources[0])
        if isinstance(raw, dict):
            _apply(conn, self.kind, raw, _rows(conn, self.kind), drop_missing=False)

    def export_data(self, conn: sqlite3.Connection) -> dict[str, Any]:
        return {self.sources[0]: {key: json.loads(text) for key, text in _rows(conn, self.kind).items()}}


class KeyedDoc:
    """一份「编号 → 数据」。``conn`` 为空时就是原来的 json 文件。"""

    def __init__(self, kind: str, file: JsonFile, *, conn: sqlite3.Connection | None = None):
        self.kind = kind
        self._file = file
        self._conn = conn
        self._lock = threading.RLock()

    @property
    def path(self) -> str:
        return self._file.path

    def read(self, default: Any = None) -> Any:
        if self._conn is None:
            return self._file.read(default)
        with self._lock:
            rows = _rows(self._conn, self.kind)
        if not rows and default is not None and not isinstance(default, dict):
            return default
        return {key: json.loads(text) for key, text in rows.items()}

    def write(self, data: Any) -> None:
        if self._conn is None:
            self._file.write(data)
            return
        if not isinstance(data, dict):
            logger.warning(f"[BiliBot] {self.kind} 要写的不是 dict（{type(data).__name__}），没写")
            return
        with self._lock:
            _apply(self._conn, self.kind, data, _rows(self._conn, self.kind), drop_missing=True)
            self._conn.commit()

    def locked(self):
        return self._file.locked() if self._conn is None else self._lock

    def update(self, change: Callable[[Any], Any], default: Any = None) -> Any:
        with self.locked():
            data = self.read(default)
            result = change(data)
            if result is None:
                result = data
            self.write(result)
            return result


class KeyedDocs:
    """几份「编号 → 数据」。装配时先按 json 建好（各处拿到的就是这些对象），启动时 ``connect`` 接上 sqlite。

    ``kinds``：种类 → 旧文件名；每种挂成同名属性。
    """

    def __init__(self, data_dir: str, kinds: dict[str, str]):
        self._data_dir = data_dir
        self._kinds = dict(kinds)
        self._db_path = ""
        self._conn: sqlite3.Connection | None = None
        self._docs = {kind: KeyedDoc(kind, JsonFile(os.path.join(data_dir, name))) for kind, name in kinds.items()}
        for kind, doc in self._docs.items():
            setattr(self, kind, doc)

    @property
    def in_database(self) -> bool:
        return self._conn is not None

    def _importers(self) -> Iterable[_KeyedImporter]:
        return (_KeyedImporter(kind, name) for kind, name in self._kinds.items())

    def connect(self, db_path: str) -> None:
        """先把旧 json 合并进来（指纹没变就跳过），再把每份切到 sqlite。

        导入失败的那份照样切过去，原 json 不动、下次启动再试；sqlite 打不开就抛出，继续用 json。
        """
        report = MigrationRunner(db_path, self._data_dir).run(self._importers())
        for name in report.disabled_modules:
            logger.warning(f"[BiliBot] {name} 旧数据这次没导入（{report.results[name].reason}），新记录照常写进数据库")
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(f"PRAGMA cache_size=-{CACHE_KIB}")
        _create_table(conn)
        conn.commit()
        self._conn, self._db_path = conn, db_path
        for doc in self._docs.values():
            doc._conn = conn

    def export_json(self) -> list[str]:
        """按旧格式写回各自的 json，返回写了哪些文件。手动用。"""
        if self._conn is None:
            return []
        runner = MigrationRunner(self._db_path, self._data_dir)
        return [path for importer in self._importers() for path in runner.export(importer)]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            for doc in self._docs.values():
                doc._conn = None


# ---------------------------------------------------------------- 整份存一行的小状态

class _WholeImporter(Importer):
    """小状态：整份 json 存成 ``kind='state'``、``key=旧文件名`` 的一行；合并时 json 整份为准。"""

    def __init__(self, filename: str):
        self.module = f"keyed_json.state.{filename}"
        self.sources = (filename,)
        self.defaults = {filename: None}

    def prepare(self, conn: sqlite3.Connection) -> None:
        _create_table(conn)

    def import_data(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        raw = data.get(self.sources[0])
        if raw is not None:
            _apply(conn, "state", {self.sources[0]: raw}, _rows_of(conn, self.sources[0]), drop_missing=False)

    def export_data(self, conn: sqlite3.Connection) -> dict[str, Any]:
        text = _rows_of(conn, self.sources[0]).get(self.sources[0])
        return {} if text is None else {self.sources[0]: json.loads(text)}


def _rows_of(conn: sqlite3.Connection, filename: str) -> dict[str, str]:
    row = conn.execute("SELECT data FROM keyed_json WHERE kind='state' AND key=?", (filename,)).fetchone()
    return {} if row is None else {filename: row[0]}


class WholeDoc:
    """一份整存整取的小状态（心情、日程、冷却……），读写同 ``JsonFile``，外加 ``delete``。"""

    def __init__(self, filename: str, file: JsonFile, *, conn: sqlite3.Connection | None = None):
        self.filename = filename
        self._file = file
        self._conn = conn
        self._lock = threading.RLock()

    @property
    def path(self) -> str:
        return self._file.path

    def read(self, default: Any = None) -> Any:
        if self._conn is None:
            return self._file.read(default)
        with self._lock:
            text = _rows_of(self._conn, self.filename).get(self.filename)
        if text is None:
            return {} if default is None else default
        return json.loads(text)

    def write(self, data: Any) -> None:
        if self._conn is None:
            self._file.write(data)
            return
        with self._lock:
            _apply(self._conn, "state", {self.filename: data}, _rows_of(self._conn, self.filename), drop_missing=False)
            self._conn.commit()

    def delete(self) -> None:
        """删掉这份（WebUI 改了日程配置、手动重新生成时用：下次读到空的就重新生成）。"""
        if self._conn is None:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            return
        with self._lock:
            self._conn.execute("DELETE FROM keyed_json WHERE kind='state' AND key=?", (self.filename,))
            self._conn.commit()

    def locked(self):
        return self._file.locked() if self._conn is None else self._lock

    def update(self, change: Callable[[Any], Any], default: Any = None) -> Any:
        with self.locked():
            data = self.read(default)
            result = change(data)
            if result is None:
                result = data
            self.write(result)
            return result


class StateDocs:
    """一组小状态。``kinds``：种类 → 旧文件名；每种挂成同名属性。用法同 ``KeyedDocs``。"""

    def __init__(self, data_dir: str, kinds: dict[str, str]):
        self._data_dir = data_dir
        self._kinds = dict(kinds)
        self._db_path = ""
        self._conn: sqlite3.Connection | None = None
        self._docs = {kind: WholeDoc(name, JsonFile(os.path.join(data_dir, name))) for kind, name in kinds.items()}
        for kind, doc in self._docs.items():
            setattr(self, kind, doc)

    @property
    def in_database(self) -> bool:
        return self._conn is not None

    def _importers(self) -> Iterable[_WholeImporter]:
        return (_WholeImporter(name) for name in self._kinds.values())

    def connect(self, db_path: str) -> None:
        report = MigrationRunner(db_path, self._data_dir).run(self._importers())
        for name in report.disabled_modules:
            logger.warning(f"[BiliBot] {name} 旧状态这次没导入（{report.results[name].reason}），新状态照常写进数据库")
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(f"PRAGMA cache_size=-{CACHE_KIB}")
        _create_table(conn)
        conn.commit()
        self._conn, self._db_path = conn, db_path
        for doc in self._docs.values():
            doc._conn = conn

    def export_json(self) -> list[str]:
        if self._conn is None:
            return []
        runner = MigrationRunner(self._db_path, self._data_dir)
        return [path for importer in self._importers() for path in runner.export(importer)]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            for doc in self._docs.values():
                doc._conn = None
