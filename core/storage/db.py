"""SQLite 存储核心。

设计要点：
- 单一连接 + `asyncio.Lock`，所有写操作串行化。插件是单进程模型，
  这样既避免了 "database is locked"，也让 claim 语义可以依赖事务。
- 所有阻塞调用通过 ``asyncio.to_thread`` 交给线程池，不阻塞事件循环。
- WAL 模式，崩溃后可恢复；``synchronous=NORMAL`` 在个人实例上足够。
- 时间统一 UTC 秒（float）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_VERSION = "6"


def now() -> float:
    """当前 UTC 秒。集中一处便于测试替换。"""
    return time.time()


class Database:
    """轻量异步包装的 SQLite 句柄。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 生命周期
    async def open(self) -> None:
        if self._conn is not None:
            return
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        self._migrate_schema_sync(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
        conn.commit()
        self._conn = conn

    @staticmethod
    def _migrate_schema_sync(conn: sqlite3.Connection) -> None:
        """Apply additive migrations for databases created before schema v3."""

        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(actions)").fetchall()
        }
        additions = {
            "priority": "INTEGER NOT NULL DEFAULT 40",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "budget": "TEXT NOT NULL DEFAULT '[]'",
            "updated_at": "REAL",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE actions ADD COLUMN {name} {declaration}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_actions_queue "
            "ON actions(state,priority,created_at)"
        )
        conn.execute(
            "UPDATE actions SET updated_at=COALESCE(updated_at,finished_at,created_at)"
        )

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await asyncio.to_thread(conn.close)

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not open; call await db.open() first")
        return self._conn

    # ------------------------------------------------------------ 基础读写
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行写语句，返回 lastrowid（无则返回受影响行数）。"""
        async with self._lock:
            return await asyncio.to_thread(self._execute_sync, sql, params)

    def _execute_sync(self, sql: str, params: Sequence[Any]) -> int:
        conn = self._require()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid if cur.lastrowid else cur.rowcount

    async def execute_many(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._execute_many_sync, sql, list(seq))

    def _execute_many_sync(self, sql: str, rows: list[Sequence[Any]]) -> None:
        conn = self._require()
        conn.executemany(sql, rows)
        conn.commit()

    async def fetch_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[sqlite3.Row]:
        async with self._lock:
            return await asyncio.to_thread(self._fetch_all_sync, sql, params)

    def _fetch_all_sync(self, sql: str, params: Sequence[Any]) -> list[sqlite3.Row]:
        return list(self._require().execute(sql, params).fetchall())

    async def fetch_one(
        self, sql: str, params: Sequence[Any] = ()
    ) -> sqlite3.Row | None:
        async with self._lock:
            return await asyncio.to_thread(self._fetch_one_sync, sql, params)

    def _fetch_one_sync(self, sql: str, params: Sequence[Any]) -> sqlite3.Row | None:
        return self._require().execute(sql, params).fetchone()

    async def fetch_value(
        self, sql: str, params: Sequence[Any] = (), default: Any = None
    ) -> Any:
        row = await self.fetch_one(sql, params)
        if row is None or len(row) == 0:
            return default
        value = row[0]
        return default if value is None else value

    async def transaction(self, steps: Sequence[tuple[str, Sequence[Any]]]) -> None:
        """在单个事务里按序执行多条语句，任一失败则整体回滚。"""
        async with self._lock:
            await asyncio.to_thread(self._transaction_sync, list(steps))

    def _transaction_sync(self, steps: list[tuple[str, Sequence[Any]]]) -> None:
        conn = self._require()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for sql, params in steps:
                conn.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    async def run(self, fn, *args: Any) -> Any:
        """在持锁的线程里跑一段自定义逻辑，`fn(conn, *args)`。

        用于需要"读后写"原子性的场景（如事件 claim）。fn 内部不要提交，
        由本方法统一提交或回滚。
        """
        async with self._lock:
            return await asyncio.to_thread(self._run_sync, fn, *args)

    def _run_sync(self, fn, *args: Any) -> Any:
        conn = self._require()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = fn(conn, *args)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------ KV 便捷层
    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetch_one(
            "SELECT value, expires_at FROM kv WHERE key=?", (key,)
        )
        if row is None:
            return default
        expires_at = row["expires_at"]
        if expires_at is not None and expires_at < now():
            await self.execute("DELETE FROM kv WHERE key=?", (key,))
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    async def kv_set(self, key: str, value: Any, ttl: float | None = None) -> None:
        expires_at = now() + ttl if ttl else None
        await self.execute(
            "INSERT INTO kv(key, value, updated_at, expires_at) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at, expires_at=excluded.expires_at",
            (key, json.dumps(value, ensure_ascii=False), now(), expires_at),
        )

    async def kv_delete(self, key: str) -> None:
        await self.execute("DELETE FROM kv WHERE key=?", (key,))

    # ------------------------------------------------------------ 维护
    async def db_size_bytes(self) -> int:
        page_size = await self.fetch_value("PRAGMA page_size", default=0)
        page_count = await self.fetch_value("PRAGMA page_count", default=0)
        return int(page_size or 0) * int(page_count or 0)

    async def table_counts(self) -> dict[str, int]:
        rows = await self.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        counts: dict[str, int] = {}
        for row in rows:
            name = row["name"]
            counts[name] = int(
                await self.fetch_value(f"SELECT COUNT(*) FROM {name}", default=0) or 0
            )
        return counts

    async def vacuum(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._vacuum_sync)

    def _vacuum_sync(self) -> None:
        conn = self._require()
        conn.execute("VACUUM")
        conn.commit()
