"""记忆系统：存储、检索、压缩、上下文构建。

上下文优先级（评论回复场景）：
  第一层（主上下文）：永久记忆 + 视频/动态内容 + 本评论区所有对话(oid) + 当前评论线(thread)
  第二层（认识这人）：用户画像/印象（不拉聊天记录）
  第三层（相关调取）：全局语义搜索（高阈值，不相关不注入）
"""
import hashlib
import re
import json
from datetime import datetime
from astrbot.api import logger
from .security import readable_scopes
from .config import (
    MAX_SEMANTIC_RESULTS, MEMORY_FILE, MEMORY_SYNC_STATE_FILE, PERMANENT_MEMORY_FILE,
    THREAD_COMPRESS_THRESHOLD,
    OID_COMPRESS_THRESHOLD, OID_KEEP_RECENT,
    USER_MEMORY_COMPRESS_THRESHOLD, USER_MEMORY_KEEP_RECENT,
    USER_PROFILE_FILE,
    WATCH_LOG_FILE, COMMENTED_FILE, VIDEO_MEMORY_FILE, EXTERNAL_MEMORY_FILE,
    SEEN_VIDEOS_FILE,
)


class MemoryMixin:
    """记忆的增删改查、语义搜索、压缩与上下文构建。"""

    # ══════════════════════════════════════
    #  归一化 & 基础
    # ══════════════════════════════════════
    def _normalize_memory_entry(self, record):
        rec = dict(record)
        if not rec.get("memory_type"):
            thread_id = str(rec.get("thread_id", ""))
            text = str(rec.get("text", ""))
            if thread_id == "dynamic" or "Bot发了一条动态" in text:
                rec["memory_type"] = "dynamic"
            elif thread_id.startswith("video:") or thread_id == "proactive_watch" or "Bot看了视频" in text or "视频分析记忆" in text:
                rec["memory_type"] = "video"
            elif text.startswith("[记忆压缩]") or text.startswith("[评论区总结]"):
                rec["memory_type"] = "user_summary"
            else:
                rec["memory_type"] = "chat"
        # level 归一化：无 level 的旧记忆保持不设（由 consolidation 迁移处理）
        # 注意：不要 setdefault("level", None)，那会导致迁移检测 key 存在而跳过
        return rec

    @staticmethod
    def _memory_timestamp(value):
        if isinstance(value, (int, float)):
            return float(value)
        raw = str(value or "").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw[:19], fmt).timestamp()
            except ValueError:
                continue
        return datetime.now().timestamp()

    def _video_memory_windows(self):
        config = getattr(self, "config", {}) or {}
        try:
            detail_days = max(1, min(60, int(config.get("VIDEO_MEMORY_DETAIL_DAYS", 15) or 15)))
        except (TypeError, ValueError):
            detail_days = 15
        try:
            fade_days = max(30, min(730, int(config.get("VIDEO_MEMORY_FADE_DAYS", 90) or 90)))
        except (TypeError, ValueError):
            fade_days = 90
        return detail_days, max(detail_days, fade_days)

    def _video_memory_stage_at(self, record, now_ts=None):
        """Return detail/long_term/faded without mutating the memory."""
        explicit = str(record.get("video_memory_stage") or "").strip()
        if explicit == "faded":
            return "faded"
        created_at = self._memory_timestamp(
            record.get("created_at") or record.get("time")
        )
        detail_days, fade_days = self._video_memory_windows()
        current = float(now_ts if now_ts is not None else datetime.now().timestamp())
        try:
            detail_until = float(record.get("video_detail_until"))
        except (TypeError, ValueError, OverflowError):
            detail_until = created_at + detail_days * 86400
        try:
            fade_after = float(record.get("video_fade_after"))
        except (TypeError, ValueError, OverflowError):
            fade_after = created_at + fade_days * 86400
        fade_after = max(detail_until, fade_after)
        if current >= fade_after:
            return "faded"
        if current >= detail_until:
            return "long_term"
        return explicit if explicit in {"detail", "long_term"} else "detail"

    def _memory_recall_weight(self, record):
        """Reduce unsolicited recall of older video memories."""
        if self._normalize_memory_entry(record).get("memory_type") != "video":
            return 1.0
        return {
            "detail": 1.0,
            "long_term": 0.68,
            # 三个月后只在高度相关时偶尔浮现；永久 BV 去重由 seen_videos 独立负责。
            "faded": 0.52,
        }.get(self._video_memory_stage_at(record), 1.0)

    def _prepare_memory_entry(self, record):
        rec = self._normalize_memory_entry(record)
        if not rec.get("rpid"):
            stable = {
                key: value
                for key, value in rec.items()
                if key not in {"embedding", "_sqlite_id", "created_at"}
            }
            digest = hashlib.sha256(
                json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:24]
            rec["rpid"] = f"legacy_{digest}"
        source = str(rec.get("source") or "").strip().lower()
        thread_id = str(rec.get("thread_id") or "").strip().lower()
        memory_type = str(rec.get("memory_type") or "chat").strip().lower()
        known_scopes = {
            "bili_comment", "bili_dm", "bili_live", "qq_group", "qq_private",
            "admin", "background", "proactive", "self", "analytics",
        }
        requested_scope = str(rec.get("scope") or "").strip()
        if requested_scope in known_scopes:
            scope = requested_scope
        elif "private" in source or thread_id.startswith("private:"):
            scope = "bili_dm"
        elif "live" in source or thread_id.startswith("live:"):
            scope = "bili_live"
        elif source.startswith("qq_private"):
            scope = "qq_private"
        elif source.startswith("qq"):
            scope = "qq_group"
        elif memory_type in {"video", "dynamic", "bangumi"} or source in {
            "proactive", "tool_watch", "private_share", "private_tool"
        }:
            scope = "proactive"
        elif str(rec.get("user_id") or "") == "self":
            scope = "self"
        else:
            scope = "bili_comment"
        rec["scope"] = scope
        uid = str(rec.get("user_id") or "").strip()
        if not rec.get("actor_id"):
            if uid == "self":
                rec["actor_id"] = "sys:self"
            elif uid:
                platform = "qq" if scope.startswith("qq_") else "bili"
                rec["actor_id"] = f"{platform}:{uid}"
        rec["created_at"] = self._memory_timestamp(rec.get("created_at") or rec.get("time"))
        if memory_type == "video":
            detail_days, fade_days = self._video_memory_windows()
            rec.setdefault("video_detail_until", rec["created_at"] + detail_days * 86400)
            rec.setdefault("video_fade_after", rec["created_at"] + fade_days * 86400)
            rec.setdefault("video_memory_stage", self._video_memory_stage_at(rec))
            trace = str(
                rec.get("video_summary")
                or rec.get("summary")
                or rec.get("review")
                or rec.get("text")
                or ""
            ).strip()
            if trace:
                rec.setdefault("video_summary", trace[:180].rstrip())
        promoted_at = rec.get("promoted_at")
        if promoted_at and not rec.get("promoted_at_ts"):
            rec["promoted_at_ts"] = self._memory_timestamp(promoted_at)
        return rec

    @staticmethod
    def _memory_backup_records(records):
        result = []
        for item in records:
            clean = dict(item)
            clean.pop("_sqlite_id", None)
            result.append(clean)
        return result

    def _mark_memory_sync_pending(self, reason=""):
        self._save_json(
            MEMORY_SYNC_STATE_FILE,
            {
                "pending": True,
                "reason": str(reason or "")[:300],
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    def _clear_memory_sync_pending(self):
        self._save_json(
            MEMORY_SYNC_STATE_FILE,
            {
                "pending": False,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    async def _initialize_unified_memory(self):
        """首次导入旧 JSON；此后 SQLite 为主库，JSON 只保留兼容备份。"""
        layered = getattr(self, "layered_runtime", None)
        if layered is None or not layered.is_open:
            return False
        legacy = [
            self._prepare_memory_entry(item)
            for item in list(self._memory)
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        sync_state = self._load_json(MEMORY_SYNC_STATE_FILE, {})
        migrated = bool(await layered.db.kv_get("unified_memory_v2_ready", False))
        mapped_count = await layered.memories.legacy_count()
        if (
            not migrated
            or bool(sync_state.get("pending"))
            or (bool(legacy) and mapped_count == 0)
        ):
            await layered.memories.replace_legacy(legacy)
            await layered.db.kv_set("unified_memory_v2_ready", True)
        self._memory = [
            self._normalize_memory_entry(item)
            for item in await layered.memories.load_legacy()
        ]
        self._save_json(MEMORY_FILE, self._memory_backup_records(self._memory))
        self._clear_memory_sync_pending()
        logger.info(
            f"[BiliBot] 统一记忆库已就绪: SQLite {len(self._memory)} 条；"
            "memory.json 保留为兼容备份"
        )
        return True

    def _legacy_seen_video_records(self):
        """Collect all old, possibly capped sources for one-time permanent import."""
        records = []

        def add(bvid, item=None, source="legacy"):
            data = item if isinstance(item, dict) else {}
            key = str(bvid or data.get("bvid") or "").strip()
            if len(key) < 4 or key[:2].lower() != "bv":
                return
            first_seen = self._memory_timestamp(
                data.get("first_seen_at")
                or data.get("time")
                or data.get("watched_at")
            )
            last_related = self._memory_timestamp(
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

        watch_log = self._load_json(WATCH_LOG_FILE, [])
        for item in (watch_log if isinstance(watch_log, list) else []):
            if isinstance(item, dict):
                add(item.get("bvid"), item, "watch_log")
        commented = self._load_json(COMMENTED_FILE, [])
        for item in (
            commented if isinstance(commented, (list, tuple, set)) else []
        ):
            if isinstance(item, dict):
                add(item.get("bvid"), item, "commented")
            else:
                add(item, source="commented")
        video_memory = self._load_json(VIDEO_MEMORY_FILE, {})
        for bvid, item in (
            video_memory.items() if isinstance(video_memory, dict) else []
        ):
            add(bvid, item, "video_memory")
        external_memory = self._load_json(EXTERNAL_MEMORY_FILE, {})
        for bvid, item in (
            external_memory.items() if isinstance(external_memory, dict) else []
        ):
            add(bvid, item, "external_memory")
        seen_backup = self._load_json(SEEN_VIDEOS_FILE, {})
        for bvid, item in (
            seen_backup.items() if isinstance(seen_backup, dict) else []
        ):
            add(bvid, item, "seen_backup")
        for item in self._memory:
            if self._match_memory_type(item, {"video"}):
                add(item.get("bvid"), item, "semantic_memory")
        return records

    async def _initialize_seen_videos(self):
        """Idempotently migrate every legacy BV source into the permanent ledger."""
        layered = getattr(self, "layered_runtime", None)
        if layered is None or not layered.is_open:
            return 0
        records = self._legacy_seen_video_records()
        created = await layered.seen_videos.import_many(records)
        total = await layered.seen_videos.count()
        logger.info(
            f"[BiliBot] 永久视频去重账本已就绪: {total} 条"
            f"（本次迁移新增 {created} 条）"
        )
        return total

    async def _seen_video_bvids(self):
        """Return permanent seen BVs, with legacy sources as a safe fallback."""
        result = {
            str(item.get("bvid") or "")
            for item in self._legacy_seen_video_records()
            if item.get("bvid")
        }
        layered = getattr(self, "layered_runtime", None)
        if layered is not None and layered.is_open:
            try:
                result.update(await layered.seen_videos.all_bvids())
            except Exception as exc:
                logger.warning(f"[BiliBot] 读取永久视频去重账本失败，使用兼容记录: {exc}")
        return result

    async def _has_seen_video(self, bvid):
        key = str(bvid or "").strip()
        if not key:
            return False
        return ("BV" + key[2:] if key[:2].lower() == "bv" else key) in (
            await self._seen_video_bvids()
        )

    async def _seen_video_record(self, bvid):
        """Return the lightweight trace without pulling video content into the ledger."""
        key = str(bvid or "").strip()
        if len(key) < 4 or key[:2].lower() != "bv":
            return None
        key = "BV" + key[2:]
        ledger = self._load_json(SEEN_VIDEOS_FILE, {})
        if isinstance(ledger, dict) and isinstance(ledger.get(key), dict):
            return dict(ledger[key])
        layered = getattr(self, "layered_runtime", None)
        if layered is not None and layered.is_open:
            try:
                return await layered.seen_videos.get(key)
            except Exception as exc:
                logger.warning(f"[BiliBot] 读取视频观看痕迹失败: {exc}")
        return None

    async def _mark_video_seen(
        self, bvid, info=None, source="watch", *, increment=True
    ):
        """Persist a watched BV before capped activity logs can forget it."""
        data = info if isinstance(info, dict) else {}
        key = str(bvid or data.get("bvid") or "").strip()
        if len(key) < 4 or key[:2].lower() != "bv":
            return False
        key = "BV" + key[2:]
        lock = getattr(self, "_seen_video_write_lock", None)
        if lock is None:
            import asyncio
            lock = self._seen_video_write_lock = asyncio.Lock()
        async with lock:
            ledger = self._load_json(SEEN_VIDEOS_FILE, {})
            if not isinstance(ledger, dict):
                ledger = {}
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
            self._save_json(SEEN_VIDEOS_FILE, ledger)
            layered = getattr(self, "layered_runtime", None)
            if layered is not None and layered.is_open:
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

    async def _save_memory_entry(self, record):
        lock = getattr(self, "_memory_write_lock", None)
        if lock is None:
            import asyncio
            lock = self._memory_write_lock = asyncio.Lock()
        async with lock:
            await self._save_memory_entry_unlocked(record)

    async def _save_memory_entry_unlocked(self, record):
        rec = self._prepare_memory_entry(record)
        key = str(rec["rpid"])
        self._memory = [m for m in self._memory if str(m.get("rpid")) != key]
        self._memory.append(rec)
        layered = getattr(self, "layered_runtime", None)
        try:
            if layered is None or not layered.is_open:
                raise RuntimeError("SQLite memory store is unavailable")
            sync_state = self._load_json(MEMORY_SYNC_STATE_FILE, {})
            if bool(sync_state.get("pending")):
                await layered.memories.replace_legacy(self._memory)
            else:
                await layered.memories.upsert_legacy(rec)
            self._clear_memory_sync_pending()
        except Exception as exc:
            # 保留旧文件兜底，并明确标记；下次 SQLite 成功启动会自动补同步。
            self._mark_memory_sync_pending(exc)
            logger.warning(f"[BiliBot] 统一记忆写入失败，已保留 JSON 待同步: {exc}")
        self._save_json(MEMORY_FILE, self._memory_backup_records(self._memory))

    async def _replace_memory_snapshot(self, assume_locked=False):
        lock = getattr(self, "_memory_write_lock", None)
        if not assume_locked:
            if lock is None:
                import asyncio
                lock = self._memory_write_lock = asyncio.Lock()
            async with lock:
                return await self._replace_memory_snapshot_unlocked()
        return await self._replace_memory_snapshot_unlocked()

    async def _replace_memory_snapshot_unlocked(self):
        prepared = [
            self._prepare_memory_entry(item)
            for item in self._memory
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        # rpid 是兼容主键；保留最后一次更新，避免旧文件中的重复项继续扩散。
        unique = {str(item["rpid"]): item for item in prepared}
        self._memory = list(unique.values())
        layered = getattr(self, "layered_runtime", None)
        try:
            if layered is None or not layered.is_open:
                raise RuntimeError("SQLite memory store is unavailable")
            await layered.memories.replace_legacy(self._memory)
            self._clear_memory_sync_pending()
        except Exception as exc:
            self._mark_memory_sync_pending(exc)
            logger.warning(f"[BiliBot] 统一记忆快照同步失败，已保留 JSON 待同步: {exc}")
        self._save_json(MEMORY_FILE, self._memory_backup_records(self._memory))

    @staticmethod
    def _is_derived_memory(record):
        """Return whether a memory is a generated summary of other memories."""
        rpid = str(record.get("rpid") or "")
        text = str(record.get("text") or "")
        return bool(
            record.get("derived_from_rpids")
            or record.get("summary_kind")
            or rpid.startswith(("oid_compressed_", "thread_compressed_", "compressed_"))
            or text.startswith(("[评论区总结]", "[评论线总结]", "[记忆压缩]"))
        )

    @staticmethod
    def _derived_memory_id(prefix, records):
        """Build a collision-free, reproducible ID from the source memory IDs."""
        source_ids = sorted(
            {str(item.get("rpid") or "") for item in records if item.get("rpid")}
        )
        digest = hashlib.sha256(
            json.dumps(source_ids, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:24]
        return f"{prefix}_{digest}"

    @staticmethod
    def _legacy_summary_depends_on(summary, target):
        """Conservatively infer dependencies for summaries created before provenance existed."""
        if not MemoryMixin._is_derived_memory(summary):
            return False
        if summary.get("derived_from_rpids"):
            return False

        summary_thread = str(summary.get("thread_id") or "")
        target_thread = str(target.get("thread_id") or "")
        if summary_thread and target_thread and summary_thread == target_thread:
            return True

        summary_oid = str(summary.get("oid") or "")
        target_oid = str(target.get("oid") or "")
        if summary_oid and target_oid and summary_oid == target_oid:
            return True

        # 用户压缩总结没有可靠的评论线/视频归属；旧数据只能按用户和 scope
        # 保守失效，防止删除的原文仍从旧摘要中被召回。评论线/评论区摘要
        # 不走此兜底，否则会误删同一用户在别处的独立摘要。
        summary_rpid = str(summary.get("rpid") or "")
        summary_text = str(summary.get("text") or "")
        is_user_summary = bool(
            summary.get("summary_kind") == "user"
            or summary_rpid.startswith("compressed_")
            or summary_text.startswith("[记忆压缩]")
        )
        if not is_user_summary:
            return False
        summary_uid = str(summary.get("user_id") or "")
        target_uid = str(target.get("user_id") or "")
        if summary_uid not in {"", "self", "summary"} and summary_uid == target_uid:
            summary_scope = str(summary.get("scope") or "")
            target_scope = str(target.get("scope") or "")
            return bool(summary_scope and summary_scope == target_scope)
        return False

    def _remove_profile_memory_refs(self, removed_rpids):
        """Prune lightweight live-memory references after their memories are deleted."""
        removed = {str(item) for item in removed_rpids if item}
        if not removed:
            return 0
        profiles = self._load_json(USER_PROFILE_FILE, {})
        if not isinstance(profiles, dict):
            return 0
        changed = 0
        for profile in profiles.values():
            if not isinstance(profile, dict):
                continue
            live = profile.get("live")
            if not isinstance(live, dict):
                continue
            refs = live.get("memory_refs")
            if not isinstance(refs, list):
                continue
            kept = [ref for ref in refs if str(ref) not in removed]
            changed += len(refs) - len(kept)
            if len(kept) != len(refs):
                live["memory_refs"] = kept
        if changed:
            self._save_json(USER_PROFILE_FILE, profiles)
        return changed

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

        lock = getattr(self, "_memory_write_lock", None)
        if lock is None:
            import asyncio
            lock = self._memory_write_lock = asyncio.Lock()
        async with lock:
            by_id = {
                str(item.get("rpid") or ""): item
                for item in self._memory
                if isinstance(item, dict) and item.get("rpid")
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
                    if candidate_id in removed or not self._is_derived_memory(candidate):
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
                            self._legacy_summary_depends_on(candidate, item)
                            for item in targets
                        )
                    if depends:
                        removed.add(candidate_id)
                        targets.append(candidate)
                        changed = True

            self._memory = [
                item for item in self._memory
                if str(item.get("rpid") or "") not in removed
            ]
            await self._replace_memory_snapshot(assume_locked=True)
            profile_refs_removed = self._remove_profile_memory_refs(removed)

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

    @staticmethod
    def _memory_type_label(memory_type):
        return {"chat": "交流", "video": "视频", "dynamic": "动态", "live": "直播", "user_summary": "用户总结"}.get(memory_type, memory_type)

    def _match_memory_type(self, memory, memory_types=None):
        if not memory_types:
            return True
        return self._normalize_memory_entry(memory).get("memory_type") in set(memory_types)

    @staticmethod
    def _reader_scope_for_channel(channel):
        return {
            "private": "bili_dm",
            "live": "bili_live",
            "qq_private": "qq_private",
            "qq_group": "qq_group",
        }.get(str(channel or "comment").strip().lower(), "bili_comment")

    def _memory_visible_to(self, memory, reader_scope, user_id=""):
        allowed = {scope.value for scope in readable_scopes(reader_scope)}
        memory_scope = str(memory.get("scope") or self._prepare_memory_entry(memory).get("scope"))
        if memory_scope not in allowed:
            return False
        # 全局联想不得把另一位用户的个人对话/总结套到当前用户身上。
        if user_id and self._match_memory_type(memory, {"chat", "live", "user_summary"}):
            remembered_uid = str(memory.get("user_id") or "")
            if remembered_uid not in {"", "self", "summary", str(user_id)}:
                return False
        return True

    # ══════════════════════════════════════
    #  写入记忆
    # ══════════════════════════════════════
    async def _save_memory_record(self, rpid, thread_id, user_id, username, content, reply_text, source="bilibili", oid=0, bvid="", video_title=""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        text = f"[{now}] 用户{user_id}({username})说：{content} | Bot回复：{reply_text}"
        emb = await self._get_embedding(text)
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
        await self._save_memory_entry(rec)

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
        emb = await self._get_embedding(text)
        if emb:
            rec["embedding"] = emb
        await self._save_memory_entry(rec)
        if memory_type == "video" and rec.get("owner_mid") and rec.get("bvid"):
            self._link_video_to_user_profile(
                rec.get("owner_mid"),
                rec.get("owner_name") or rec.get("up_name") or "",
                rec.get("bvid"),
                rec.get("video_title") or rec.get("title") or "",
                "uploaded_by",
            )

    # ══════════════════════════════════════
    #  检索
    # ══════════════════════════════════════
    def _get_thread_memories(self, thread_id):
        """当前评论线（reply chain）的对话，返回结构化记录"""
        docs = [m for m in self._memory if m.get("thread_id") == str(thread_id) and self._match_memory_type(m, {"chat"})]
        docs.sort(key=lambda x: x.get("time", ""))
        return docs

    def _get_oid_memories(self, oid, exclude_thread_id=None):
        """同一评论区（oid）下的所有对话记忆，不限用户。排除当前 thread 避免重复。"""
        oid_str = str(oid)
        docs = [
            m for m in self._memory
            if m.get("oid") == oid_str
            and self._match_memory_type(m, {"chat", "user_summary"})
            and (exclude_thread_id is None or m.get("thread_id") != str(exclude_thread_id))
        ]
        docs.sort(key=lambda x: x.get("time", ""))
        return docs

    @staticmethod
    def _format_conversation_turn(m):
        """将一条记忆格式化为清晰的对话轮次，标注uid、时间、视频来源"""
        uid = m.get("user_id", "?")
        name = m.get("username", "?")
        t = m.get("time", "?")
        text = m.get("text", "")
        # 视频来源标注
        video_tag = ""
        vt = m.get("video_title", "")
        bv = m.get("bvid", "")
        if vt and bv:
            video_tag = f" [视频《{vt}》({bv})]"
        elif bv:
            video_tag = f" [{bv}]"
        # 旧格式兼容：从text中提取内容
        import re
        match = re.search(r'说：(.+?)\s*\|\s*Bot回复：(.+)$', text)
        if match:
            user_said = match.group(1).strip()
            bot_said = match.group(2).strip()
            return f"[{t}]{video_tag} {name}(uid:{uid})：{user_said}\n[{t}] Bot：{bot_said}"
        return f"[{t}]{video_tag} {text}"

    @staticmethod
    def _format_oid_memories_grouped(docs):
        """将评论区记忆按用户分组格式化，防止窜台，标注视频来源"""
        from collections import OrderedDict
        import re
        groups = OrderedDict()
        # 提取这组记忆的视频来源（取第一条有信息的）
        video_label = ""
        for m in docs:
            vt = m.get("video_title", "")
            bv = m.get("bvid", "")
            if vt:
                video_label = f"《{vt}》({bv})" if bv else f"《{vt}》"
                break
            elif bv:
                video_label = bv
                break
        for m in docs:
            uid = m.get("user_id", "?")
            name = m.get("username", "?")
            key = f"{name}(uid:{uid})"
            if key not in groups:
                groups[key] = []
            t = m.get("time", "?")
            text = m.get("text", "")
            match = re.search(r'说：(.+?)\s*\|\s*Bot回复：(.+)$', text)
            if match:
                groups[key].append(f"  [{t}] {name}：{match.group(1).strip()}")
                groups[key].append(f"  [{t}] Bot：{match.group(2).strip()}")
            else:
                groups[key].append(f"  [{t}] {text}")
        lines = []
        header_suffix = f" （视频：{video_label}）" if video_label else ""
        for user_key, turns in groups.items():
            lines.append(f"── 与{user_key}的对话{header_suffix} ──")
            lines.extend(turns)
        return lines

    def _get_bvid_memories(self, bvid, exclude_oid=None, reader_scope="bili_comment", user_id=""):
        """按bvid调取所有与该视频相关的历史记忆（主动看视频、以前的评论区互动等）。
        排除当前oid避免和评论区记忆重复。"""
        exclude_oid_str = str(exclude_oid) if exclude_oid else ""
        docs = [
            m for m in self._memory
            if (m.get("bvid") == bvid or m.get("thread_id") == f"video:{bvid}")
            and (not exclude_oid_str or m.get("oid", "") != exclude_oid_str)
            and self._memory_visible_to(m, reader_scope, user_id)
        ]
        docs.sort(key=lambda x: x.get("time", ""))
        return [self._format_memory_with_meta(m) for m in docs]

    async def _get_user_semantic_memories(self, user_id, query_text, memory_types=None, reader_scope="bili_comment"):
        um = [
            m for m in self._memory
            if m.get("user_id") == str(user_id)
            and "embedding" in m
            and self._match_memory_type(m, memory_types or {"chat", "user_summary"})
            and self._memory_visible_to(m, reader_scope, user_id)
        ]
        if not um:
            return []
        qe = await self._get_embedding(query_text)
        if not qe:
            return []
        scored = [
            (
                self._cosine_similarity(qe, m["embedding"])
                * self._memory_recall_weight(m),
                m["text"],
            )
            for m in um
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [t for s, t in scored[:MAX_SEMANTIC_RESULTS] if s > 0.6]

    async def _search_memories_raw(self, query_text, limit=5, source=None, memory_types=None, user_id=None, score_threshold=0.5, reader_scope="admin"):
        """底层语义搜索：返回 [(score, record), ...]"""
        cands = list(self._memory)
        if source:
            cands = [m for m in cands if m.get("source") == source]
        if user_id is not None:
            cands = [m for m in cands if m.get("user_id") == str(user_id)]
        cands = [
            m for m in cands
            if self._memory_visible_to(m, reader_scope, str(user_id or ""))
        ]
        cands = [self._normalize_memory_entry(m) for m in cands if self._match_memory_type(m, memory_types)]
        cands = [m for m in cands if "embedding" in m]
        if not cands:
            return []
        qe = await self._get_embedding(query_text)
        if not qe:
            return []
        scored = [
            (
                self._cosine_similarity(qe, m["embedding"])
                * self._memory_recall_weight(m),
                m,
            )
            for m in cands
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [(s, m) for s, m in scored[:limit] if s > score_threshold]

    async def _search_memories(self, query_text, limit=5, source=None, memory_types=None, user_id=None, score_threshold=0.5, reader_scope="admin"):
        """语义搜索，返回格式化的文本列表"""
        raw = await self._search_memories_raw(query_text, limit=limit, source=source, memory_types=memory_types, user_id=user_id, score_threshold=score_threshold, reader_scope=reader_scope)
        results = []
        for s, m in raw:
            tag = f"[{m.get('source', '?')}]" if not source else ""
            type_tag = f"[{self._memory_type_label(m.get('memory_type', '?'))}]"
            results.append(f"{tag}{type_tag}{m['text']}")
        return results

    # ══════════════════════════════════════
    #  压缩
    # ══════════════════════════════════════
    async def _compress_oid_memory(self, oid):
        """同一评论区（oid）记忆太多时压缩旧记录"""
        oid_str = str(oid)
        oid_mems = [m for m in self._memory if m.get("oid") == oid_str and self._match_memory_type(m, {"chat"})]
        if len(oid_mems) <= OID_COMPRESS_THRESHOLD:
            return
        cooldowns = getattr(self, "_compress_cooldowns", {})
        import time as _time
        cooldown_key = f"oid_{oid_str}"
        if cooldown_key in cooldowns and _time.time() - cooldowns[cooldown_key] < 3600:
            return
        logger.info(f"[BiliBot] 🗜️ 评论区 {oid} 记忆达 {len(oid_mems)} 条，压缩...")
        oid_mems.sort(key=lambda x: x.get("time", ""))
        old = oid_mems[:-OID_KEEP_RECENT]
        old_texts = "\n".join([m["text"] for m in old])
        prompt = (
            f"请用150字以内总结以下同一视频评论区下的所有对话要点。\n"
            f"保留：关键话题、不同用户的观点、重要信息。\n"
            f"去掉：重复的寒暄、无意义的回复。\n\n"
            f"{old_texts[:4000]}\n\n直接输出总结，不加前缀。"
        )
        try:
            summary = await self._llm_call(prompt, max_tokens=300)
            if not summary:
                cooldowns[cooldown_key] = _time.time()
                self._compress_cooldowns = cooldowns
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            emb = await self._get_embedding(summary)
            comp = {
                "rpid": self._derived_memory_id("oid_compressed", old),
                "thread_id": f"oid_summary:{oid_str}",
                "oid": oid_str,
                "user_id": "summary",
                "time": now,
                "text": f"[评论区总结] {summary}",
                "source": "bilibili",
                "memory_type": "user_summary",
                "summary_kind": "oid",
                "derived_from_rpids": sorted({str(m["rpid"]) for m in old}),
                "level": "long_term", "importance": 7, "promoted_at": now,
            }
            # 保留视频元数据（从被压缩的记录中提取）
            for m in old:
                if m.get("bvid"):
                    comp["bvid"] = m["bvid"]
                    break
            for m in old:
                if m.get("video_title"):
                    comp["video_title"] = m["video_title"]
                    break
            if emb:
                comp["embedding"] = emb
            old_rpids = {m["rpid"] for m in old}
            self._memory = [m for m in self._memory if m.get("rpid") not in old_rpids]
            self._memory.append(self._prepare_memory_entry(comp))
            await self._replace_memory_snapshot()
            logger.info(f"[BiliBot] 🗜️ 评论区 {oid} 压缩：{len(old)} 条 → 1 条总结")
        except Exception as e:
            cooldowns[cooldown_key] = _time.time()
            self._compress_cooldowns = cooldowns
            logger.error(f"[BiliBot] 评论区压缩失败（1小时后重试）：{e}")

    async def _compress_thread_memory(self, thread_id):
        thread_mems = [m for m in self._memory if m.get("thread_id") == str(thread_id) and self._match_memory_type(m, {"chat"})]
        if len(thread_mems) <= THREAD_COMPRESS_THRESHOLD:
            return
        cooldowns = getattr(self, "_compress_cooldowns", {})
        import time as _time
        cooldown_key = f"thread_{thread_id}"
        if cooldown_key in cooldowns and _time.time() - cooldowns[cooldown_key] < 3600:
            return
        thread_mems.sort(key=lambda x: x.get("time", ""))
        keep_recent = 3
        old = thread_mems[:-keep_recent]
        old_texts = "\n".join([m["text"] for m in old])
        prompt = f"请用80字以内总结以下同一评论线下的对话要点，保留关键信息和话题走向：\n\n{old_texts[:3000]}\n\n直接输出总结，不加前缀。"
        try:
            summary = await self._llm_call(prompt, max_tokens=200)
            if not summary:
                cooldowns[cooldown_key] = _time.time()
                self._compress_cooldowns = cooldowns
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            emb = await self._get_embedding(summary)
            # 保留oid字段
            old_oid = old[0].get("oid", "")
            comp = {
                "rpid": self._derived_memory_id("thread_compressed", old),
                "thread_id": str(thread_id),
                "user_id": old[0].get("user_id", ""),
                "time": now,
                "text": f"[评论线总结] {summary}",
                "source": "bilibili",
                "memory_type": "chat",
                "summary_kind": "thread",
                "derived_from_rpids": sorted({str(m["rpid"]) for m in old}),
                "level": "long_term", "importance": 6, "promoted_at": now,
            }
            if old_oid:
                comp["oid"] = str(old_oid)
            # 保留视频元数据
            for m in old:
                if m.get("bvid"):
                    comp["bvid"] = m["bvid"]
                    break
            for m in old:
                if m.get("video_title"):
                    comp["video_title"] = m["video_title"]
                    break
            if emb:
                comp["embedding"] = emb
            old_rpids = {m["rpid"] for m in old}
            self._memory = [m for m in self._memory if m.get("rpid") not in old_rpids]
            self._memory.append(self._prepare_memory_entry(comp))
            await self._replace_memory_snapshot()
            logger.info(f"[BiliBot] 🗜️ 评论线 {thread_id} 压缩：{len(old)} 条 → 1 条总结")
        except Exception as e:
            cooldowns[cooldown_key] = _time.time()
            self._compress_cooldowns = cooldowns
            logger.error(f"[BiliBot] 评论线压缩失败（1小时后重试）：{e}")

    async def _compress_user_memory(self, user_id, username, memory_scope="bili_comment"):
        """按用户且按记忆域压缩，禁止把评论、私信和直播揉进同一份总结。"""
        um = [
            m for m in self._memory
            if m.get("user_id") == str(user_id)
            and self._match_memory_type(m, {"chat"})
            and str(m.get("scope") or self._prepare_memory_entry(m).get("scope"))
            == str(memory_scope)
        ]
        if len(um) <= USER_MEMORY_COMPRESS_THRESHOLD:
            return
        # 冷却机制：压缩失败后1小时内不重试，避免反复浪费 Token
        cooldowns = getattr(self, "_compress_cooldowns", {})
        import time as _time
        cooldown_key = f"user_{user_id}"
        if cooldown_key in cooldowns and _time.time() - cooldowns[cooldown_key] < 3600:
            return
        logger.info(f"[BiliBot] 🗜️ {username} 记忆达 {len(um)} 条，压缩...")
        um.sort(key=lambda x: x.get("time", ""))
        old = um[:-USER_MEMORY_KEEP_RECENT]
        old_texts = "\n".join([m["text"] for m in old])
        prompt = (
            f'请根据以下与用户"{username}"的历史互动，完成：\n'
            f"1. 总结（100字以内）\n2. 3-5个标签\n3. 提取用户个人信息\n\n"
            f'历史：\n{old_texts[:3000]}\n\nJSON格式：{{"summary":"","tags":[],"user_facts":[]}}'
        )
        try:
            text = await self._llm_call(prompt, max_tokens=400)
            if not text:
                cooldowns[cooldown_key] = _time.time()
                self._compress_cooldowns = cooldowns
                return
            text = self._repair_llm_json(text)
            try:
                result = json.loads(text)
            except Exception:
                result = {"summary": text[:100], "tags": [], "user_facts": []}
            self._update_user_profile(
                user_id,
                impression=result.get("summary") or None,
                new_facts=result.get("user_facts") or None,
                new_tags=result.get("tags") or None,
                source_scope=memory_scope,
            )
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            emb = await self._get_embedding(result.get("summary", ""))
            comp = {
                "rpid": self._derived_memory_id("compressed", old),
                "thread_id": f"compressed:{memory_scope}", "user_id": str(user_id),
                "time": now, "text": f"[记忆压缩] {result.get('summary', '')}",
                "source": "bilibili", "memory_type": "user_summary",
                "scope": str(memory_scope),
                "summary_kind": "user",
                "derived_from_rpids": sorted({str(m["rpid"]) for m in old}),
                "level": "long_term", "importance": 7, "promoted_at": now,
            }
            # 保留元数据（用户可能在多个视频下互动，取最近的）
            for m in reversed(old):
                if m.get("oid"):
                    comp["oid"] = str(m["oid"])
                    break
            for m in reversed(old):
                if m.get("bvid"):
                    comp["bvid"] = m["bvid"]
                    break
            for m in reversed(old):
                if m.get("video_title"):
                    comp["video_title"] = m["video_title"]
                    break
            if emb:
                comp["embedding"] = emb
            old_rpids = {m["rpid"] for m in old}
            self._memory = [m for m in self._memory if m.get("rpid") not in old_rpids]
            self._memory.append(self._prepare_memory_entry(comp))
            await self._replace_memory_snapshot()
            logger.info(f"[BiliBot] 🗜️ 压缩完成：{len(old)} 条 → 1 条")
        except Exception as e:
            cooldowns[cooldown_key] = _time.time()
            self._compress_cooldowns = cooldowns
            logger.error(f"[BiliBot] 记忆压缩失败（{username}，1小时后重试）：{e}")

    # ══════════════════════════════════════
    #  上下文构建（分层优先级）
    # ══════════════════════════════════════
    async def _build_memory_context(self, thread_id, user_id, query_text, oid=0, comment_type=1, channel="comment"):
        """
        三层优先级：
        第一层（主上下文）：永久记忆 + 视频/动态内容 + 评论区对话(oid) + 当前评论线
        第二层（认人）：用户画像/印象
        第三层（联想）：全局相关记忆（高阈值）
        """
        parts = []
        bot_mid = self.config.get("DEDE_USER_ID", "")
        reader_scope = self._reader_scope_for_channel(channel)

        # ── 第一层：主上下文 ──

        # 1.1 永久记忆（自我认知）
        perm = self._load_json(PERMANENT_MEMORY_FILE, [])
        if perm:
            parts.append("【Bot的自我认知】\n" + "\n".join([f"[{p.get('time', '?')}] {p['text']}" for p in perm[-20:]]))

        # 1.2 视频/动态内容
        bvid = ""
        if comment_type == 1 and oid:
            vc, cache_entry = await self._get_video_context(oid, comment_type)
            if vc:
                parts.append(vc)
            if cache_entry:
                bvid = cache_entry.get("bvid", "")
                # UP主画像（知道这个UP主是谁）
                up_mid = str(cache_entry.get("owner_mid", ""))
                if up_mid and up_mid != bot_mid:
                    up_profile = self._get_user_profile_context(up_mid, reader_scope)
                    if up_profile:
                        parts.append(up_profile.replace("【对该用户的了解】", "【该视频UP主的了解】"))
            # 1.2.1 调取与该视频相关的历史记忆（按bvid匹配，排除当前oid避免重复）
            if bvid:
                bvid_mems = self._get_bvid_memories(
                    bvid,
                    exclude_oid=oid,
                    reader_scope=reader_scope,
                    user_id=user_id,
                )
                if bvid_mems:
                    parts.append("【以前关于这个视频的记忆】\n" + "\n".join(bvid_mems[-10:]))
        elif comment_type in (11, 17) and oid:
            dc = await self._get_dynamic_context(oid, comment_type=comment_type)
            if dc:
                parts.append(dc)

        # 1.3 当前评论线（最直接的对话上下文）
        td = self._get_thread_memories(thread_id)
        if td:
            formatted = [self._format_conversation_turn(m) for m in td[-10:]]
            if str(thread_id).startswith("private:"):
                parts.append(
                    "【当前私信对话上下文】以下是你和这位用户此前的一对一私信，"
                    "按时间顺序排列：\n" + "\n".join(formatted)
                )
            else:
                parts.append(
                    "【当前评论线上下文】以下是你和这位用户在同一评论线里的历史对话，"
                    "按时间顺序排列：\n" + "\n".join(formatted)
                )

        # 1.4 本评论区其他对话（同oid，排除当前thread，不限用户）
        if oid:
            oid_mems = self._get_oid_memories(oid, exclude_thread_id=thread_id)
            if oid_mems:
                # 最多取最近15条，按用户分组避免窜台
                recent_oid = oid_mems[-15:]
                grouped = self._format_oid_memories_grouped(recent_oid)
                parts.append(
                    "【本评论区其他用户的对话】以下是同一评论区里你和其他用户的交流记录，"
                    "注意区分不同用户（各自标注了uid），不要把不同人的对话混淆：\n"
                    + "\n".join(grouped)
                )

        # ── 第二层：认人 ──

        # 2.1 当前用户画像（印象+标签+事实，不拉聊天记录）
        upc = self._get_user_profile_context(user_id, reader_scope)
        if upc:
            parts.append(upc)

        # ── 第三层：联想（让模型自行判断相关性） ──

        # 3.1 全局语义搜索（排除本oid的记忆，避免重复；带元数据让模型判断）
        global_mems = await self._search_global_relevant(
            query_text,
            current_oid=oid,
            limit=5,
            reader_scope=reader_scope,
            user_id=user_id,
        )
        if global_mems:
            parts.append(
                "【以下是从记忆中调取的可能相关内容，每条标注了时间和来源。\n"
                "这些是次要参考，不是当前对话的一部分。\n"
                "请自行判断是否与当前话题有关，无关的忽略即可。】\n"
                + "\n".join(global_mems)
            )

        return "\n\n".join(parts) if parts else ""

    async def _search_global_relevant(self, query_text, current_oid=0, limit=5, reader_scope="bili_comment", user_id=""):
        """全局语义搜索，排除当前 oid，返回带元数据的格式化结果。
        不做硬阈值截断，让模型根据上下文自行判断相关性。"""
        current_oid_str = str(current_oid) if current_oid else ""
        cands = [
            m for m in self._memory
            if "embedding" in m
            and (not current_oid_str or m.get("oid", "") != current_oid_str)
            and self._memory_visible_to(m, reader_scope, user_id)
        ]
        if not cands:
            return []
        qe = await self._get_embedding(query_text)
        if not qe:
            return []
        scored = [
            (
                self._cosine_similarity(qe, m["embedding"])
                * self._memory_recall_weight(m),
                m,
            )
            for m in cands
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        # 取 top N，但最低要有基本的语义相关（0.5 以下基本是噪声）
        results = []
        for s, m in scored[:limit]:
            if s < 0.5:
                break
            results.append(self._format_memory_with_meta(m))
        return results

    @staticmethod
    def _format_memory_with_meta(m):
        """给记忆条目附加元数据标签：类型、级别、时间、来源。"""
        parts = []
        # 类型
        mtype = m.get("memory_type", "chat")
        type_labels = {"chat": "交流", "video": "视频", "dynamic": "动态", "live": "直播", "user_summary": "总结"}
        parts.append(f"[{type_labels.get(mtype, mtype)}]")
        # 级别
        level = m.get("level")
        if level:
            level_labels = {"today": "今日", "recent": "近期", "long_term": "长期"}
            parts.append(f"[{level_labels.get(level, level)}]")
        # 来源
        source = m.get("source", "")
        if source and source != "bilibili":
            parts.append(f"[来源:{source}]")
        # 时间
        t = m.get("time", "")
        if t:
            parts.append(f"[{t}]")
        # 视频标题（如果有）
        vt = m.get("video_title", "")
        if vt:
            parts.append(f"[视频:《{vt}》]")
        # 分区（如果有）
        tn = m.get("tname", "")
        if tn:
            parts.append(f"[分区:{tn}]")
        # 链接（番剧用 ep 链接，普通视频用 bvid 链接）
        ep_id = m.get("ep_id", "")
        bvid = m.get("bvid", "")
        if ep_id:
            parts.append(f"[链接:https://www.bilibili.com/bangumi/play/ep{ep_id}]")
        elif bvid:
            parts.append(f"[链接:https://www.bilibili.com/video/{bvid}]")
        prefix = "".join(parts)
        return f"{prefix} {m.get('text', '')}"
