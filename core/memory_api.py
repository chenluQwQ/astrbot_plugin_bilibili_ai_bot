"""BiliBot 记忆外部接口 — 供其他插件（如 KuukiYomi、PocketMoney）调用。

用法（在其他插件中）：
    from astrbot.api.star import Context
    # 获取 BiliBot 实例
    bilibot = context.get_registered_star("astrbot_plugin_bilibili_ai_bot")
    if bilibot and hasattr(bilibot, "memory_api"):
        api = bilibot.memory_api
        # 语义搜索
        results = await api.search("某个关键词", user_id="12345", limit=5)
        # 写入记忆
        await api.record("用户做了某事", user_id="12345", username="小明", source="qq")
        # 查询统计
        stats = api.stats()
"""

from copy import deepcopy
from datetime import datetime
import uuid
from astrbot.api import logger
from .config import (
    USER_PROFILE_FILE, WATCH_LOG_FILE, BANGUMI_WATCH_LOG_FILE,
    REPLY_LOG_FILE, PROACTIVE_LOG_FILE, DYNAMIC_LOG_FILE,
)


class BiliBotMemoryAPI:
    """暴露给外部插件的记忆读写接口。

    持有 bot 主实例引用，通过 mixin 方法操作底层数据。
    所有方法都是安全的、不会破坏内部状态。
    """

    def __init__(self, bot):
        self.bot = bot

    api_version = 2

    # ══════════════════════════════════════
    #  查询
    # ══════════════════════════════════════

    async def search(
        self,
        query: str,
        *,
        user_id: str | None = None,
        source: str | None = None,
        memory_types: set[str] | None = None,
        level: str | None = None,
        limit: int = 5,
        score_threshold: float = 0.5,
    ) -> list[dict]:
        """语义搜索记忆。

        Args:
            query: 搜索文本
            user_id: 限定用户
            source: 限定来源 ("bilibili" / "qq")
            memory_types: 限定类型 ("chat" / "video" / "dynamic" / "user_summary")
            level: 限定级别 ("today" / "recent" / "long_term")
            limit: 最大返回条数
            score_threshold: 最低相似度

        Returns:
            [{"rpid", "text", "time", "user_id", "username",
              "memory_type", "level", "importance", "score"}, ...]
        """
        raw = await self.bot._search_memories_raw(
            query,
            limit=limit,
            source=source,
            memory_types=memory_types,
            user_id=user_id,
            score_threshold=score_threshold,
        )
        results = []
        for score, m in raw:
            if level and m.get("level") != level:
                continue
            results.append({
                "rpid": m.get("rpid", ""),
                "text": m.get("text", ""),
                "time": m.get("time", ""),
                "user_id": m.get("user_id", ""),
                "username": m.get("username", ""),
                "memory_type": m.get("memory_type", "chat"),
                "level": m.get("level", "long_term"),
                "importance": m.get("importance", 5),
                "source": m.get("source", ""),
                "bvid": m.get("bvid", ""),
                "session_id": m.get("session_id", ""),
                "live_event_type": m.get("live_event_type", ""),
                "external_event_id": m.get("external_event_id", ""),
                "score": round(score, 4),
            })
        return results[:limit]

    async def search_text(
        self,
        query: str,
        *,
        user_id: str | None = None,
        limit: int = 5,
    ) -> list[str]:
        """简易接口：返回文本列表。"""
        results = await self.search(query, user_id=user_id, limit=limit)
        return [r["text"] for r in results]

    def get_recent_memories(
        self,
        *,
        user_id: str | None = None,
        source: str | None = None,
        memory_types: set[str] | None = None,
        hours: int = 24,
        limit: int = 20,
    ) -> list[dict]:
        """获取最近 N 小时的记忆（无需语义，按时间倒序）。"""
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(hours=hours)
        results = []
        for m in reversed(self.bot._memory):
            if user_id and m.get("user_id") != str(user_id):
                continue
            if source and m.get("source") != source:
                continue
            if memory_types and not self.bot._match_memory_type(m, memory_types):
                continue
            t = m.get("time", "")
            try:
                mt = datetime.strptime(t[:16], "%Y-%m-%d %H:%M")
                if mt < cutoff:
                    break  # 时间有序，可以直接 break
            except (ValueError, TypeError):
                continue
            results.append({
                "rpid": m.get("rpid", ""),
                "text": m.get("text", ""),
                "time": t,
                "user_id": m.get("user_id", ""),
                "username": m.get("username", ""),
                "memory_type": m.get("memory_type", "chat"),
                "level": m.get("level", "long_term"),
                "importance": m.get("importance", 5),
            })
            if len(results) >= limit:
                break
        return results

    def get_user_profile(self, user_id: str) -> dict | None:
        """获取用户画像。"""
        profiles = self.bot._load_json(USER_PROFILE_FILE, {})
        profile = profiles.get(str(user_id))
        if not isinstance(profile, dict):
            return None
        return deepcopy(self.bot._normalize_user_profile(profile))

    def activity_overview(self, date: str = "") -> str:
        """Return the bot's activity overview without requiring an LLM tool loop."""
        target_date = date or datetime.now().strftime("%Y-%m-%d")
        parts = []
        watch = [item for item in self.bot._load_json(WATCH_LOG_FILE, []) if item.get("time", "").startswith(target_date)]
        if watch:
            lines = [f"🎬 看了 {len(watch)} 个视频:"]
            for item in watch:
                lines.append(
                    f"  [{item.get('time', '?').split(' ', 1)[-1]}] 「{item.get('title', '?')[:30]}」"
                    f"{item.get('score', '?')}分 {item.get('mood', '?')} {item.get('review', '')[:40]}"
                )
            parts.append("\n".join(lines))
        bangumi = [item for item in self.bot._load_json(BANGUMI_WATCH_LOG_FILE, []) if item.get("time", "").startswith(target_date)]
        if bangumi:
            lines = [f"📺 看了 {len(bangumi)} 集番剧:"]
            for item in bangumi:
                lines.append(
                    f"  [{item.get('time', '?').split(' ', 1)[-1]}] 《{item.get('title', '?')}》"
                    f"第{item.get('ep_index', '?')}话 {item.get('score', '?')}分 {item.get('mood', '?')}"
                )
            parts.append("\n".join(lines))
        replies = [item for item in self.bot._load_json(REPLY_LOG_FILE, []) if item.get("time", "").startswith(target_date)]
        if replies:
            lines = [f"💬 回复了 {len(replies)} 条评论:"]
            for item in replies[-10:]:
                lines.append(
                    f"  [{item.get('time', '?').split(' ', 1)[-1]}] {item.get('username', '?')}: "
                    f"{item.get('content', '')[:30]} → {item.get('reply', '')[:30]}"
                )
            parts.append("\n".join(lines))
        comments = [item for item in self.bot._load_json(PROACTIVE_LOG_FILE, []) if item.get("time", "").startswith(target_date)]
        if comments:
            lines = [f"📝 发了 {len(comments)} 条主动评论:"]
            for item in comments[-10:]:
                lines.append(
                    f"  [{item.get('time', '?').split(' ', 1)[-1]}] 「{item.get('title', '')[:20]}」"
                    f"{item.get('comment', '')[:40]}"
                )
            parts.append("\n".join(lines))
        dynamics = [item for item in self.bot._load_json(DYNAMIC_LOG_FILE, []) if item.get("time", "").startswith(target_date)]
        if dynamics:
            lines = [f"📢 发了 {len(dynamics)} 条动态:"]
            for item in dynamics[-10:]:
                lines.append(
                    f"  [{item.get('time', '?').split(' ', 1)[-1]}] "
                    f"{(item.get('content') or item.get('text') or '')[:50]}"
                )
            parts.append("\n".join(lines))
        if not parts:
            return f"{target_date} 没有任何活动记录。"
        return f"📋 {target_date} 活动总览:\n\n" + "\n\n".join(parts)

    async def recall_user(
        self,
        user_id: str,
        query: str,
        *,
        memory_limit: int = 4,
        video_limit: int = 2,
        exclude_event_ids: set[str] | None = None,
    ) -> dict:
        """按 UID 召回画像、本人记忆，以及仅限其视频引用的相关视频记忆。"""
        uid = str(user_id)
        profile = self.get_user_profile(uid) or {}
        memories = await self.search(
            query,
            user_id=uid,
            memory_types={"chat", "live", "user_summary"},
            limit=max(1, memory_limit) + len(exclude_event_ids or set()),
            score_threshold=0.45,
        )
        if exclude_event_ids:
            memories = [
                item for item in memories
                if item.get("external_event_id") not in exclude_event_ids
            ]
        memories = memories[:max(1, memory_limit)]
        linked_bvids = {
            str(item.get("bvid"))
            for item in profile.get("video_refs", [])
            if isinstance(item, dict) and item.get("bvid")
        }
        video_memories = []
        if linked_bvids and video_limit > 0:
            query_embedding = await self.bot._get_embedding(query)
            if query_embedding:
                candidates = [
                    item for item in self.bot._memory
                    if str(item.get("bvid", "")) in linked_bvids
                    and self.bot._match_memory_type(item, {"video"})
                    and item.get("embedding")
                ]
                scored = sorted(
                    (
                        (self.bot._cosine_similarity(query_embedding, item["embedding"]), item)
                        for item in candidates
                    ),
                    key=lambda pair: pair[0],
                    reverse=True,
                )
                for score, item in scored:
                    if score < 0.5 or len(video_memories) >= video_limit:
                        break
                    video_memories.append({
                        "bvid": item.get("bvid", ""),
                        "title": item.get("video_title", ""),
                        "text": item.get("text", ""),
                        "time": item.get("time", ""),
                        "score": round(score, 4),
                    })
        return {"user_id": uid, "profile": profile, "memories": memories, "video_memories": video_memories}

    def record_video_reference(
        self,
        *,
        user_id: str,
        bvid: str,
        username: str = "",
        title: str = "",
        relation: str = "related",
    ) -> None:
        """在用户画像中记录轻量视频关系，不复制视频总结正文。"""
        allowed_relations = {"commented_under", "uploaded_by", "about_user", "related"}
        normalized_relation = relation if relation in allowed_relations else "related"
        self.bot._link_video_to_user_profile(
            str(user_id), username, str(bvid), title, normalized_relation,
        )

    # ══════════════════════════════════════
    #  写入
    # ══════════════════════════════════════

    async def record(
        self,
        text: str,
        *,
        user_id: str = "external",
        username: str = "",
        source: str = "external",
        memory_type: str = "chat",
        level: str = "today",
        importance: int = 5,
        extra: dict | None = None,
    ) -> str:
        """写入一条记忆，返回 rpid。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        rpid = f"ext_{uuid.uuid4().hex}"
        rec = {
            "rpid": rpid,
            "thread_id": f"external:{source}",
            "user_id": str(user_id),
            "username": username,
            "time": now,
            "text": text,
            "source": source,
            "memory_type": memory_type,
            "level": level,
            "importance": importance,
            "promoted_at": now,
        }
        if extra:
            rec.update(extra)
        emb = await self.bot._get_embedding(text)
        if emb:
            rec["embedding"] = emb
        self.bot._save_memory_entry(rec)
        logger.debug(f"[MemoryAPI] 记录: [{level}] {text[:60]}...")
        return rpid

    async def record_live_event(
        self,
        *,
        user_id: str,
        username: str,
        event_type: str,
        content: str = "",
        session_id: str = "",
        event_id: str = "",
        room_id: str = "",
        amount: float | int | None = None,
        extra: dict | None = None,
    ) -> str:
        """写入直播向量记忆，并在用户画像中只记录计数与记忆引用。"""
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("record_live_event requires a Bilibili user_id")

        external_event_id = str(event_id or "").strip()
        if external_event_id:
            for item in reversed(self.bot._memory):
                if (
                    item.get("memory_type") == "live"
                    and str(item.get("external_event_id", "")) == external_event_id
                ):
                    return str(item.get("rpid", ""))

        normalized_type = str(event_type or "interaction").strip().lower()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        details = [f"[{now}] {username or uid} 在直播中触发 {normalized_type}"]
        if content:
            details.append(f"内容：{content}")
        if amount is not None:
            details.append(f"金额：{amount}")
        if room_id:
            details.append(f"直播间：{room_id}")
        text = "；".join(details)
        importance = 8 if normalized_type in {"super_chat", "guard", "buy_guard"} else 6
        record_extra = {
            "session_id": str(session_id or ""),
            "room_id": str(room_id or ""),
            "external_event_id": external_event_id,
            "live_event_type": normalized_type,
        }
        if amount is not None:
            record_extra["amount"] = amount
        if extra:
            record_extra["live_extra"] = deepcopy(extra)
        rpid = await self.record(
            text,
            user_id=uid,
            username=username,
            source="bilibili_live",
            memory_type="live",
            level="today",
            importance=importance,
            extra=record_extra,
        )
        self.bot._update_user_profile(
            uid,
            username=username or None,
            live_event={
                "event_type": normalized_type,
                "time": now,
                "session_id": session_id,
                "memory_ref": rpid,
            },
        )
        return rpid

    # ══════════════════════════════════════
    #  统计 / 工具
    # ══════════════════════════════════════

    def stats(self) -> dict:
        """返回记忆统计信息。"""
        s = {"total": len(self.bot._memory)}
        for key in ("today", "recent", "long_term"):
            s[key] = sum(1 for m in self.bot._memory if m.get("level") == key)
        s["aged"] = sum(1 for m in self.bot._memory if m.get("aged"))
        for key in ("chat", "video", "dynamic", "live", "user_summary"):
            s[f"type_{key}"] = sum(
                1 for m in self.bot._memory
                if self.bot._match_memory_type(m, {key})
            )
        return s

    def count_user_memories(self, user_id: str) -> int:
        """返回指定用户的记忆总条数。"""
        return sum(1 for m in self.bot._memory if m.get("user_id") == str(user_id))
