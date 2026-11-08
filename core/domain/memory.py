"""记忆条目：工作副本、写锁、和 sqlite / json 备份的同步，都只在这里。

- 真身在 sqlite（``layered.memories``）；``records`` 是整份读进内存的工作副本，
  检索、拼上下文都读它
- 向量在内存里是 float32 数组（``compact_vector``），不是 Python 浮点数列表（省 8 倍内存）
- ``memory.json`` 不再每存一条重写：sqlite 落后（``pending``）时立刻整份写、带向量，当恢复来源；
  平时只在启动和每晚清算后 ``export_backup`` 导出一份不带向量的；``memory_sync_state.json`` 的
  ``pending`` 表示 sqlite 落后了，下次启动或下次写入时整份补上
- 找记忆走 ``find`` / ``count`` / ``exists``；**只有拿着 ``lock``（或在 ``editing()`` 里）的
  成批任务（清算、启动修复）才直接碰 ``records``**
- 改副本的口子：``save`` 一条、``remove``、``replace``（压缩）、``editing()`` 里成批改（拿锁，退出时整份同步）、
  已经拿着 ``lock`` 的流程最后调 ``sync(assume_locked=True)``

记录本身怎么整理（补类型、补域、视频生命周期）是 ``_memory_entry.MemoryEntry`` 的规则，构造时传进来；
装配时用 ``memories_for`` 建。
"""
from __future__ import annotations

import asyncio
from astrbot.api import logger
from array import array
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Callable

from ..config import MEMORY_FILE


# sqlite 里记着「旧 json 已经整份导入过」的键；老用户靠它不重复导入，不能改名。
MIGRATED_KEY = "unified_memory_v2_ready"


def compact_vector(value):
    """向量在内存里存成 float32 数组：Python 浮点数列表一个数 32 字节，这样 4 字节。"""
    if isinstance(value, array) and value.typecode == "f":
        return value
    if isinstance(value, (list, tuple, array)) and value:
        try:
            return array("f", (float(v) for v in value))
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _compact(record):
    vector = record.get("embedding")
    if vector is not None:
        compact = compact_vector(vector)
        if compact is None:
            record.pop("embedding", None)
        else:
            record["embedding"] = compact
    return record


def backup_records(records, *, vectors=False):
    """写进 ``memory.json`` 的样子：去掉只在 sqlite 里有意义的行号。

    平时导出不带向量（sqlite 里有一份）；sqlite 落后、要拿 json 恢复的时候才带上（``vectors=True``）。
    """
    result = []
    for item in records:
        clean = dict(item)
        clean.pop("_sqlite_id", None)
        vector = clean.pop("embedding", None)
        if vectors and vector is not None:
            clean["embedding"] = [float(v) for v in vector]
        result.append(clean)
    return result


class Memories:
    def __init__(
        self,
        *,
        prepare: Callable[[dict], dict],
        normalize: Callable[[dict], dict],
        layered: Callable[[], Any],
        load_state: Callable[[], Any],
        save_state: Callable[[dict], None],
        save_backup: Callable[[list], None],
    ):
        self._prepare = prepare
        self._normalize = normalize
        self._layered = layered
        self._load_state = load_state
        self._save_state = save_state
        self._save_backup = save_backup
        self.records: list[dict] = []
        self.lock = asyncio.Lock()

    def load_backup(self, items) -> None:
        """启动时先从 ``memory.json`` 读进来；sqlite 能用的话 ``initialize`` 会再换成 sqlite 里的。"""
        self.records = [_compact(self._normalize(m)) for m in items if isinstance(m, dict)] if isinstance(items, list) else []

    def _store(self):
        layered = self._layered()
        if layered is None or not layered.is_open:
            return None
        return layered

    # ── 同步标记 ──
    def mark_pending(self, reason=""):
        """sqlite 落后了：记下来，并马上把整份（带向量）写进 ``memory.json``，下次启动拿它补。"""
        self._save_state({
            "pending": True,
            "reason": str(reason or "")[:300],
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._save_backup(backup_records(self.records, vectors=True))

    def clear_pending(self):
        self._save_state({
            "pending": False,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    def _pending(self) -> bool:
        state = self._load_state()
        return bool(state.get("pending")) if isinstance(state, dict) else False

    def write_backup(self):
        """只重写 ``memory.json``（清算中途留恢复点用，带向量），不碰 sqlite。"""
        self._save_backup(backup_records(self.records, vectors=True))

    def export_backup(self):
        """导出一份不带向量的 ``memory.json``（启动后、每晚清算后），给人看、给别的工具读。"""
        self._save_backup(backup_records(self.records))

    # ── 启动 ──
    async def initialize(self) -> bool:
        """首次导入旧 JSON；此后 SQLite 为主库，JSON 只保留兼容备份。"""
        layered = self._store()
        if layered is None:
            return False
        legacy = [
            self._prepare(item)
            for item in list(self.records)
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        migrated = bool(await layered.db.kv_get(MIGRATED_KEY, False))
        mapped_count = await layered.memories.legacy_count()
        if (
            not migrated
            or self._pending()
            or (bool(legacy) and mapped_count == 0)
        ):
            await layered.memories.replace_legacy(legacy)
            await layered.db.kv_set(MIGRATED_KEY, True)
        self.records = [
            _compact(self._normalize(item))
            for item in await layered.memories.load_legacy()
        ]
        if any("tags" not in item for item in self.records):
            # 加标签之前存的记忆：按现有字段补上标签，sqlite 的标签表一起写（只做一次，不调模型）
            self.records = [self._prepare(item) for item in self.records]
            await layered.memories.replace_legacy(self.records)
            logger.info(f"[BiliBot] 给旧记忆补上了标签：{len(self.records)} 条")
        self.clear_pending()
        self.export_backup()
        logger.info(
            f"[BiliBot] 统一记忆库已就绪: SQLite {len(self.records)} 条；"
            "memory.json 保留为兼容备份"
        )
        return True

    # ── 写 ──
    async def save(self, record):
        """存一条：同 rpid 的换掉，写 sqlite 一条（落后时整份），重写备份。"""
        async with self.lock:
            await self.save_unlocked(record)

    async def save_unlocked(self, record):
        rec = _compact(self._prepare(record))
        key = str(rec["rpid"])
        self.records = [m for m in self.records if str(m.get("rpid")) != key]
        self.records.append(rec)
        try:
            layered = self._store()
            if layered is None:
                raise RuntimeError("SQLite memory store is unavailable")
            if self._pending():
                await layered.memories.replace_legacy(self.records)
            else:
                await layered.memories.upsert_legacy(rec)
            self.clear_pending()
        except Exception as exc:
            # 整份写进 json 兜底，并明确标记；下次 SQLite 成功启动会自动补同步。
            self.mark_pending(exc)
            logger.warning(f"[BiliBot] 统一记忆写入失败，已保留 JSON 待同步: {exc}")

    async def sync(self, assume_locked=False):
        """把整份副本写进 sqlite 和备份；改过副本之后调。"""
        if assume_locked:
            return await self._sync_unlocked()
        async with self.lock:
            return await self._sync_unlocked()

    async def _sync_unlocked(self):
        prepared = [
            _compact(self._prepare(item))
            for item in self.records
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        # rpid 是兼容主键；保留最后一次更新，避免旧文件中的重复项继续扩散。
        unique = {str(item["rpid"]): item for item in prepared}
        self.records = list(unique.values())
        try:
            layered = self._store()
            if layered is None:
                raise RuntimeError("SQLite memory store is unavailable")
            await layered.memories.replace_legacy(self.records)
            self.clear_pending()
        except Exception as exc:
            self.mark_pending(exc)
            logger.warning(f"[BiliBot] 统一记忆快照同步失败，已保留 JSON 待同步: {exc}")

    # ── 查（别处只经过这里找记忆；以后换成 SQL，调用方不用改） ──
    def _match(self, m, *, thread_id, thread_prefix, oid, bvid, user_id, rpids, types, source, has_vector, tag, where):
        if not isinstance(m, dict):
            return False
        if tag is not None and tag not in (m.get("tags") or ()):
            return False
        if thread_id is not None and m.get("thread_id") != str(thread_id):
            return False
        if thread_prefix is not None and not str(m.get("thread_id") or "").startswith(thread_prefix):
            return False
        if oid is not None and m.get("oid") != str(oid):
            return False
        if bvid is not None and m.get("bvid") != bvid and m.get("thread_id") != f"video:{bvid}":
            return False
        if user_id is not None and m.get("user_id") != str(user_id):
            return False
        if rpids is not None and str(m.get("rpid") or "") not in rpids:
            return False
        if types is not None and self._normalize(m).get("memory_type") not in types:
            return False
        if source is not None and m.get("source") != source:
            return False
        if has_vector is not None and ("embedding" in m) != has_vector:
            return False
        return where is None or bool(where(m))

    def find_sync(self, *, thread_id=None, thread_prefix=None, oid=None, bvid=None, user_id=None, rpids=None,
                  types=None, source=None, has_vector=None, tag=None, where=None, newest_first=False,
                  limit=None) -> list[dict]:
        """按条件找记忆，按存的先后排（``newest_first`` 反过来）。

        ``bvid`` 同时认 ``thread_id == "video:BV"``；``types`` 按整理后的 ``memory_type``；
        ``tag`` 是一个「种类:值」标签（``domain/_memory_tags.py``）；
        ``where`` 是别的条件（域过滤之类）。找到的是只读的：要改用 ``editing()``、``remove``、``replace``。
        sync 版只给必须是 sync 的对外接口（``memory_api`` 的几个老方法）用，别处用 ``find``。
        """
        rpids = {str(r) for r in rpids} if rpids is not None else None
        types = set(types) if types is not None else None
        rows = reversed(self.records) if newest_first else self.records
        result = []
        for m in rows:
            if self._match(m, thread_id=thread_id, thread_prefix=thread_prefix, oid=oid, bvid=bvid, user_id=user_id,
                           rpids=rpids, types=types, source=source, has_vector=has_vector, tag=tag, where=where):
                result.append(m)
                if limit is not None and len(result) >= limit:
                    break
        return result

    async def find(self, **filters) -> list[dict]:
        return self.find_sync(**filters)

    def count_sync(self, **filters) -> int:
        return len(self.find_sync(**filters))

    async def count(self, **filters) -> int:
        return len(await self.find(**filters))

    async def exists(self, **filters) -> bool:
        return bool(await self.find(limit=1, **filters))

    # ── 改（除了 save） ──
    async def add_tag(self, new_tag, **filters) -> int:
        """给按 ``filters``（同 ``find``）找到的记忆加一个标签，返回加了几条。"""
        async with self.lock:
            changed = 0
            for m in self.find_sync(**filters):
                if new_tag in (m.get("tags") or ()):
                    continue
                await self.save_unlocked({**m, "tags": [*(m.get("tags") or ()), new_tag]})
                changed += 1
            return changed

    async def remove(self, rpids, *, assume_locked=False) -> int:
        """按 rpid 删，返回删了几条。"""
        rpids = {str(r) for r in rpids}

        async def run():
            before = len(self.records)
            self.records = [m for m in self.records if str(m.get("rpid") or "") not in rpids]
            removed = before - len(self.records)
            if removed:
                await self._sync_unlocked()
            return removed

        if assume_locked:
            return await run()
        async with self.lock:
            return await run()

    async def replace(self, old_rpids, record) -> None:
        """压缩用：删掉 ``old_rpids``，换成一条新的（总结）。"""
        old = {str(r) for r in old_rpids}
        async with self.lock:
            self.records = [m for m in self.records if str(m.get("rpid") or "") not in old]
            self.records.append(_compact(self._prepare(record)))
            await self._sync_unlocked()

    @asynccontextmanager
    async def editing(self):
        """成批改副本：拿着锁交出自己，``records`` 可以就地改也可以整个换掉；正常退出时整份同步。"""
        async with self.lock:
            yield self
            await self._sync_unlocked()


def memories_for(host):
    """按插件上的 ``memory_entry``、``json_store``、``layered_runtime`` 建好；都是用到时才去插件上找。"""
    return Memories(
        prepare=lambda record: host.memory_entry._prepare_memory_entry(record),
        normalize=lambda record: host.memory_entry._normalize_memory_entry(record),
        layered=lambda: getattr(host, "layered_runtime", None),
        load_state=lambda: host.states.memory_sync.read({}),
        save_state=lambda state: host.states.memory_sync.write(state),
        save_backup=lambda records: host.json_store._save_json(MEMORY_FILE, records),
    )
