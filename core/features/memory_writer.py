"""写一条记忆、删一条记忆。

写：拼文字、算向量，交给 ``Memories.save``；视频记忆顺带在 UP 主画像上挂上这个视频。
删：连带作废从它压缩出来的总结（有来源的按来源，旧总结按评论线 / 评论区 / 用户保守推断），
再清掉画像里指向它们的直播引用。
"""
from datetime import datetime

from ..infra.host_view import HostView


class MemoryWriter(HostView):
    HOST_API = frozenset({"memories", "memory_entry", "user_profiles", "vision"})

    async def _save_memory_record(self, rpid, thread_id, user_id, username, content, reply_text, source="bilibili", oid=0, bvid="", video_title=""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        text = f"[{now}] 用户{user_id}({username})说：{content} | Bot回复：{reply_text}"
        emb = await self.vision._get_embedding(text)
        rec = {
            "rpid": str(rpid), "thread_id": str(thread_id),
            "user_id": str(user_id), "username": username,
            "time": now, "text": text, "source": source,
            "memory_type": "chat",
            "level": "today", "importance": 5, "promoted_at": now,
        }
        if oid:
            rec["oid"] = str(oid)
        if bvid:
            rec["bvid"] = bvid
        if video_title:
            rec["video_title"] = video_title
        if emb:
            rec["embedding"] = emb
        await self.memories.save(rec)

    async def _save_self_memory_record(self, thread_id, text, source="bilibili", memory_type="chat", user_id="self", extra=None):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        # 视频/动态类记忆默认 importance 更高
        default_imp = 6 if memory_type in ("video", "dynamic", "live") else 5
        rec = {
            "rpid": f"{thread_id}_{int(datetime.now().timestamp())}",
            "thread_id": str(thread_id),
            "user_id": str(user_id),
            "time": now, "text": text,
            "source": source, "memory_type": memory_type,
            "level": "today", "importance": default_imp, "promoted_at": now,
        }
        if extra:
            rec.update(extra)
        emb = await self.vision._get_embedding(text)
        if emb:
            rec["embedding"] = emb
        await self.memories.save(rec)
        if memory_type == "video" and rec.get("owner_mid") and rec.get("bvid"):
            self.user_profiles.link_video(
                rec.get("owner_mid"),
                rec.get("owner_name") or rec.get("up_name") or "",
                rec.get("bvid"),
                rec.get("video_title") or rec.get("title") or "",
                "uploaded_by",
            )

    async def _delete_memory_by_rpid(self, rpid):
        """Precisely delete one memory and invalidate summaries derived from it.

        This is the storage primitive for future Web management. It deliberately
        does not delete independent user-profile facts; clearing a profile has a
        different meaning and will use a separate operation.
        """
        key = str(rpid or "").strip()
        report = {
            "requested_rpid": key,
            "found": False,
            "deleted_count": 0,
            "invalidated_summary_count": 0,
            "profile_memory_refs_removed": 0,
            "removed_rpids": [],
        }
        if not key:
            return report

        entry = self.memory_entry
        async with self.memories.lock:
            by_id = {
                str(item.get("rpid") or ""): item
                for item in await self.memories.find(where=lambda item: bool(item.get("rpid")))
            }
            target = by_id.get(key)
            if target is None:
                return report

            removed = {key}
            targets = [target]
            changed = True
            while changed:
                changed = False
                for candidate_id, candidate in by_id.items():
                    if candidate_id in removed or not entry._is_derived_memory(candidate):
                        continue
                    raw_sources = candidate.get("derived_from_rpids", [])
                    sources = {
                        str(item)
                        for item in raw_sources
                        if item
                    } if isinstance(raw_sources, (list, tuple, set)) else set()
                    depends = bool(sources & removed)
                    if not depends:
                        depends = any(
                            entry._legacy_summary_depends_on(candidate, item)
                            for item in targets
                        )
                    if depends:
                        removed.add(candidate_id)
                        targets.append(candidate)
                        changed = True

            await self.memories.remove(removed, assume_locked=True)
            profile_refs_removed = self.user_profiles.remove_memory_refs(removed)

        report.update(
            {
                "found": True,
                "deleted_count": len(removed),
                "invalidated_summary_count": max(0, len(removed) - 1),
                "profile_memory_refs_removed": profile_refs_removed,
                "removed_rpids": sorted(removed),
            }
        )
        return report
