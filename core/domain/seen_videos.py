"""已看视频账本：去重用，看过的 BV 永远记得，不跟着记忆遗忘。

真身在 sqlite（``layered.seen_videos``），``seen_videos.json`` 是备份，只由这里写。
旧版本把「看过」散在五个地方（看片记录、评论过的、视频缓存、外部记忆、视频记忆），
启动时一次性导进来；读的时候也把它们一起算上兜底，sqlite 打不开时照样能去重。
"""
from __future__ import annotations

import asyncio
from astrbot.api import logger
from datetime import datetime

from ..config import SEEN_VIDEOS_FILE
from ..infra.host_view import HostView


def _bv(value) -> str:
    """规范成 ``BV`` 开头；不像 BV 号的返回空串。"""
    key = str(value or "").strip()
    if len(key) < 4 or key[:2].lower() != "bv":
        return ""
    return "BV" + key[2:]


class SeenVideos(HostView):
    HOST_API = frozenset({"memory_docs", 
        "activity_logs", "json_store", "layered_runtime", "memories", "memory_entry", "processed_keys",
    })

    def __init__(self, host):
        super().__init__(host)
        self._lock = asyncio.Lock()

    def _store(self):
        layered = getattr(self, "layered_runtime", None)
        if layered is None or not layered.is_open:
            return None
        return layered

    def _ledger(self) -> dict:
        ledger = self.json_store._load_json(SEEN_VIDEOS_FILE, {})
        return ledger if isinstance(ledger, dict) else {}

    # ── 旧来源 ──
    async def legacy_records(self, include_ledger=True):
        """Collect all old, possibly capped sources for one-time permanent import."""
        records = []

        def add(bvid, item=None, source="legacy"):
            data = item if isinstance(item, dict) else {}
            if data.get("content_analyzed") is False or (
                data.get("source") == "group_share" and not data.get("content_analyzed")
            ):
                return
            key = str(bvid or data.get("bvid") or "").strip()
            if len(key) < 4 or key[:2].lower() != "bv":
                return
            first_seen = self.memory_entry._memory_timestamp(
                data.get("first_seen_at")
                or data.get("time")
                or data.get("watched_at")
            )
            last_related = self.memory_entry._memory_timestamp(
                data.get("last_related_at")
                or data.get("time")
                or data.get("watched_at")
            )
            records.append(
                {
                    "bvid": "BV" + key[2:],
                    "first_seen_at": min(first_seen, last_related),
                    "last_related_at": max(first_seen, last_related),
                    "title": data.get("title", ""),
                    "owner_mid": data.get("owner_mid") or data.get("up_mid", ""),
                    "owner_name": data.get("owner_name") or data.get("up_name", ""),
                    "tname": data.get("tname", ""),
                    "source": data.get("source") or source,
                }
            )

        watch_log = self.activity_logs.watch.recent()
        for item in (watch_log if isinstance(watch_log, list) else []):
            if isinstance(item, dict):
                add(item.get("bvid"), item, "watch_log")
        for item in self.processed_keys.commented_video.all():
            add(item, source="commented")
        video_memory = self.memory_docs.video_memory.read({})
        for bvid, item in (
            video_memory.items() if isinstance(video_memory, dict) else []
        ):
            add(bvid, item, "video_memory")
        external_memory = self.memory_docs.external_memory.read({})
        for bvid, item in (
            external_memory.items() if isinstance(external_memory, dict) else []
        ):
            add(bvid, item, "external_memory")
        seen_backup = self.json_store._load_json(SEEN_VIDEOS_FILE, {}) if include_ledger else {}
        for bvid, item in (
            seen_backup.items() if isinstance(seen_backup, dict) else []
        ):
            add(bvid, item, "seen_backup")
        for item in await self.memories.find(types={"video"}):
            add(item.get("bvid"), item, "semantic_memory")
        return records

    async def import_legacy(self):
        """Idempotently migrate every legacy BV source into the permanent ledger."""
        layered = self._store()
        if layered is None:
            return 0
        records = await self.legacy_records()
        created = await layered.seen_videos.import_many(records)
        total = await layered.seen_videos.count()
        logger.info(
            f"[BiliBot] 永久视频去重账本已就绪: {total} 条"
            f"（本次迁移新增 {created} 条）"
        )
        return total

    # ── 读 ──
    async def bvids(self):
        """Return permanent seen BVs, with legacy sources as a safe fallback."""
        result = {
            str(item.get("bvid") or "")
            for item in await self.legacy_records()
            if item.get("bvid")
        }
        layered = self._store()
        if layered is not None:
            try:
                result.update(await layered.seen_videos.all_bvids())
            except Exception as exc:
                logger.warning(f"[BiliBot] 读取永久视频去重账本失败，使用兼容记录: {exc}")
        return result

    async def has_seen(self, bvid):
        key = str(bvid or "").strip()
        if not key:
            return False
        return ("BV" + key[2:] if key[:2].lower() == "bv" else key) in (
            await self.bvids()
        )

    async def record(self, bvid):
        """Return the lightweight trace without pulling video content into the ledger."""
        key = _bv(bvid)
        if not key:
            return None
        ledger = self._ledger()
        if isinstance(ledger.get(key), dict):
            return dict(ledger[key])
        layered = self._store()
        if layered is not None:
            try:
                record = await layered.seen_videos.get(key)
                if record:
                    return record
            except Exception as exc:
                logger.warning(f"[BiliBot] 读取视频观看痕迹失败: {exc}")
        return next((item for item in reversed(await self.legacy_records())
                     if item.get("bvid") == key), None)

    async def entry_source(self, bvid):
        """账本里这个 BV 是谁记上的（备份优先，再看 sqlite）；没有记录返回 ``None``。"""
        entry = self._ledger().get(bvid)
        if isinstance(entry, dict):
            return str(entry.get("source") or "")
        layered = self._store()
        if layered is not None:
            entry = await layered.seen_videos.get(bvid)
            if entry is not None:
                return str(entry.get("last_source", "") or "")
        return None

    # ── 写 ──
    async def mark(self, bvid, info=None, source="watch", *, increment=True):
        """Persist a watched BV before capped activity logs can forget it."""
        data = info if isinstance(info, dict) else {}
        key = _bv(bvid or data.get("bvid"))
        if not key:
            return False
        async with self._lock:
            ledger = self._ledger()
            previous = ledger.get(key) if isinstance(ledger.get(key), dict) else {}
            now_text = datetime.now().strftime("%Y-%m-%d %H:%M")
            previous_count = int(previous.get("watch_count", 0) or 0)
            ledger[key] = {
                "bvid": key,
                "first_seen_at": previous.get("first_seen_at") or now_text,
                "last_related_at": now_text,
                "watch_count": max(1, previous_count + (1 if increment else 0)),
                "title": data.get("title") or previous.get("title", ""),
                "owner_mid": str(
                    data.get("owner_mid") or data.get("up_mid")
                    or previous.get("owner_mid", "")
                ),
                "owner_name": data.get("owner_name")
                or data.get("up_name")
                or previous.get("owner_name", ""),
                "tname": data.get("tname") or previous.get("tname", ""),
                "source": source or previous.get("source", ""),
            }
            self.json_store._save_json(SEEN_VIDEOS_FILE, ledger)
            layered = self._store()
            if layered is not None:
                try:
                    await layered.seen_videos.mark_seen(
                        key,
                        seen_at=datetime.now().timestamp(),
                        title=ledger[key]["title"],
                        owner_mid=ledger[key]["owner_mid"],
                        owner_name=ledger[key]["owner_name"],
                        tname=ledger[key]["tname"],
                        source=source,
                        increment=increment,
                    )
                except Exception as exc:
                    logger.warning(
                        f"[BiliBot] 永久视频去重账本写入失败，已保留 JSON: {exc}"
                    )
        return True

    async def forget(self, bvids):
        """误记的观看痕迹：从备份和 sqlite 一起去掉。"""
        bvids = set(bvids)
        if not bvids:
            return
        async with self._lock:
            ledger = self._ledger()
            for bvid in bvids:
                ledger.pop(bvid, None)
            self.json_store._save_json(SEEN_VIDEOS_FILE, ledger)
            layered = self._store()
            if layered is not None:
                await layered.seen_videos.forget(bvids)
