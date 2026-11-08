"""一条记忆长什么样：补类型、补域和说话人、视频记忆的老化、召回权重、派生总结怎么认。

都是纯规则，只读配置（视频记忆的老化窗口）。``Memories`` 存一条之前用它整理，
清算、压缩、检索、已看视频账本用它判断类型和派生关系。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from ..infra.host_view import HostView
from ._memory_tags import tags_for


class MemoryEntry(HostView):
    HOST_API = frozenset({"settings"})

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
        detail_days = max(1, min(60, self.settings.int("VIDEO_MEMORY_DETAIL_DAYS") or 15))
        fade_days = max(30, min(730, self.settings.int("VIDEO_MEMORY_FADE_DAYS") or 90))
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
        """召回权重：乘在相似度上，决定排序和能不能过阈值。

        视频按自己的生命周期降权；别的记忆看清算打的分和老没老：
        分高的（约定、个人近况）更容易想起，分低的、半年多没提的更难想起。
        """
        if self._normalize_memory_entry(record).get("memory_type") == "video":
            return {
                "detail": 1.0,
                "long_term": 0.68,
                # 三个月后只在高度相关时偶尔浮现；永久 BV 去重由 seen_videos 独立负责。
                "faded": 0.52,
            }.get(self._video_memory_stage_at(record), 1.0)
        try:
            importance = int(record.get("importance", 6))
        except (TypeError, ValueError):
            importance = 6
        if importance >= 9:
            weight = 1.1
        elif importance == 8:
            weight = 1.05
        elif importance >= 6:
            weight = 1.0
        elif importance == 5:
            weight = 0.95
        else:
            weight = 0.9
        if record.get("aged"):
            weight *= 0.75
        return weight

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
            "proactive", "tool_watch", "private_share", "private_tool", "comment_tool"
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
        rec["tags"] = tags_for(rec)
        return rec

    @staticmethod
    def _memory_type_label(memory_type):
        return {"chat": "交流", "video": "视频", "dynamic": "动态", "live": "直播", "user_summary": "用户总结"}.get(memory_type, memory_type)

    def _match_memory_type(self, memory, memory_types=None):
        if not memory_types:
            return True
        return self._normalize_memory_entry(memory).get("memory_type") in set(memory_types)

    # ══════════════════════════════════════
    #  派生总结（压缩出来的）
    # ══════════════════════════════════════
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
        if not MemoryEntry._is_derived_memory(summary):
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
