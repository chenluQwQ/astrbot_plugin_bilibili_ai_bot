"""日终清算引擎 — 记忆分级评估、升级、老化。

流程：
  1. 迁移：无有效 level 的旧记忆（含 level=None/""）
         → chat 类型到 recent(7分)，其他到 long_term(8分)
         chat 类型有 level 但无 importance → recent, importance=7
  2. 日终：LLM 批量评估 today 的 chat 记忆
       非今天产生的 chat 记忆 → 直接晋升 recent, importance=7
       今天产生的：≤ DISCARD_THRESHOLD → 丢弃 / > DISCARD_THRESHOLD → 晋升 recent
     video / dynamic 类型不评估，直接晋升 recent
  3. 时效升级：recent 超 14 天 → long_term
  4. 老化标记：long_term 超 6 月 → aged: true
"""

import json
from datetime import datetime, timedelta
from astrbot.api import logger
from .config import (
    MEMORY_FILE,
    CONSOLIDATION_DISCARD_THRESHOLD,
    CONSOLIDATION_BATCH_SIZE,
    RECENT_PROMOTE_DAYS,
    LONG_TERM_AGE_DAYS,
    CONSOLIDATION_STATE_FILE,
)

# ── LLM 评估 Prompt ──

MEMORY_EVALUATE_PROMPT = """你是记忆管理系统。请对以下记忆逐条评估重要度（1-10分）。

评分标准：
- 9-10：关键个人信息（生日、地址、真名）、重大事件、深层情感、承诺约定
- 6-8：兴趣爱好、态度观点、值得记住的互动细节
- 4-5：普通闲聊中偶尔透露的信息、一般性讨论
- 1-3：纯问候、无信息量的水聊、重复已知信息

规则：
- 每条记忆必须返回结果，不要遗漏
- rpid 必须与原始记忆完全一致（字符串）
- summary 一句话概括核心内容

只输出 JSON 数组，不要任何其他文字：
[{{"rpid": "原始rpid", "importance": 评分数字, "summary": "一句话总结"}}]

待评估记忆：
{memories}"""


class ConsolidationEngine:
    """BiliBot 日终清算引擎（JSON 架构，不依赖 SQLite）。

    由 MemoryMixin 所在的 Bot 实例持有，通过 mixin 方法访问
    self._memory / self._save_json / self._load_json / self._llm_call。
    """

    def __init__(self, bot):
        """bot: 拥有 MemoryMixin + UtilsMixin + LLMMixin 的主实例"""
        self.bot = bot

    # ══════════════════════════════════════
    #  公共入口
    # ══════════════════════════════════════

    async def run_daily(self) -> str:
        """完整日终清算，返回结果摘要字符串。"""
        lines = []
        start = datetime.now()

        # 0. 迁移旧数据
        migrated = self._migrate_legacy_entries()
        if migrated:
            lines.append(f"📦 迁移旧记忆 {migrated} 条（chat→recent/其他→long_term）")

        # 1. 时效升级 recent → long_term
        promoted = self._promote_recent_to_longterm()
        if promoted:
            lines.append(f"⏫ {promoted} 条 recent → long_term")

        # 2. 老化标记
        aged = self._mark_aged()
        if aged:
            lines.append(f"🕰️ {aged} 条标记 aged")

        # 3. LLM 评估 today 的 chat 记忆
        eval_result = await self._evaluate_today_memories()
        lines.append(f"🧠 {eval_result}")

        # 4. video / dynamic 直接晋升
        auto_promoted = self._auto_promote_non_chat()
        if auto_promoted:
            lines.append(f"🎬 {auto_promoted} 条视频/动态 → recent")

        elapsed = (datetime.now() - start).total_seconds()
        lines.append(f"⏱️ 耗时 {elapsed:.1f}s")

        summary = "\n".join(lines)
        logger.info(f"[BiliBot] 日终清算完成:\n{summary}")

        # 记录清算时间
        self.bot._save_json(CONSOLIDATION_STATE_FILE, {
            "last_consolidation": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": summary,
        })
        return summary

    def should_run_today(self) -> bool:
        """检查今天是否已执行过清算。"""
        state = self.bot._load_json(CONSOLIDATION_STATE_FILE, {})
        last = state.get("last_consolidation", "")
        if not last:
            return True
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M")
            return last_dt.date() < datetime.now().date()
        except ValueError:
            return True

    # ══════════════════════════════════════
    #  Step 0: 旧数据迁移
    # ══════════════════════════════════════

    def _migrate_legacy_entries(self) -> int:
        """将无有效 level 的旧记忆标记：
        - chat 类型 → recent, importance=7
        - 非 chat 类型 → long_term, importance=8
        首次运行时，也将所有 today/recent 记忆提升为 long_term, importance=7。
        额外：chat 类型有 level 但无有效 importance → recent, importance=7。"""
        count = 0
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 检查是否已执行过首次全量迁移
        state = self.bot._load_json(CONSOLIDATION_STATE_FILE, {})
        bootstrap_done = state.get("bootstrap_all_longterm", False)

        for m in self.bot._memory:
            norm = self.bot._normalize_memory_entry(m)
            is_chat = norm.get("memory_type") == "chat"
            level = m.get("level")  # 可能是 None / "" / 缺失

            if not level:
                # level 缺失 或 None / ""：全部视为无级别旧记忆
                if is_chat:
                    m["level"] = "recent"
                    m["importance"] = 7
                else:
                    m["level"] = "long_term"
                    m["importance"] = 8
                m["promoted_at"] = m.get("time", now_str)
                count += 1
            elif not bootstrap_done and level in ("today", "recent"):
                m["level"] = "long_term"
                m["importance"] = max(m.get("importance", 5), 7)
                m["promoted_at"] = now_str
                count += 1
            elif is_chat and level and not m.get("importance"):
                # chat 有 level 但无分数 → 强制 recent，固定7分
                m["level"] = "recent"
                m["importance"] = 7
                m["promoted_at"] = now_str
                count += 1

        if count:
            self.bot._save_json(MEMORY_FILE, self.bot._memory)
        if not bootstrap_done:
            state["bootstrap_all_longterm"] = True
            self.bot._save_json(CONSOLIDATION_STATE_FILE, state)
        return count

    # ══════════════════════════════════════
    #  Step 1: recent → long_term
    # ══════════════════════════════════════

    def _promote_recent_to_longterm(self) -> int:
        """超过 RECENT_PROMOTE_DAYS 天的 recent 记忆自动升级为 long_term。"""
        cutoff = datetime.now() - timedelta(days=RECENT_PROMOTE_DAYS)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        count = 0
        for m in self.bot._memory:
            if m.get("level") != "recent":
                continue
            promoted_at = m.get("promoted_at", m.get("time", ""))
            try:
                pt = datetime.strptime(promoted_at[:16], "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                continue
            if pt < cutoff:
                m["level"] = "long_term"
                m["promoted_at"] = now_str
                count += 1
        if count:
            self.bot._save_json(MEMORY_FILE, self.bot._memory)
        return count

    # ══════════════════════════════════════
    #  Step 2: long_term 老化
    # ══════════════════════════════════════

    def _mark_aged(self) -> int:
        """long_term 超 LONG_TERM_AGE_DAYS 天的记忆标 aged: true。"""
        cutoff = datetime.now() - timedelta(days=LONG_TERM_AGE_DAYS)
        count = 0
        for m in self.bot._memory:
            if m.get("level") != "long_term" or m.get("aged"):
                continue
            t = m.get("promoted_at", m.get("time", ""))
            try:
                pt = datetime.strptime(t[:16], "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                continue
            if pt < cutoff:
                m["aged"] = True
                count += 1
        if count:
            self.bot._save_json(MEMORY_FILE, self.bot._memory)
        return count

    # ══════════════════════════════════════
    #  Step 3: LLM 评估 today → recent / discard
    # ══════════════════════════════════════

    async def _evaluate_today_memories(self) -> str:
        """评估 level=today 且 memory_type=chat 的记忆。
        非今天产生的 chat 记忆直接晋升 recent（importance=7），不走 LLM 评估。"""
        all_today_chats = [
            m for m in self.bot._memory
            if m.get("level") == "today"
            and self.bot._match_memory_type(m, {"chat"})
        ]
        if not all_today_chats:
            return "无待评估 today 记忆"

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        today_date = datetime.now().strftime("%Y-%m-%d")

        # ── 先处理非今天的 chat 记忆：直接晋升 recent，固定7分 ──
        today_chats = []
        auto_promoted_old = 0
        for m in all_today_chats:
            mem_time = m.get("time", "")[:10]  # 取 YYYY-MM-DD 部分
            if mem_time != today_date:
                # 不是今天的记忆，跳过 LLM 评估，直接晋升
                m["level"] = "recent"
                m["importance"] = 7
                m["promoted_at"] = now_str
                auto_promoted_old += 1
            else:
                today_chats.append(m)

        if auto_promoted_old:
            self.bot._save_json(MEMORY_FILE, self.bot._memory)
            logger.info(f"[BiliBot] 清算：{auto_promoted_old} 条非今天 chat 记忆直接 → recent (7分)")

        if not today_chats:
            return f"非今天 chat {auto_promoted_old} 条直接晋升 recent | 无今日 chat 待评估"

        total_promoted = 0
        total_discarded = 0
        total_failed = 0

        for i in range(0, len(today_chats), CONSOLIDATION_BATCH_SIZE):
            batch = today_chats[i:i + CONSOLIDATION_BATCH_SIZE]
            try:
                result = await self._evaluate_batch(batch)
                if result is None:
                    total_failed += len(batch)
                    continue

                # 建立 rpid → evaluation 映射
                eval_map = {}
                for ev in result:
                    if not isinstance(ev, dict):
                        total_failed += 1
                        continue
                    rpid = str(ev.get("rpid", ""))
                    if not rpid:
                        total_failed += 1
                        continue
                    eval_map[rpid] = ev

                batch_rpids = {m["rpid"] for m in batch}
                discard_rpids = set()

                for m in batch:
                    rpid = m["rpid"]
                    ev = eval_map.get(rpid)
                    if ev is None:
                        # LLM 漏评的记忆，默认保留，升 recent
                        m["level"] = "recent"
                        m["importance"] = m.get("importance", 5)
                        m["promoted_at"] = now_str
                        total_promoted += 1
                        continue

                    importance = ev.get("importance", 5)
                    try:
                        importance = int(importance)
                    except (TypeError, ValueError):
                        importance = 5
                    importance = max(1, min(10, importance))

                    if importance <= CONSOLIDATION_DISCARD_THRESHOLD:
                        discard_rpids.add(rpid)
                        total_discarded += 1
                    else:
                        m["level"] = "recent"
                        m["importance"] = importance
                        m["promoted_at"] = now_str
                        total_promoted += 1

                # 删除低分记忆
                if discard_rpids:
                    self.bot._memory = [
                        m for m in self.bot._memory
                        if m.get("rpid") not in discard_rpids
                    ]

            except Exception as e:
                logger.error(f"[BiliBot] 清算评估批次失败: {e}")
                total_failed += len(batch)

        self.bot._save_json(MEMORY_FILE, self.bot._memory)
        parts = []
        if auto_promoted_old:
            parts.append(f"📅非今天 {auto_promoted_old} 条直接晋升")
        parts.append(
            f"今日 {len(today_chats)} 条 → "
            f"📌晋升 {total_promoted} | 🗑丢弃 {total_discarded} | ⚠失败 {total_failed}"
        )
        return " | ".join(parts)

    async def _evaluate_batch(self, batch: list[dict]) -> list[dict] | None:
        """对一批记忆调用 LLM 评估，返回评估结果列表。"""
        mem_lines = []
        for m in batch:
            rpid = m.get("rpid", "?")
            uid = m.get("user_id", "?")
            username = m.get("username", "")
            text = m.get("text", "")[:200]
            mem_lines.append(f"- rpid:{rpid} | 用户:{username}({uid}) | 内容:{text}")

        prompt = MEMORY_EVALUATE_PROMPT.format(memories="\n".join(mem_lines))

        try:
            resp = await self.bot._llm_call(prompt, max_tokens=600)
            if not resp:
                return None
            resp = self.bot._repair_llm_json(resp)
            result = json.loads(resp)
            if isinstance(result, list):
                return result
            logger.warning(f"[BiliBot] 清算 LLM 返回非数组: {resp[:200]}")
            return None
        except json.JSONDecodeError:
            logger.warning(f"[BiliBot] 清算 JSON 解析失败: {resp[:200] if resp else 'empty'}")
            return None

    # ══════════════════════════════════════
    #  Step 4: video / dynamic 直接晋升
    # ══════════════════════════════════════

    def _auto_promote_non_chat(self) -> int:
        """today 的非 chat 记忆直接晋升 recent，不经 LLM 评估。"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        count = 0
        for m in self.bot._memory:
            if m.get("level") != "today":
                continue
            mtype = self.bot._normalize_memory_entry(m).get("memory_type", "chat")
            if mtype in ("video", "dynamic", "user_summary"):
                m["level"] = "recent"
                m["importance"] = max(m.get("importance", 6), 6)
                m["promoted_at"] = now_str
                count += 1
        if count:
            self.bot._save_json(MEMORY_FILE, self.bot._memory)
        return count

    # ══════════════════════════════════════
    #  用户命令：清理 aged 记忆
    # ══════════════════════════════════════

    def cleanup_aged(self) -> int:
        """删除 aged=true 的 chat 记忆，非 chat 类型只保留 aged 标记不删除。"""
        before = len(self.bot._memory)
        self.bot._memory = [
            m for m in self.bot._memory
            if not m.get("aged")
            or self.bot._normalize_memory_entry(m).get("memory_type", "chat") != "chat"
        ]
        after = len(self.bot._memory)
        removed = before - after
        if removed:
            self.bot._save_json(MEMORY_FILE, self.bot._memory)
        return removed

    def get_stats(self) -> dict:
        """返回各级别的记忆统计。"""
        stats = {"today": 0, "recent": 0, "long_term": 0, "aged": 0, "no_level": 0}
        for m in self.bot._memory:
            level = m.get("level")
            if not level:
                stats["no_level"] += 1
            elif level in stats:
                stats[level] += 1
            if m.get("aged"):
                stats["aged"] += 1
        return stats
