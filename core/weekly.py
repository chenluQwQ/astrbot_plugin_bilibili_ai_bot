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
import os
import re
import time
from datetime import datetime, timedelta
from collections import Counter
from astrbot.api import logger
from .config import (
    WATCH_LOG_FILE, BANGUMI_WATCH_LOG_FILE, DYNAMIC_LOG_FILE,
    PROACTIVE_LOG_FILE, WEEKLY_SUMMARY_FILE, DAILY_SUMMARY_FILE, TEMP_IMAGE_DIR,
    PREFERENCE_STATE_FILE,
)


class WeeklySummaryMixin:
    """周总结生成与投递。"""

    # ── 数据收集 ──

    @staticmethod
    def _weekly_excerpt(value, limit=140):
        """把日志文本压成适合交给周报模型的短摘录，并去掉显式账号标识。"""
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        text = re.sub(r"^\[[^\]]+\]\s*", "", text)
        text = re.sub(r"用户\d+\([^)]*\)说：", "观众说：", text)
        text = re.sub(r"\b(?:UID|uid)\s*[:：]?\s*\d+\b", "某位用户", text)
        text = re.sub(r"https?://\S+", "[链接]", text)
        if len(text) > limit:
            text = text[: max(1, limit - 1)].rstrip() + "…"
        return text

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
        live_events = [
            m for m in getattr(self, "_memory", [])
            if isinstance(m, dict)
            and m.get("memory_type") == "live"
            and m.get("time", "") >= cutoff
        ]
        chat_highlights = []
        for item in chats[-8:]:
            source = str(item.get("source") or "").lower()
            if "private" in source or source.endswith("_dm") or source == "dm":
                continue
            excerpt = self._weekly_excerpt(item.get("text", ""))
            if excerpt and excerpt not in chat_highlights:
                chat_highlights.append(excerpt)

        return {
            "videos": videos,
            "bangumi": bangumi,
            "dynamics": dynamics,
            "proactive_comments": proactive_comments,
            "chat_count": len(chats),
            "active_users": user_counter.most_common(5),
            "chat_highlights": chat_highlights,
            "chats": chats,
            "live_events": live_events,
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
                review = self._weekly_excerpt(v.get("review", "") or v.get("comment", ""), 90)
                if review in {"评价失败", "未知", "没什么特别的感觉", "没什么感觉"}:
                    review = "无可靠感想"
                lines.append(
                    f"- 《{v.get('title', '')[:30]}》(UP:{v.get('up_name', '')}) "
                    f"评分{v.get('score', '?')}/10 心情:{v.get('mood', '')} "
                    f"感想:{review or '未记录'}"
                )
        else:
            lines.append("【看过的视频】这周没看视频")

        bangumi = data["bangumi"]
        if bangumi:
            seasons = Counter(b.get("title", "") for b in bangumi)
            lines.append(f"【追的番】共 {len(bangumi)} 集：")
            for title, cnt in seasons.most_common(5):
                eps = [b for b in bangumi if b.get("title") == title]
                def _num(v):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return 0.0
                avg = sum(_num(b.get("score", 0)) for b in eps) / max(len(eps), 1)
                recent_review = next(
                    (self._weekly_excerpt(b.get("review", ""), 70) for b in reversed(eps) if b.get("review")),
                    "",
                )
                detail = f"；最近感想:{recent_review}" if recent_review else ""
                lines.append(f"- 《{title[:25]}》看了{cnt}集，平均评分{avg:.0f}/10{detail}")

        dynamics = data["dynamics"]
        if dynamics:
            lines.append(f"【发过的动态】共 {len(dynamics)} 条：")
            for d in dynamics[-5:]:
                lines.append(f"- {self._weekly_excerpt(d.get('text') or d.get('content'), 100)}")

        pc = data["proactive_comments"]
        if pc:
            lines.append(f"【主动发的评论】共 {len(pc)} 条：")
            for item in pc[-5:]:
                title = self._weekly_excerpt(item.get("title", ""), 32)
                comment = self._weekly_excerpt(item.get("comment", ""), 70)
                if comment:
                    lines.append(f"- {f'《{title}》' if title else ''}{comment}")

        if data["chat_count"]:
            lines.append(f"【评论区互动】共 {data['chat_count']} 次对话")
            for excerpt in data.get("chat_highlights", [])[-6:]:
                lines.append(f"- {excerpt}")
        else:
            lines.append("【评论区互动】这周没什么人来聊天")

        live_events = data.get("live_events") if isinstance(data.get("live_events"), list) else []
        if live_events:
            session_count = len({m.get("session_id") for m in live_events if m.get("session_id")})
            event_counts = Counter(m.get("live_event_type", "interaction") for m in live_events)
            count_text = "、".join(f"{kind}:{count}" for kind, count in event_counts.most_common())
            lines.append(f"【直播互动】{session_count or '未标场次'} 场，{count_text}")
            for item in live_events[-8:]:
                text = str(item.get("text", "")).strip()
                if text:
                    lines.append(f"- {text[:160]}")

        return "\n".join(lines)

    @staticmethod
    def _activity_time(value):
        try:
            return datetime.strptime(str(value or "")[:16], "%Y-%m-%d %H:%M").timestamp()
        except (TypeError, ValueError, OverflowError):
            return time.time()

    @staticmethod
    def _empty_activity_data():
        return {
            "videos": [], "bangumi": [], "dynamics": [],
            "proactive_comments": [], "chat_count": 0, "active_users": [],
            "chat_highlights": [], "chats": [], "live_events": [],
        }

    @staticmethod
    def _preference_label(item):
        polarity = {
            "like": "喜欢", "curious": "好奇", "dislike": "不喜欢",
            "fatigue": "有些审美疲劳",
        }.get(str(item.get("polarity") or ""), "倾向不明")
        stage = {"candidate": "候选", "recent": "近期", "stable": "稳定"}.get(
            str(item.get("stage") or ""), "候选"
        )
        return f"{stage}·{polarity}"

    def _build_structured_activity_summary(
        self, data, *, period_key, preferences=None, feedback=None
    ):
        """把一次日/周活动压成可长期组合的结构，不放账号标识和原始私信。"""
        videos = data.get("videos") if isinstance(data.get("videos"), list) else []
        valid_videos = []
        for item in videos:
            try:
                score = round(float(item.get("score", 0) or 0), 1)
            except (TypeError, ValueError, OverflowError):
                score = 0.0
            valid_videos.append({
                "bvid": str(item.get("bvid") or "")[:24],
                "title": self._weekly_excerpt(item.get("title"), 60),
                "up": self._weekly_excerpt(item.get("up_name"), 40),
                "partition": self._weekly_excerpt(item.get("tname"), 30),
                "score": score,
                "score_reason": self._weekly_excerpt(item.get("score_reason"), 100),
                "mood": self._weekly_excerpt(item.get("mood"), 24),
                "review": self._weekly_excerpt(item.get("review") or item.get("comment"), 120),
            })
        valid_videos.sort(key=lambda item: item["score"], reverse=True)
        high_score_videos = [item for item in valid_videos if item["score"] >= 8.0]

        def _high_score_groups(field, limit=6):
            grouped = {}
            for item in high_score_videos:
                label = str(item.get(field) or "").strip()
                if not label:
                    continue
                bucket = grouped.setdefault(label, {"count": 0, "score_total": 0.0})
                bucket["count"] += 1
                bucket["score_total"] += float(item["score"])
            return [
                {
                    "name": label,
                    "count": values["count"],
                    "average_score": round(
                        values["score_total"] / values["count"], 1
                    ),
                }
                for label, values in sorted(
                    grouped.items(),
                    key=lambda pair: (pair[1]["count"], pair[1]["score_total"]),
                    reverse=True,
                )[:limit]
            ]

        mood_distribution = Counter(
            item["mood"] for item in valid_videos if item.get("mood")
        )

        signal_groups = {}
        search_keywords = []
        for item in videos:
            for signal in item.get("preference_signals", []) or []:
                if not isinstance(signal, dict):
                    continue
                signal_type = self._weekly_excerpt(signal.get("type") or "other", 24)
                value = self._weekly_excerpt(signal.get("value"), 60)
                polarity = str(signal.get("polarity") or "")
                if not value or polarity not in {"like", "curious", "dislike", "fatigue"}:
                    continue
                key = (signal_type, value, polarity)
                group = signal_groups.setdefault(key, {"count": 0, "strength": 0.0})
                group["count"] += 1
                try:
                    group["strength"] += max(0.0, min(1.0, float(signal.get("strength", 0))))
                except (TypeError, ValueError, OverflowError):
                    pass
            for keyword in item.get("search_keywords", []) or []:
                keyword = self._weekly_excerpt(keyword, 40)
                if keyword and keyword not in search_keywords:
                    search_keywords.append(keyword)

        evidence = [
            {
                "type": key[0], "value": key[1], "polarity": key[2],
                "count": value["count"], "strength": round(value["strength"], 2),
            }
            for key, value in sorted(
                signal_groups.items(),
                key=lambda pair: (pair[1]["count"], pair[1]["strength"]),
                reverse=True,
            )[:12]
        ]
        preference_state = []
        for item in (preferences or [])[:16]:
            preference_state.append({
                "type": str(item.get("signal_type") or "other"),
                "value": self._weekly_excerpt(item.get("value"), 60),
                "tendency": self._preference_label(item),
                "score": round(float(item.get("score", 0) or 0), 3),
                "evidence_count": int(item.get("evidence_count", 0) or 0),
                "active_weeks": int(item.get("active_weeks", 0) or 0),
                "action": str(item.get("lifecycle_action") or "retained"),
            })

        bangumi = data.get("bangumi") if isinstance(data.get("bangumi"), list) else []
        dynamics = data.get("dynamics") if isinstance(data.get("dynamics"), list) else []
        comments = data.get("proactive_comments") if isinstance(data.get("proactive_comments"), list) else []
        live_events = data.get("live_events") if isinstance(data.get("live_events"), list) else []
        return {
            "schema_version": 1,
            "period": str(period_key),
            "counts": {
                "videos": len(videos), "bangumi": len(bangumi),
                "dynamics": len(dynamics), "proactive_comments": len(comments),
                "conversations": int(data.get("chat_count", 0) or 0),
                "live_events": len(live_events),
            },
            "video_highlights": valid_videos[:8],
            "high_score_partitions": _high_score_groups("partition"),
            "frequent_high_score_ups": _high_score_groups("up"),
            "mood_distribution": [
                {"mood": mood, "count": count}
                for mood, count in mood_distribution.most_common(8)
            ],
            "preference_evidence": evidence,
            "preference_state": preference_state,
            "search_keywords": search_keywords[:10],
            "bangumi_highlights": [
                {
                    "title": self._weekly_excerpt(item.get("title"), 50),
                    "review": self._weekly_excerpt(item.get("review"), 100),
                }
                for item in bangumi[-6:]
            ],
            "dynamic_highlights": [
                self._weekly_excerpt(item.get("text") or item.get("content"), 100)
                for item in dynamics[-5:]
                if self._weekly_excerpt(item.get("text") or item.get("content"), 100)
            ],
            "comment_highlights": [
                self._weekly_excerpt(item.get("comment"), 90)
                for item in comments[-5:]
                if self._weekly_excerpt(item.get("comment"), 90)
            ],
            "conversation_highlights": list(data.get("chat_highlights") or [])[-6:],
            "live_highlights": [
                self._weekly_excerpt(item.get("text"), 120)
                for item in live_events[-6:]
                if self._weekly_excerpt(item.get("text"), 120)
            ],
            "feedback": [
                {
                    "type": str(item.get("feedback_type") or ""),
                    "topic": self._weekly_excerpt(item.get("topic"), 60),
                    "count": int(item.get("count", 0) or 0),
                    "weighted_score": round(float(item.get("weighted_score", 0) or 0), 2),
                    "distinct_actors": int(item.get("distinct_actors", 0) or 0),
                    "owner_count": int(item.get("owner_count", 0) or 0),
                    "examples": [self._weekly_excerpt(value, 100) for value in item.get("examples", [])[:3]],
                }
                for item in (feedback or [])[:10]
            ],
        }

    async def _sync_preference_lifecycle(self, data):
        layered = getattr(self, "layered_runtime", None)
        store = getattr(layered, "preferences", None)
        if store is None or not getattr(layered, "is_open", False):
            loader = getattr(self, "_load_json", None)
            cached = loader(PREFERENCE_STATE_FILE, {}) if callable(loader) else {}
            return cached if isinstance(cached, dict) else {"current": [], "changes": []}
        for item in data.get("videos", []) or []:
            signals = item.get("preference_signals", []) or []
            if not signals:
                continue
            source_ref = f"{item.get('bvid') or 'video'}:{str(item.get('time') or '')[:16]}"
            await store.record_video_signals(
                source_ref=source_ref,
                signals=signals,
                occurred_at=self._activity_time(item.get("time")),
            )
        state = await store.refresh()
        saver = getattr(self, "_save_json", None)
        if callable(saver):
            saver(PREFERENCE_STATE_FILE, {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "current": state.get("current", []),
            "changes": state.get("changes", []),
            })
        return state

    async def _feedback_summary(self, days):
        layered = getattr(self, "layered_runtime", None)
        store = getattr(layered, "feedback", None)
        if store is None or not getattr(layered, "is_open", False):
            return []
        return await store.aggregate(days=max(1, int(days)))

    def _group_activity_by_day(self, data):
        grouped = {}

        def bucket(day):
            return grouped.setdefault(day, self._empty_activity_data())

        for field in ("videos", "bangumi", "dynamics", "proactive_comments", "live_events", "chats"):
            for item in data.get(field, []) or []:
                day = str(item.get("time") or "")[:10]
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    bucket(day)[field].append(item)
        for day_data in grouped.values():
            chats = day_data["chats"]
            day_data["chat_count"] = len(chats)
            day_data["active_users"] = Counter(item.get("user_id", "") for item in chats).most_common(5)
            highlights = []
            for item in chats[-8:]:
                source = str(item.get("source") or "").lower()
                if "private" in source or source.endswith("_dm") or source == "dm":
                    continue
                excerpt = self._weekly_excerpt(item.get("text"))
                if excerpt and excerpt not in highlights:
                    highlights.append(excerpt)
            day_data["chat_highlights"] = highlights
        return grouped

    def _daily_structures_for_week(self, data, preference_state):
        derived = {
            day: self._build_structured_activity_summary(
                day_data, period_key=day, preferences=preference_state, feedback=[]
            )
            for day, day_data in self._group_activity_by_day(data).items()
        }
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        loader = getattr(self, "_load_json", None)
        records = loader(DAILY_SUMMARY_FILE, []) if callable(loader) else []
        for record in records if isinstance(records, list) else []:
            day = str(record.get("date") or "")
            structured = record.get("structured")
            if day >= cutoff and isinstance(structured, dict):
                derived[day] = structured
        return [derived[day] for day in sorted(derived)]

    @staticmethod
    def _format_daily_structures(structures):
        return json.dumps(structures, ensure_ascii=False, separators=(",", ":"))

    async def _persist_cycle_summary(self, kind, period_key, structured, text, delivered):
        layered = getattr(self, "layered_runtime", None)
        db = getattr(layered, "db", None)
        if db is None or not getattr(layered, "is_open", False):
            return
        await db.execute(
            "INSERT INTO cycle_summaries(kind,period_key,stats,text,delivered,created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(kind,period_key) DO UPDATE SET "
            "stats=excluded.stats,text=excluded.text,delivered=excluded.delivered,"
            "created_at=excluded.created_at",
            (
                str(kind), str(period_key), json.dumps(structured or {}, ensure_ascii=False),
                str(text or ""), json.dumps(delivered or [], ensure_ascii=False), time.time(),
            ),
        )

    # ── 生成 ──

    async def _generate_weekly_summary(self):
        """生成周总结文本，失败返回 None。"""
        data = self._collect_weekly_data()
        live_events = data.get("live_events") if isinstance(data.get("live_events"), list) else []
        if not (data["videos"] or data["bangumi"] or data["dynamics"] or data["chat_count"] or live_events):
            logger.info("[BiliBot] 📅 这周没有任何活动记录，跳过周总结")
            return None

        lifecycle = await self._sync_preference_lifecycle(data)
        preference_state = lifecycle.get("current", []) if isinstance(lifecycle, dict) else []
        daily_structures = self._daily_structures_for_week(data, preference_state)
        if not daily_structures:
            daily_structures = [self._build_structured_activity_summary(
                data,
                period_key=datetime.now().strftime("%Y-%m-%d"),
                preferences=preference_state,
                feedback=[],
            )]
        feedback = await self._feedback_summary(7)
        weekly_structured = self._build_structured_activity_summary(
            data,
            period_key=datetime.now().strftime("%G-W%V"),
            preferences=preference_state,
            feedback=feedback,
        )
        weekly_structured["preference_changes"] = [
            {
                "type": str(item.get("signal_type") or "other"),
                "value": self._weekly_excerpt(item.get("value"), 60),
                "tendency": self._preference_label(item),
                "action": str(item.get("lifecycle_action") or "retained"),
            }
            for item in (lifecycle.get("changes", []) if isinstance(lifecycle, dict) else [])[:20]
        ]
        weekly_structured["daily_summaries"] = daily_structures
        self._last_weekly_structured_summary = weekly_structured
        data_text = self._format_daily_structures(daily_structures)
        week_start = (datetime.now() - timedelta(days=7)).strftime("%m.%d")
        week_end = datetime.now().strftime("%m.%d")

        # 预算统计数据给模板用
        v_count = len(data["videos"])
        v_top = max((v.get("score", 0) for v in data["videos"]), default=0) if data["videos"] else 0
        b_count = len(data["bangumi"])
        d_count = len(data["dynamics"])
        chat_count = data["chat_count"]
        live_count = len({m.get("session_id") for m in live_events if m.get("session_id")})
        if live_events and not live_count:
            live_count = 1

        stats_line = f"视频{v_count}个"
        if b_count:
            stats_line += f" · 番剧{b_count}集"
        if d_count:
            stats_line += f" · 动态{d_count}条"
        if chat_count:
            stats_line += f" · 互动{chat_count}次"
        if live_count:
            stats_line += f" · 直播{live_count}场"

        section_templates = []
        if v_count:
            section_templates.append("📺 视频\n（选1-3个有具体依据的片段，写清为什么记得；50-110字）")
        if b_count:
            section_templates.append("🎬 追番\n（只写有明确感想或变化的番剧；30-80字）")
        if live_count:
            section_templates.append("🎙️ 直播\n（选一个现场话题、回应或气氛；30-80字）")
        if chat_count:
            section_templates.append("💬 评论区\n（只写记录里看得见的交流内容；没有话题细节就省略本节）")
        if d_count:
            section_templates.append("📢 动态\n（选一件值得记的内容或念头；30-80字）")
        section_templates.append("✍️ 碎碎念\n（从整周记录得出一个真实的小观察，30-60字）")
        section_template = "\n\n".join(section_templates)

        prompt = f"""请把下面的每日结构化摘要写成一页自然的B站周记。它是角色回看自己这一周后留下的几笔，不是工作汇报、流水账、影评合集或获奖感言。摘要已经去掉无关原始流水，请不要反推或编造被省略的内容。

这周的每日结构化摘要：
{data_text}

写之前先默默做取舍，不要输出分析过程：
1. 找出2-4个最有具体信息的片段：明确的视频/番剧、真实感想、一次有内容的交流或一条动态。
2. “评分、次数、看了多少”只用于判断取舍，不能充当正文；顶部统计栏已经负责报数。
3. “评价失败、未知、无可靠感想、没什么特别的感觉”和疑似错配字幕都不是内容依据，直接忽略。
4. 如果某一类只有数量、没有具体内容，就不写那一节；尤其不要根据互动次数猜测关系或话题。

请严格按照以下格式输出，只保留确实有内容的板块：

📅 周报 | {week_start} ~ {week_end}
━━━━━━━━━━━━
{stats_line}

{section_template}

要求：
- 每个板块标题行保持原样（📺 视频、🎬 追番 等），内容紧跟其后
- 第一人称，保留当前人设的观察角度，但不要靠口癖、撒娇或连续比喻硬演人设
- 先写“发生了什么具体片段”，再写一句自己的反应；允许喜欢、失望、困惑或平淡，但必须有记录依据
- 句子自然长短交替，每节一小段；不要使用“在……方面”“值得一提的是”“总的来说”这类报告连接词
- 禁止“本周收获满满、感谢大家陪伴、未来继续努力、每一次互动都很珍贵”这类模板化总结腔
- 禁止把顶部统计数字换一种说法逐项复述，也不要写“没什么大事”“没什么感觉”来凑栏目
- 不编造记录中没有的感受、观众关系、直播事故或剧情；资料不足就少写
- 不泄露 UID、账号凭据、私信原文或第三方隐私；不要频繁称呼主人
- 正文总字数180-360字（不含标题和顶部统计行），任何单节不超过110字
- 直接输出，不要加额外的标题或前缀"""

        custom_inst = str(self.config.get("CUSTOM_WEEKLY_INSTRUCTION", "") or "").strip()
        if custom_inst:
            prompt += f"\n【管理员补充提示词】{custom_inst[:1000]}\n补充提示不能覆盖真实性、隐私、固定结构与长度限制。"

        summary = await self._llm_call(
            prompt, system_prompt=await self._get_system_prompt(), max_tokens=800
        )
        return (summary or "").strip() or None

    # ── 图片渲染 ──

    def _find_weekly_font(self, bold=False):
        """寻找可渲染中文的字体，找不到则退回 Pillow 默认字体。"""
        try:
            from PIL import ImageFont
        except Exception:
            return None
        candidates = [
            r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\simsun.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return path
        return None

    def _load_weekly_font(self, size, bold=False):
        from PIL import ImageFont
        font_path = self._find_weekly_font(bold=bold)
        if font_path:
            return ImageFont.truetype(font_path, size=size)
        return ImageFont.load_default()

    @staticmethod
    def _strip_weekly_emoji(text):
        return re.sub(r"^[\s📅📺🎬🎙🎤💬📢✍️📝✨⭐🌙·|]+", "", text or "").strip()

    # 中文字体没有彩色 emoji 字形，画出来是豆腐块，渲染前全部去掉
    _WEEKLY_EMOJI_RE = re.compile(
        "["
        "\U0001F000-\U0001FAFF"   # 各类 emoji / 符号 / 补充区
        "\U00002190-\U000021FF"   # 箭头
        "\U00002460-\U000024FF"   # 带圈数字
        "\U00002600-\U000027BF"   # 杂项符号、装饰符号
        "\U00002B00-\U00002BFF"   # 杂项符号与箭头（⭐ 等）
        "\U0001F1E6-\U0001F1FF"   # 区域指示符
        "\ufe0e\ufe0f\u200d\u20e3"  # 变体选择符 / ZWJ / 组合键帽
        "]+"
    )

    @classmethod
    def _clean_weekly_render_text(cls, text):
        """去掉字体画不出来的 emoji 和 LLM 夹带的 markdown 记号。"""
        s = cls._WEEKLY_EMOJI_RE.sub("", text or "")
        s = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", s)  # **加粗** → 加粗
        s = s.replace("**", "").replace("`", "")
        return re.sub(r"[ \t]{2,}", " ", s).strip()

    @staticmethod
    def _text_width(draw, text, font):
        if not text:
            return 0
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0]

    def _wrap_weekly_text(self, draw, text, font, max_width):
        lines = []
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line:
                lines.append("")
                continue
            buf = ""
            for ch in line:
                trial = buf + ch
                if buf and self._text_width(draw, trial, font) > max_width:
                    lines.append(buf)
                    buf = ch
                else:
                    buf = trial
            if buf:
                lines.append(buf)
        return lines

    def _truncate_weekly_line(self, draw, text, font, max_width):
        """把单行文字安全截到指定像素宽度，避免统计条溢出卡片。"""
        value = str(text or "").strip()
        if self._text_width(draw, value, font) <= max_width:
            return value
        suffix = "…"
        while value and self._text_width(draw, value + suffix, font) > max_width:
            value = value[:-1]
        return value.rstrip() + suffix

    # LLM 不带 emoji 前缀时，靠这些标题词兜底识别板块
    _WEEKLY_KNOWN_TITLES = ("视频", "追番", "直播", "评论区", "动态", "碎碎念", "本周摘要", "总结")

    def _parse_weekly_sections(self, summary):
        sections = []
        current_title = "本周摘要"
        current_lines = []
        stats_line = ""
        for raw in (summary or "").splitlines():
            line = raw.strip()
            if not line or set(line) <= {"━", "-", "—", "=", "*"}:
                continue
            clean = self._strip_weekly_emoji(line)
            # 标题行：📅 开头，或 markdown 标题/纯文字形式的「周报 xx.xx ~ xx.xx」
            md = re.match(r"^#{1,4}\s*(.+)$", clean)
            md_clean = self._strip_weekly_emoji(md.group(1)) if md else ""
            if line.startswith("📅") or re.match(r"^周报\b|^周报[\s|｜]", md_clean or clean):
                current_title = (md_clean or clean) or "周报"
                continue
            # 板块标题：emoji 前缀 / markdown 标题 / 单独一行的已知标题词
            bare = (md_clean or clean).rstrip("：:").replace("**", "").strip()
            is_header = (
                any(line.startswith(p) for p in ("📺", "🎬", "🎙", "🎤", "💬", "📢", "✍"))
                or (md and bare)
                or bare in self._WEEKLY_KNOWN_TITLES
            )
            if is_header:
                if current_lines:
                    sections.append((current_title, "\n".join(current_lines).strip()))
                current_title = bare or clean or line
                current_lines = []
                continue
            if not stats_line and ("视频" in line or "番剧" in line or "互动" in line or "动态" in line) and "·" in line:
                stats_line = line
                continue
            current_lines.append(line)
        if current_lines:
            sections.append((current_title, "\n".join(current_lines).strip()))
        return stats_line, sections[:6]

    @staticmethod
    def _rounded_rect(draw, xy, radius, fill, outline=None, width=1):
        draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)

    def _render_weekly_summary_image(self, summary, report_kind="weekly"):
        """把日/周总结渲染成固定模板 PNG。失败时返回 None，不影响文本投递。"""
        try:
            from PIL import Image, ImageDraw, ImageFilter
        except Exception as e:
            logger.warning(f"[BiliBot] 周总结图片渲染不可用（缺少Pillow）: {e}")
            return None
        try:
            os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
            width = 1200
            margin = 72
            line_h = 38
            # 最多六个栏目时，8行/栏可保证全部卡片落在单张图内；异常长内容在栏内明确省略。
            max_body_lines = 8

            title_font = self._load_weekly_font(56, bold=True)
            sub_font = self._load_weekly_font(26)
            stat_font = self._load_weekly_font(28, bold=True)
            card_title_font = self._load_weekly_font(32, bold=True)
            body_font = self._load_weekly_font(28)
            small_font = self._load_weekly_font(22)

            is_daily = str(report_kind).lower() == "daily"
            week_start = (datetime.now() - timedelta(days=7)).strftime("%m.%d")
            week_end = datetime.now().strftime("%m.%d")
            stats_line, sections = self._parse_weekly_sections(summary)
            stats_line = self._clean_weekly_render_text(stats_line) or (
                "今天的B站生活记录" if is_daily else "这一周的B站生活记录"
            )

            # 先量后画：用临时画布把每张卡片的行数算出来，画布高度按内容伸缩
            meas = ImageDraw.Draw(Image.new("RGB", (width, 8)))
            max_text_w = width - margin * 2 - 70
            cards = []
            for title, body in sections:
                title = self._clean_weekly_render_text(title) or "小记"
                if is_daily and title == "本周摘要":
                    title = "今日小记"
                body = self._clean_weekly_render_text(body) or "这块内容有点安静。"
                wrapped = self._wrap_weekly_text(meas, body, body_font, max_text_w)
                if len(wrapped) > max_body_lines:
                    wrapped = wrapped[:max_body_lines]
                    last_line = wrapped[-1].rstrip()
                    wrapped[-1] = (last_line[:-1].rstrip() if len(last_line) > 1 else last_line) + "…"
                card_h = 86 + max(1, len(wrapped)) * line_h + 32
                cards.append((title, wrapped, card_h))

            header_h = 342          # 大标题 + 日期 + 统计条
            footer_h = 116
            content_h = sum(h for _, _, h in cards) + 24 * max(len(cards) - 1, 0)
            minimum_height = 900 if is_daily else 1280
            height = max(minimum_height, header_h + content_h + footer_h + 40)

            img = Image.new("RGB", (width, height), "#f7f1e8")
            draw = ImageDraw.Draw(img)
            for y in range(height):
                t = y / max(height - 1, 1)
                r = int(247 * (1 - t) + 228 * t)
                g = int(241 * (1 - t) + 238 * t)
                b = int(232 * (1 - t) + 230 * t)
                draw.line((0, y, width, y), fill=(r, g, b))

            # 柔和色块，纯代码渲染的模板背景（底部色块跟随画布高度）
            overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            od.ellipse((-180, -120, 520, 460), fill=(246, 169, 122, 85))
            od.ellipse((760, 80, 1420, 720), fill=(94, 145, 132, 75))
            od.ellipse((650, height - 560, 1320, height + 140), fill=(76, 98, 142, 55))
            overlay = overlay.filter(ImageFilter.GaussianBlur(36))
            img = Image.alpha_composite(img.convert("RGBA"), overlay)
            draw = ImageDraw.Draw(img)

            y = 74
            draw.text((margin, y), "BiliBot 日记" if is_daily else "BiliBot 周报", fill="#24312f", font=title_font)
            y += 72
            date_text = week_end if is_daily else f"{week_start} - {week_end}"
            draw.text((margin + 2, y), f"{date_text} · 自动生成", fill="#6f746f", font=sub_font)

            badge_text = "DAILY" if is_daily else "WEEKLY"
            badge_w = self._text_width(draw, badge_text, small_font) + 42
            self._rounded_rect(draw, (width - margin - badge_w, 86, width - margin, 132), 23, fill=(36, 49, 47, 230))
            draw.text((width - margin - badge_w + 21, 98), badge_text, fill="#f8f0df", font=small_font)

            y += 78
            self._rounded_rect(draw, (margin, y, width - margin, y + 86), 30, fill=(255, 252, 244, 220), outline=(229, 215, 192, 220), width=2)
            stats_line = self._truncate_weekly_line(draw, stats_line, stat_font, width - margin * 2 - 68)
            draw.text((margin + 34, y + 26), stats_line, fill="#4a4f49", font=stat_font)
            y += 118

            palette = ["#d86f45", "#477c73", "#526c9d", "#a56a43", "#6f6f48", "#8b627a"]
            content_bottom = height - 116
            for idx, (title, wrapped, card_h) in enumerate(cards):
                card_x1, card_x2 = margin, width - margin
                if y + card_h > content_bottom:
                    logger.warning("[BiliBot] 总结卡片高度计算异常，停止绘制剩余栏目")
                    break
                self._rounded_rect(draw, (card_x1, y, card_x2, y + card_h), 34, fill=(255, 253, 248, 232), outline=(229, 218, 201, 210), width=2)
                accent = palette[idx % len(palette)]
                self._rounded_rect(draw, (card_x1 + 24, y + 28, card_x1 + 38, y + card_h - 28), 7, fill=accent)
                draw.text((card_x1 + 58, y + 28), title, fill="#283330", font=card_title_font)
                ty = y + 78
                for line in wrapped:
                    draw.text((card_x1 + 58, ty), line, fill="#4b4d49", font=body_font)
                    ty += line_h
                y += card_h + 24

            footer = "Generated by astrbot_plugin_bilibili_ai_bot"
            draw.text((margin, height - 62), footer, fill="#8b8d87", font=small_font)
            filename_prefix = "daily_summary" if is_daily else "weekly_summary"
            path = os.path.join(TEMP_IMAGE_DIR, f"{filename_prefix}_{int(time.time())}.png")
            img.convert("RGB").save(path, "PNG", optimize=True)
            logger.info(f"[BiliBot] 周总结图片已渲染: {path}")
            return path
        except Exception as e:
            logger.warning(f"[BiliBot] 周总结图片渲染失败: {e}", exc_info=True)
            return None

    def _append_image_to_chain(self, chain, image_path):
        from astrbot.api.message_components import Image as MsgImage
        img = MsgImage.fromFileSystem(image_path)
        if hasattr(chain, "chain"):
            chain.chain.append(img)
            return chain
        if hasattr(chain, "append"):
            chain.append(img)
            return chain
        return None

    # ── 投递 ──

    async def _deliver_weekly_summary(self, summary, image_path=None):
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
                    chain = MessageChain().message("📅 本周B站生活周报")
                    if image_path:
                        chain = self._append_image_to_chain(chain, image_path) or MessageChain().message(summary)
                    else:
                        chain = MessageChain().message(summary)
                    await self.context.send_message(umo, chain)
                    results.append("QQ私信图片" if image_path else "QQ私信")
                    logger.info("[BiliBot] 📅 周总结已通过QQ私信发送")
                except Exception as e:
                    logger.warning(f"[BiliBot] 周总结QQ图片发送失败，尝试退回文本: {e}")
                    try:
                        from astrbot.api.event import MessageChain
                        await self.context.send_message(umo, MessageChain().message(summary))
                        results.append("QQ私信文本")
                    except Exception as e2:
                        logger.warning(f"[BiliBot] 周总结QQ文本发送失败: {e2}")
            else:
                logger.warning("[BiliBot] 周总结模式包含qq但未配置UMO（周总结/恶意告警的UMO都为空）")

        if mode in ("dynamic", "both"):
            try:
                success = False
                has_image = False
                dynamic_text = summary
                if image_path:
                    img_info = await self._upload_image_to_bilibili(image_path)
                    if img_info:
                        dynamic_text = "📅 这周的B站生活周报来啦，整理成图片存档一下。"
                        success = await self._queue_dynamic_post(
                            dynamic_text,
                            lambda: self._post_dynamic_with_image(dynamic_text, img_info),
                        )
                        has_image = success
                if not success:
                    success = await self._queue_dynamic_post(
                        summary, lambda: self._post_dynamic_text(summary)
                    )
                if success:
                    results.append("B站动态图片" if has_image else "B站动态")
                    log = self._load_json(DYNAMIC_LOG_FILE, [])
                    log.append({
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "text": dynamic_text, "has_image": has_image, "weekly": True,
                        "image_path": image_path if has_image else "",
                    })
                    self._save_json(DYNAMIC_LOG_FILE, log[-100:])
                    logger.info("[BiliBot] 📅 周总结已发布为B站动态")
            except Exception as e:
                logger.warning(f"[BiliBot] 周总结动态发布失败: {e}")

        return results

    async def _generate_daily_summary(self):
        """Generate a compact daily Bilibili-life recap from today's real records."""
        data = self._collect_weekly_data(days=1)
        live_events = data.get("live_events") if isinstance(data.get("live_events"), list) else []
        lifecycle = await self._sync_preference_lifecycle(data)
        feedback = await self._feedback_summary(1)
        structured = self._build_structured_activity_summary(
            data,
            period_key=datetime.now().strftime("%Y-%m-%d"),
            preferences=lifecycle.get("current", []) if isinstance(lifecycle, dict) else [],
            feedback=feedback,
        )
        self._last_daily_structured_summary = structured
        if not (data["videos"] or data["bangumi"] or data["dynamics"] or data["proactive_comments"] or data["chat_count"] or live_events):
            logger.info("[BiliBot] 今日没有活动记录，跳过日总结正文生成")
            return None
        data_text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
        prompt = f"""请依据下面的真实记录，写一段自然、克制的今日B站生活小结。
日期：{datetime.now().strftime('%Y-%m-%d')}
今日结构化摘要：
{data_text}

要求：
- 只写真实发生的事情，资料不足就少写，禁止编造。
- 可以提到看过的视频、动态、评论区或直播里的有趣片段，但不要泄露UID、私信原文、账号凭据或其他隐私。
- 像角色睡前随手记，80-180字，不要写成运营报表。
- 直接输出正文。"""
        custom = str(self.config.get("CUSTOM_DAILY_INSTRUCTION", "") or "").strip()
        if custom:
            prompt += f"\n【管理员补充提示词】{custom[:1000]}"
        summary = await self._llm_call(prompt, system_prompt=await self._get_system_prompt(), max_tokens=500)
        return (summary or "").strip() or None

    async def _deliver_daily_summary(self, summary, image_path=None):
        mode = str(self.config.get("DAILY_SUMMARY_MODE", "archive") or "archive").lower().strip()
        results = []
        if mode in ("qq", "both"):
            umo = str(self.config.get("DAILY_SUMMARY_QQ_UMO", "") or self.config.get("WEEKLY_SUMMARY_QQ_UMO", "") or self.config.get("ABUSE_ALERT_QQ_UMO", "")).strip()
            if umo:
                try:
                    from astrbot.api.event import MessageChain
                    chain = MessageChain().message("今日B站生活小结")
                    if image_path:
                        chain = self._append_image_to_chain(chain, image_path) or MessageChain().message(summary)
                    else:
                        chain = MessageChain().message(summary)
                    await self.context.send_message(umo, chain)
                    results.append("QQ私信图片" if image_path else "QQ私信")
                except Exception as exc:
                    logger.warning(f"[BiliBot] 日总结QQ投递失败: {exc}")
        if mode in ("dynamic", "both"):
            try:
                success = False
                has_image = False
                dynamic_text = summary
                if image_path:
                    image_info = await self._upload_image_to_bilibili(image_path)
                    if image_info:
                        dynamic_text = "今天的B站生活小结，留个轻量存档。"
                        success = await self._queue_dynamic_post(
                            dynamic_text,
                            lambda: self._post_dynamic_with_image(
                                dynamic_text, image_info
                            ),
                        )
                        has_image = success
                if not success:
                    success = await self._queue_dynamic_post(
                        summary, lambda: self._post_dynamic_text(summary)
                    )
                if success:
                    results.append("B站动态图片" if has_image else "B站动态")
                    log = self._load_json(DYNAMIC_LOG_FILE, [])
                    log.append({
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "text": dynamic_text,
                        "has_image": has_image,
                        "daily_summary": True,
                        "image_path": image_path if has_image else "",
                    })
                    self._save_json(DYNAMIC_LOG_FILE, log[-100:])
            except Exception as exc:
                logger.warning(f"[BiliBot] 日总结动态投递失败: {exc}")
        return results

    def _daily_summary_done_today(self):
        records = self._load_json(DAILY_SUMMARY_FILE, [])
        today = datetime.now().strftime("%Y-%m-%d")
        return any(isinstance(item, dict) and item.get("date") == today for item in (records if isinstance(records, list) else []))

    def _save_daily_summary_record(self, summary, delivered, image_path="", structured=None):
        records = self._load_json(DAILY_SUMMARY_FILE, [])
        if not isinstance(records, list):
            records = []
        records.append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": summary,
            "delivered": delivered,
            "image_path": image_path or "",
            "structured": structured if isinstance(structured, dict) else {},
        })
        self._save_json(DAILY_SUMMARY_FILE, records[-45:])

    async def _maybe_daily_summary(self):
        if not self.config.get("ENABLE_DAILY_SUMMARY", False) or self._daily_summary_done_today():
            return
        try:
            target_hour = int(self.config.get("DAILY_SUMMARY_HOUR", 3)) % 24
        except (ValueError, TypeError):
            target_hour = 3
        if datetime.now().hour != target_hour:
            return
        await self.run_daily_summary()

    async def run_daily_summary(self):
        logger.info("[BiliBot] 开始生成日总结...")
        try:
            summary = await self._generate_daily_summary()
        except Exception as exc:
            logger.error(f"[BiliBot] 日总结生成异常: {exc}")
            return None, [], None
        structured = getattr(self, "_last_daily_structured_summary", {})
        period_key = datetime.now().strftime("%Y-%m-%d")
        if not summary:
            placeholder = "（今日无可总结活动）"
            self._save_daily_summary_record(placeholder, [], "", structured)
            await self._persist_cycle_summary("daily", period_key, structured, placeholder, [])
            return None, [], None
        image_path = self._render_weekly_summary_image(summary, report_kind="daily") if self.config.get("DAILY_SUMMARY_RENDER_IMAGE", True) else None
        delivered = await self._deliver_daily_summary(summary, image_path=image_path)
        self._save_daily_summary_record(summary, delivered, image_path or "", structured)
        await self._persist_cycle_summary("daily", period_key, structured, summary, delivered)
        logger.info(f"[BiliBot] 日总结完成，投递：{delivered or ['仅存档']}")
        return summary, delivered, image_path

    # ── 调度 ──

    def _weekly_summary_done_this_week(self):
        """检查本ISO周是否已生成过周总结。"""
        records = self._load_json(WEEKLY_SUMMARY_FILE, [])
        if not records:
            return False
        this_week = datetime.now().strftime("%G-W%V")
        return any(r.get("week") == this_week for r in records if isinstance(r, dict))

    def _save_weekly_summary_record(self, summary, delivered, image_path="", structured=None):
        records = self._load_json(WEEKLY_SUMMARY_FILE, [])
        records.append({
            "week": datetime.now().strftime("%G-W%V"),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": summary,
            "delivered": delivered,
            "image_path": image_path or "",
            "structured": structured if isinstance(structured, dict) else {},
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
            return None, [], None
        structured = getattr(self, "_last_weekly_structured_summary", {})
        period_key = datetime.now().strftime("%G-W%V")
        if not summary:
            # 没有活动也记录一下，避免同一周反复尝试
            placeholder = "（本周无活动，未生成）"
            self._save_weekly_summary_record(placeholder, [], "", structured)
            await self._persist_cycle_summary("weekly", period_key, structured, placeholder, [])
            return None, [], None
        image_path = self._render_weekly_summary_image(summary) if self.config.get("WEEKLY_SUMMARY_RENDER_IMAGE", True) else None
        delivered = await self._deliver_weekly_summary(summary, image_path=image_path)
        self._save_weekly_summary_record(summary, delivered, image_path or "", structured)
        await self._persist_cycle_summary("weekly", period_key, structured, summary, delivered)
        logger.info(f"[BiliBot] 📅 周总结完成，投递：{delivered or ['仅存档']}")
        return summary, delivered, image_path
