"""把一段时间的活动日志整理成总结用的材料，只读（偏好生命周期同步除外）。人格演化也用。"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta

from ..domain.preferences import Preferences
from ..infra.host_view import HostView


class ActivityDigest(HostView):
    HOST_API = frozenset({"activity_logs", "json_store", "layered_runtime", "memories"})

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

        videos = _recent(self.activity_logs.watch.recent())
        # 没看成的集（评价失败、出错）只留在日志里，不算进总结。
        bangumi = [b for b in _recent(self.activity_logs.bangumi.recent()) if b.get("evaluation_status") != "failed"]
        dynamics = _recent(self.activity_logs.dynamic.recent())
        proactive_comments = _recent(self.activity_logs.proactive.recent())

        # 聊天互动：chat 类型记忆，统计互动条数和活跃用户
        chats = [
            m for m in getattr(getattr(self, "memories", None), "records", [])
            if isinstance(m, dict)
            and m.get("memory_type") == "chat"
            and m.get("time", "") >= cutoff
            and m.get("user_id") not in (None, "", "self")
        ]
        user_counter = Counter(m.get("user_id", "") for m in chats)
        live_events = [
            m for m in getattr(getattr(self, "memories", None), "records", [])
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
        observations = []
        for item in data.get("videos", []) or []:
            signals = item.get("preference_signals", []) or []
            if not signals:
                continue
            source_ref = f"{item.get('bvid') or 'video'}:{str(item.get('time') or '')[:16]}"
            observations.append((source_ref, signals, self._activity_time(item.get("time"))))
        return await Preferences(self).sync(observations)

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
        states = getattr(self, "states", None)
        records = states.daily_summary.read([]) if states is not None else []
        for record in records if isinstance(records, list) else []:
            day = str(record.get("date") or "")
            structured = record.get("structured")
            if day >= cutoff and isinstance(structured, dict):
                derived[day] = structured
        return [derived[day] for day in sorted(derived)]

    @staticmethod
    def _format_daily_structures(structures):
        return json.dumps(structures, ensure_ascii=False, separators=(",", ":"))
