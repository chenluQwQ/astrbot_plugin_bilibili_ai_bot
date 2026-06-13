"""周总结：回顾一周的B站生活，生成总结并通过 QQ 私信 / B站动态发送。

数据源（近7天）：
  - watch_log.json          主动看过的视频（评分/心情/感想）
  - bangumi_watch_log.json  看过的番剧
  - dynamic_log.json        发过的动态
  - proactive_log.json      主动评论
  - memory.json             chat 类型记忆（互动用户统计）

触发：
  - 自动：每周 WEEKLY_SUMMARY_DAY（0=周一...6=周日）的睡眠时段，
          日终清算之后由主循环调用 _maybe_weekly_summary()
  - 手动：/bili周总结 命令
"""
import json
from datetime import datetime, timedelta
from collections import Counter
from astrbot.api import logger
from .config import (
    WATCH_LOG_FILE, BANGUMI_WATCH_LOG_FILE, DYNAMIC_LOG_FILE,
    PROACTIVE_LOG_FILE, WEEKLY_SUMMARY_FILE,
)


class WeeklySummaryMixin:
    """周总结生成与投递。"""

    # ── 数据收集 ──

    def _collect_weekly_data(self, days=7):
        """收集近 N 天的活动数据，返回结构化 dict。"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")

        def _recent(entries):
            return [e for e in entries if isinstance(e, dict) and e.get("time", "") >= cutoff]

        videos = _recent(self._load_json(WATCH_LOG_FILE, []))
        bangumi = _recent(self._load_json(BANGUMI_WATCH_LOG_FILE, []))
        dynamics = _recent(self._load_json(DYNAMIC_LOG_FILE, []))
        proactive_comments = _recent(self._load_json(PROACTIVE_LOG_FILE, []))

        # 聊天互动：chat 类型记忆，统计互动条数和活跃用户
        chats = [
            m for m in getattr(self, "_memory", [])
            if isinstance(m, dict)
            and m.get("memory_type") == "chat"
            and m.get("time", "") >= cutoff
            and m.get("user_id") not in (None, "", "self")
        ]
        user_counter = Counter(m.get("user_id", "") for m in chats)

        return {
            "videos": videos,
            "bangumi": bangumi,
            "dynamics": dynamics,
            "proactive_comments": proactive_comments,
            "chat_count": len(chats),
            "active_users": user_counter.most_common(5),
        }

    def _format_weekly_data(self, data):
        """把收集到的数据格式化为给 LLM 的文本。"""
        lines = []

        videos = data["videos"]
        if videos:
            lines.append(f"【看过的视频】共 {len(videos)} 个：")
            # 高分和低分的更值得提
            shown = sorted(videos, key=lambda v: v.get("score", 0), reverse=True)[:10]
            for v in shown:
                lines.append(
                    f"- 《{v.get('title', '')[:30]}》(UP:{v.get('up_name', '')}) "
                    f"评分{v.get('score', '?')}/10 心情:{v.get('mood', '')} "
                    f"感想:{(v.get('review', '') or '')[:40]}"
                )
        else:
            lines.append("【看过的视频】这周没看视频")

        bangumi = data["bangumi"]
        if bangumi:
            seasons = Counter(b.get("title", "") for b in bangumi)
            lines.append(f"【追的番】共 {len(bangumi)} 集：")
            for title, cnt in seasons.most_common(5):
                eps = [b for b in bangumi if b.get("title") == title]
                avg = sum(b.get("score", 0) for b in eps) / max(len(eps), 1)
                lines.append(f"- 《{title[:25]}》看了{cnt}集，平均评分{avg:.0f}/10")

        dynamics = data["dynamics"]
        if dynamics:
            lines.append(f"【发过的动态】共 {len(dynamics)} 条：")
            for d in dynamics[-5:]:
                lines.append(f"- {(d.get('text', '') or '')[:40]}")

        pc = data["proactive_comments"]
        if pc:
            lines.append(f"【主动发的评论】共 {len(pc)} 条")

        if data["chat_count"]:
            lines.append(f"【评论区互动】共 {data['chat_count']} 次对话")
            if data["active_users"]:
                top = "、".join(f"{uid}({cnt}次)" for uid, cnt in data["active_users"][:3])
                lines.append(f"互动最多的用户UID：{top}")
        else:
            lines.append("【评论区互动】这周没什么人来聊天")

        return "\n".join(lines)

    # ── 生成 ──

    async def _generate_weekly_summary(self):
        """生成周总结文本，失败返回 None。"""
        data = self._collect_weekly_data()
        if not (data["videos"] or data["bangumi"] or data["dynamics"] or data["chat_count"]):
            logger.info("[BiliBot] 📅 这周没有任何活动记录，跳过周总结")
            return None

        data_text = self._format_weekly_data(data)
        week_start = (datetime.now() - timedelta(days=7)).strftime("%m.%d")
        week_end = datetime.now().strftime("%m.%d")

        # 预算统计数据给模板用
        v_count = len(data["videos"])
        v_top = max((v.get("score", 0) for v in data["videos"]), default=0) if data["videos"] else 0
        b_count = len(data["bangumi"])
        d_count = len(data["dynamics"])
        chat_count = data["chat_count"]

        stats_line = f"视频{v_count}个"
        if b_count:
            stats_line += f" · 番剧{b_count}集"
        if d_count:
            stats_line += f" · 动态{d_count}条"
        if chat_count:
            stats_line += f" · 互动{chat_count}次"

        prompt = f"""现在是周末，你想回顾这一周在B站的生活，写一份有格式感的周报。

这周的活动记录：
{data_text}

请严格按照以下格式输出（每个板块的内容用你自己的语气写，有感受有情绪，不是流水账）：

📅 周报 | {week_start} ~ {week_end}
━━━━━━━━━━━━
{stats_line}

📺 视频
（挑2-3个印象最深的视频聊，说说看完什么感觉、哪里戳到你了，不用每个都提）

{"🎬 追番" + chr(10) + "（追了什么番、追到第几集、感受如何）" + chr(10) + chr(10) if b_count else ""}{"💬 评论区" + chr(10) + "（和谁聊得多、有没有印象深的互动、评论区氛围怎么样）" + chr(10) + chr(10) if chat_count else ""}{"📢 动态" + chr(10) + "（发了什么动态、当时在想什么）" + chr(10) + chr(10) if d_count else ""}✍️ 碎碎念
（用1-2句话总结这周的心情/状态，随意收尾）

要求：
- 每个板块标题行保持原样（📺 视频、🎬 追番 等），内容紧跟其后
- 内容用你自己的语气，像跟亲近的人聊天
- 总字数200-350字（不含格式符号）
- 直接输出，不要加额外的标题或前缀"""

        custom_inst = self.config.get("CUSTOM_WEEKLY_INSTRUCTION", "")
        if custom_inst:
            prompt += f"\n【补充提示词】{custom_inst}"

        summary = await self._llm_call(
            prompt, system_prompt=self._get_system_prompt(), max_tokens=800
        )
        return (summary or "").strip() or None

    # ── 投递 ──

    async def _deliver_weekly_summary(self, summary):
        """按配置投递周总结，返回投递结果描述列表。"""
        mode = str(self.config.get("WEEKLY_SUMMARY_MODE", "qq")).lower().strip()
        results = []

        if mode in ("qq", "both"):
            umo = (self.config.get("WEEKLY_SUMMARY_QQ_UMO", "") or "").strip()
            if not umo:
                umo = (self.config.get("ABUSE_ALERT_QQ_UMO", "") or "").strip()
            if umo:
                try:
                    from astrbot.api.event import MessageChain
                    chain = MessageChain().message(summary)
                    await self.context.send_message(umo, chain)
                    results.append("QQ私信")
                    logger.info("[BiliBot] 📅 周总结已通过QQ私信发送")
                except Exception as e:
                    logger.warning(f"[BiliBot] 周总结QQ发送失败: {e}")
            else:
                logger.warning("[BiliBot] 周总结模式包含qq但未配置UMO（周总结/恶意告警的UMO都为空）")

        if mode in ("dynamic", "both"):
            try:
                if await self._post_dynamic_text(summary):
                    results.append("B站动态")
                    log = self._load_json(DYNAMIC_LOG_FILE, [])
                    log.append({
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "text": summary, "has_image": False, "weekly": True,
                    })
                    self._save_json(DYNAMIC_LOG_FILE, log[-100:])
                    logger.info("[BiliBot] 📅 周总结已发布为B站动态")
            except Exception as e:
                logger.warning(f"[BiliBot] 周总结动态发布失败: {e}")

        return results

    # ── 调度 ──

    def _weekly_summary_done_this_week(self):
        """检查本ISO周是否已生成过周总结。"""
        records = self._load_json(WEEKLY_SUMMARY_FILE, [])
        if not records:
            return False
        this_week = datetime.now().strftime("%G-W%V")
        return any(r.get("week") == this_week for r in records if isinstance(r, dict))

    def _save_weekly_summary_record(self, summary, delivered):
        records = self._load_json(WEEKLY_SUMMARY_FILE, [])
        records.append({
            "week": datetime.now().strftime("%G-W%V"),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": summary,
            "delivered": delivered,
        })
        self._save_json(WEEKLY_SUMMARY_FILE, records[-20:])

    async def _maybe_weekly_summary(self):
        """主循环睡眠时段调用：到了周总结日且本周未生成则执行。"""
        if not self.config.get("ENABLE_WEEKLY_SUMMARY", False):
            return
        try:
            target_day = int(self.config.get("WEEKLY_SUMMARY_DAY", 6))
        except (ValueError, TypeError):
            target_day = 6
        if datetime.now().weekday() != target_day % 7:
            return
        if self._weekly_summary_done_this_week():
            return
        await self.run_weekly_summary()

    async def run_weekly_summary(self):
        """生成并投递周总结（自动调度和手动命令共用）。返回 (summary, delivered)。"""
        logger.info("[BiliBot] 📅 开始生成周总结...")
        try:
            summary = await self._generate_weekly_summary()
        except Exception as e:
            logger.error(f"[BiliBot] 周总结生成异常: {e}")
            return None, []
        if not summary:
            # 没有活动也记录一下，避免同一周反复尝试
            self._save_weekly_summary_record("（本周无活动，未生成）", [])
            return None, []
        delivered = await self._deliver_weekly_summary(summary)
        self._save_weekly_summary_record(summary, delivered)
        logger.info(f"[BiliBot] 📅 周总结完成，投递：{delivered or ['仅存档']}")
        return summary, delivered
