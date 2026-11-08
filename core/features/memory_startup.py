"""启动时整理记忆：旧 json 导进 sqlite、旧的「看过」导进已看视频账本，顺带修旧版本误记的 QQ 解析卡。

都是幂等的，每次启动跑一遍；``Runner`` 经它。
"""
from astrbot.api import logger

from ..infra.host_view import HostView


class MemoryStartup(HostView):
    HOST_API = frozenset({"memory_docs", "json_store", "layered_runtime", "memories", "seen_videos"})

    async def _initialize_unified_memory(self):
        return await self.memories.initialize()

    @staticmethod
    def _is_unmarked_share_preview(item):
        """Old QQ parse cards were saved as video memories without a watch flag."""
        return (
            isinstance(item, dict)
            and item.get("memory_type") == "video"
            and "content_analyzed" not in item
            and str(item.get("thread_id") or "").startswith(("group_share:", "private_share:"))
        )

    async def _repair_share_preview_seen_records(self):
        """Forget seen marks that only came from QQ parse cards, then flag those cards.

        A BV keeps its mark when any real watch evidence remains, or when the
        ledger entry was written by an actual watch rather than a legacy import.
        """
        if not await self.memories.exists(where=self._is_unmarked_share_preview):
            return 0
        async with self.memories.editing() as memories:
            previews = [item for item in memories.records if self._is_unmarked_share_preview(item)]
            for item in previews:
                item["content_analyzed"] = False
        # A cache hit could copy the card into the video cache as if it were a
        # summary, and that hit then marked the BV as watched.
        video_cache = self.memory_docs.video_memory.read({})
        if not isinstance(video_cache, dict):
            video_cache = {}
        copied = {
            bvid for bvid, item in video_cache.items()
            if isinstance(item, dict) and item.get("source") == "semantic_memory" and (
                str(item.get("thread_id") or "").startswith(("group_share:", "private_share:"))
                or "有人分享了B站视频" in str(item.get("summary") or item.get("analysis") or "")
            )
        }
        if copied:
            for bvid in copied:
                video_cache.pop(bvid, None)
            self.memory_docs.video_memory.write(video_cache)
        watched = {
            item.get("bvid")
            for item in await self.seen_videos.legacy_records(include_ledger=False)
        }
        import_sources = {"", "bilibili", "semantic_memory", "legacy"}
        candidates = ({
            "BV" + str(item.get("bvid"))[2:]
            for item in previews
            if str(item.get("bvid") or "")[:2].lower() == "bv"
        } | copied) - watched
        stale = set()
        for bvid in candidates:
            source = await self.seen_videos.entry_source(bvid)
            if bvid in copied or source is None or source in import_sources:
                stale.add(bvid)
        await self.seen_videos.forget(stale)
        logger.info(
            f"[BiliBot] 已修正 {len(previews)} 条 QQ 解析卡记忆，"
            f"移除 {len(stale)} 条误记的观看痕迹"
        )
        return len(stale)

    async def _initialize_seen_videos(self):
        """Idempotently migrate every legacy BV source into the permanent ledger."""
        if getattr(self, "layered_runtime", None) is None or not self.layered_runtime.is_open:
            return 0
        await self._repair_share_preview_seen_records()
        return await self.seen_videos.import_legacy()
