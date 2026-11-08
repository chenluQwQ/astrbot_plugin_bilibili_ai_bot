"""插件数据目录里 json 文件的统一读写。

- 写入：同目录唯一临时文件 + ``os.replace``，中途失败不会留下半截文件
- 同一路径的读写共用一把可重入锁；``update()`` 在锁里完成「读 → 改 → 写」，
  多个线程同时改同一个文件不会互相覆盖
- 读到损坏的文件：先在旁边复制一份 ``<文件名>.corrupt-<时间>``，再按原来的行为
  返回默认值。这样下一次保存覆盖掉原文件时，原内容还留着，可以手动找回。
  每个文件只留最近 ``KEEP_CORRUPT_COPIES`` 份，更早的自动删掉
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import tempfile
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("astrbot")

KEEP_CORRUPT_COPIES = 3

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
# (路径, 修改时间, 大小)：同一个坏文件只备份一次
_PRESERVED: set[tuple[str, float, int]] = set()


def _lock_for(path: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.RLock())


class JsonFile:
    def __init__(self, path: str | os.PathLike):
        self.path = os.path.abspath(os.fspath(path))

    def read(self, default: Any = None) -> Any:
        if default is None:
            default = {}
        with _lock_for(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except FileNotFoundError:
                return default
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._preserve_corrupt(exc)
                return default
            except OSError as exc:
                logger.warning(f"[BiliBot] 读取数据文件失败 {os.path.basename(self.path)}: {exc}")
                return default

    def write(self, data: Any) -> None:
        parent = os.path.dirname(self.path)
        os.makedirs(parent, exist_ok=True)
        # 同路径串行提交：Windows 也可能拒绝两个线程同时 replace 同一个目标。
        with _lock_for(self.path):
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(self.path)}.",
                suffix=".tmp",
                dir=parent,
                text=True,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.path)
            finally:
                # json.dump / os.replace 失败时不遗留临时文件；原目标文件保持不变。
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass

    def locked(self) -> threading.RLock:
        """同一路径共用的可重入锁，``with JsonFile(p).locked():`` 包住一段读改写。"""
        return _lock_for(self.path)

    def update(self, change: Callable[[Any], Any], default: Any = None) -> Any:
        """在锁里读出当前内容，交给 ``change`` 修改，再写回；返回写入的内容。

        ``change`` 返回 ``None`` 时写回原对象（就地修改的写法）。
        """
        with _lock_for(self.path):
            data = self.read(default)
            result = change(data)
            if result is None:
                result = data
            self.write(result)
            return result

    def _preserve_corrupt(self, exc: Exception) -> None:
        try:
            stat = os.stat(self.path)
        except OSError:
            return
        key = (self.path, stat.st_mtime, stat.st_size)
        name = os.path.basename(self.path)
        if key in _PRESERVED:
            return
        backup = f"{self.path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            shutil.copy2(self.path, backup)
        except OSError as copy_exc:
            logger.error(f"[BiliBot] 数据文件 {name} 已损坏且备份失败（{copy_exc}），下次保存会覆盖它: {exc}")
            return
        _PRESERVED.add(key)
        self._prune_corrupt_copies()
        logger.error(
            f"[BiliBot] 数据文件 {name} 已损坏，已备份为 {os.path.basename(backup)}；"
            f"本次按空数据处理，下次保存会写入新内容: {exc}"
        )

    def _prune_corrupt_copies(self) -> None:
        # 文件名里的时间是 %Y%m%d-%H%M%S，按名字排序就是按时间排序
        copies = sorted(glob.glob(glob.escape(self.path) + ".corrupt-*"))
        for old in copies[:-KEEP_CORRUPT_COPIES]:
            try:
                os.remove(old)
            except OSError:
                pass
