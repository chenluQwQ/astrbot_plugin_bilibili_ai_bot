"""把旧版本的 json 数据导入 sqlite，以及升级前的备份。

规则：

- 每个新版本第一次启动先备份 ``data/``（json + sqlite），同一版本只备份一次，只留最近几份
- 一个模块一个事务：要么全部导入，要么一条不写
- 导入失败只停用这个模块，其他模块照常
- json 坏了不当成空的：跳过这个模块的导入，原文件不动
- 原 json 原地保留；记下导入时的文件指纹，指纹变了（中间退回旧版本跑过）就再合并导入一次
- 「导出 json」：把 sqlite 里的数据按旧格式写回 json，给想长期停在旧版本的人手动用
- 「收起旧 json」：已经完整导入、之后没再改过的旧 json 挪进 ``old_json/<时间>/``，不删

具体模块继承 ``Importer``，实现 ``prepare`` / ``import_data``，需要导出时再实现 ``export_data``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .json_file import JsonFile

logger = logging.getLogger("astrbot")

BACKUP_DIR_NAME = "backup"
KEEP_BACKUPS = 5
# 数据目录里不需要备份的子目录（临时媒体文件和备份本身）
SKIP_DIRS = {BACKUP_DIR_NAME, "temp_images", "temp_videos", "old_json"}


def file_fingerprint(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return "missing"


def record_fingerprint(*parts: Any) -> str:
    """给没有主键的日志类条目算一个稳定的指纹（时间 + 类型 + 内容）。"""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------- 备份

def backup_data_dir(data_dir: str, version: str, db_path: str | None = None,
                    keep: int = KEEP_BACKUPS) -> str | None:
    """备份数据目录；这个版本已经备份过就跳过。返回新备份的目录，跳过时返回 None。"""
    root = os.path.join(data_dir, BACKUP_DIR_NAME)
    os.makedirs(root, exist_ok=True)
    prefix = f"{version}-"
    if any(name.startswith(prefix) for name in os.listdir(root)):
        return None
    target = os.path.join(root, f"{version}-{time.strftime('%Y%m%d-%H%M%S')}")
    partial = target + ".partial"
    os.makedirs(partial)
    try:
        for name in os.listdir(data_dir):
            source = os.path.join(data_dir, name)
            if name in SKIP_DIRS or (db_path and os.path.abspath(source) == os.path.abspath(db_path)):
                continue
            if os.path.isfile(source) and name.endswith(".json"):
                shutil.copy2(source, os.path.join(partial, name))
        if db_path and os.path.exists(db_path):
            # 在线备份：插件运行时数据库是打开的，直接拷文件可能拷到写了一半的状态
            src = sqlite3.connect(db_path)
            dst = sqlite3.connect(os.path.join(partial, os.path.basename(db_path)))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        os.replace(partial, target)
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    _prune_backups(root, keep)
    logger.info(f"[BiliBot] 已备份数据目录到 {target}")
    return target


def _prune_backups(root: str, keep: int) -> None:
    backups = sorted(
        (os.path.join(root, name) for name in os.listdir(root) if not name.endswith(".partial")),
        key=os.path.getmtime,
    )
    for old in backups[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


# ---------------------------------------------------------------- 收起旧 json
OLD_JSON_DIR_NAME = "old_json"


def imported_json_files(db_path: str, data_dir: str) -> list[str]:
    """导入记录里的 json，现在的文件指纹和导入时一样的那些（相对数据目录的文件名）。

    指纹对不上的不算：导入失败没记、或者数据库断开期间又写过（下次启动还要合并）。
    """
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT source, fingerprint FROM json_imports").fetchall()
    except sqlite3.OperationalError:
        return []  # 还没导入过
    finally:
        conn.close()
    recorded: dict[str, set[str]] = {}
    for source, fingerprint in rows:
        recorded.setdefault(str(source), set()).add(str(fingerprint))
    return sorted(
        name for name, prints in recorded.items()
        if prints != {"missing"} and prints == {file_fingerprint(os.path.join(data_dir, name))}
    )


def stow_json_files(data_dir: str, names: Iterable[str]) -> str:
    """把这些 json 挪进 ``old_json/<时间>/``，返回那个目录。挪走后下次启动按「文件没了」导入一次空的，库里不变。"""
    target = os.path.join(data_dir, OLD_JSON_DIR_NAME, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(target, exist_ok=True)
    for name in names:
        source = os.path.join(data_dir, name)
        if os.path.isfile(source):
            shutil.move(source, os.path.join(target, name))
    return target


# ---------------------------------------------------------------- 导入

class Importer:
    """一个模块的旧 json → sqlite 导入器。"""

    module: str = ""
    sources: tuple[str, ...] = ()   # 相对数据目录的 json 文件名
    defaults: dict[str, Any] = {}   # 文件不存在时用的空值，默认 {}

    def prepare(self, conn: sqlite3.Connection) -> None:
        """建这个模块要用的表（``CREATE TABLE IF NOT EXISTS``）。"""

    def import_data(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        """把 ``{文件名: 内容}`` 合并写进 sqlite。会被重复调用，必须按主键或指纹去重。"""
        raise NotImplementedError

    def export_data(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """按旧 json 格式导出 ``{文件名: 内容}``。"""
        raise NotImplementedError


@dataclass
class ImportResult:
    module: str
    status: str               # imported / up_to_date / failed / db_unavailable
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.status in {"imported", "up_to_date"}


@dataclass
class MigrationReport:
    results: dict[str, ImportResult] = field(default_factory=dict)

    @property
    def disabled_modules(self) -> list[str]:
        return [name for name, result in self.results.items() if not result.usable]


class MigrationRunner:
    def __init__(self, db_path: str, data_dir: str):
        self.db_path = db_path
        self.data_dir = data_dir

    def run(self, importers: Iterable[Importer]) -> MigrationReport:
        importers = list(importers)
        report = MigrationReport()
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            # sqlite 打不开：已迁移的模块不能退回去写 json（两边会分叉），只能停用
            for importer in importers:
                report.results[importer.module] = ImportResult(importer.module, "db_unavailable", str(exc))
            logger.error(f"[BiliBot] 数据库打不开，已迁移到数据库的功能暂停: {exc}")
            return report
        try:
            for importer in importers:
                report.results[importer.module] = self._run_one(conn, importer)
        finally:
            conn.close()
        return report

    def export(self, importer: Importer) -> list[str]:
        """把一个模块的数据按旧格式写回 json，返回写了哪些文件。"""
        conn = self._connect()
        try:
            importer.prepare(conn)
            data = importer.export_data(conn)
        finally:
            conn.close()
        written = []
        for name, content in data.items():
            path = os.path.join(self.data_dir, name)
            JsonFile(path).write(content)
            written.append(path)
        return written

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS json_imports ("
            " module TEXT NOT NULL, source TEXT NOT NULL, fingerprint TEXT NOT NULL,"
            " imported_at REAL NOT NULL, PRIMARY KEY (module, source))"
        )
        return conn

    def _run_one(self, conn: sqlite3.Connection, importer: Importer) -> ImportResult:
        module = importer.module
        fingerprints = {
            name: file_fingerprint(os.path.join(self.data_dir, name)) for name in importer.sources
        }
        recorded = {
            row["source"]: row["fingerprint"]
            for row in conn.execute("SELECT source, fingerprint FROM json_imports WHERE module=?", (module,))
        }
        if recorded and all(recorded.get(name) == value for name, value in fingerprints.items()):
            return ImportResult(module, "up_to_date")

        data = {}
        for name in importer.sources:
            path = os.path.join(self.data_dir, name)
            if fingerprints[name] == "missing":
                data[name] = json.loads(json.dumps(importer.defaults.get(name, {})))
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data[name] = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                # 坏文件不当成空的：这个模块整个跳过，原文件不动，下次启动再试
                reason = f"{name} 读取失败: {exc}"
                logger.error(f"[BiliBot] {module} 数据导入跳过，功能暂停：{reason}")
                return ImportResult(module, "failed", reason)

        try:
            conn.execute("BEGIN IMMEDIATE")
            importer.prepare(conn)
            importer.import_data(conn, data)
            now = time.time()
            conn.executemany(
                "INSERT INTO json_imports(module, source, fingerprint, imported_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(module, source) DO UPDATE SET fingerprint=excluded.fingerprint, "
                "imported_at=excluded.imported_at",
                [(module, name, value, now) for name, value in fingerprints.items()],
            )
            conn.execute("COMMIT")
        except Exception as exc:
            conn.execute("ROLLBACK")
            reason = f"导入失败: {exc}"
            logger.error(f"[BiliBot] {module} 数据导入失败，已回滚，功能暂停：{exc}")
            return ImportResult(module, "failed", reason)
        logger.info(f"[BiliBot] {module} 已从旧 json 导入数据库")
        return ImportResult(module, "imported")
