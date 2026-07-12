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

from datetime import datetime
from astrbot.api import logger
from .config import MEMORY_FILE


class BiliBotMemoryAPI:
    """暴露给外部插件的记忆读写接口。

    持有 bot 主实例引用，通过 mixin 方法操作底层数据。
    所有方法都是安全的、不会破坏内部状态。
    """

    def __init__(self, bot):
        self.bot = bot

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
        return self.bot._get_user_profile(str(user_id))

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
        rpid = f"ext_{int(datetime.now().timestamp() * 1000)}"
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

    # ══════════════════════════════════════
    #  统计 / 工具
    # ══════════════════════════════════════

    def stats(self) -> dict:
        """返回记忆统计信息。"""
        s = {"total": len(self.bot._memory)}
        for key in ("today", "recent", "long_term"):
            s[key] = sum(1 for m in self.bot._memory if m.get("level") == key)
        s["aged"] = sum(1 for m in self.bot._memory if m.get("aged"))
        for key in ("chat", "video", "dynamic", "user_summary"):
            s[f"type_{key}"] = sum(
                1 for m in self.bot._memory
                if self.bot._match_memory_type(m, {key})
            )
        return s

    def count_user_memories(self, user_id: str) -> int:
        """返回指定用户的记忆总条数。"""
        return sum(1 for m in self.bot._memory if m.get("user_id") == str(user_id))
