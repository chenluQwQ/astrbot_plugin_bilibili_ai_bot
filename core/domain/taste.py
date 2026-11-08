"""近期口味：从观看日志和偏好状态算「最近爱看什么」，只读。

看片评价、搜索词、预筛和 ``/bili兴趣`` 都从这里拿。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from astrbot.api import logger

from ..infra.host_view import HostView


class Taste(HostView):
    HOST_API = frozenset({"activity_logs", "json_store", "preferences", "settings", "video_pools"})

    def _taste_window_days(self):
        try:
            return max(1, int(self.settings.get("PROACTIVE_TASTE_WINDOW_DAYS") or 7))
        except (TypeError, ValueError):
            return 7

    def _recent_taste_entries(self, watch_log=None, days=None):
        history = watch_log if isinstance(watch_log, list) else self.activity_logs.watch.recent()
        window_days = max(1, int(days or self._taste_window_days()))
        cutoff = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M")
        return [
            entry for entry in history
            if isinstance(entry, dict) and str(entry.get("time", "")) >= cutoff
        ]

    def _get_recent_taste_stats(self, watch_log=None, days=None):
        """按分区汇总近期有效评分，供搜索词和偏好分区共同使用。"""
        from collections import defaultdict

        grouped = defaultdict(list)
        for entry in self._recent_taste_entries(watch_log, days=days):
            tname = re.sub(r"\s+", " ", str(entry.get("tname", "") or "")).strip()
            try:
                score = float(entry.get("score", 0) or 0)
            except (TypeError, ValueError):
                continue
            if tname and 1 <= score <= 10:
                grouped[tname].append(score)

        stats = []
        for tname, scores in grouped.items():
            stats.append({
                "tname": tname,
                "count": len(scores),
                "average": sum(scores) / len(scores),
                "high_count": sum(1 for score in scores if score >= 7),
                "low_count": sum(1 for score in scores if score <= 4),
                "scores": scores,
            })
        return stats

    def _format_recent_taste_summary(self, watch_log=None, days=None):
        window_days = max(1, int(days or self._taste_window_days()))
        stats = self._get_recent_taste_stats(watch_log, days=window_days)
        if not stats:
            return f"- 最近{window_days}天暂无带分区的有效评分，可以自由探索"

        ranked = sorted(
            stats,
            key=lambda item: (item["average"], item["count"]),
            reverse=True,
        )
        selected = ranked[:5]
        disliked = sorted(
            (item for item in stats if item["average"] <= 5),
            key=lambda item: (item["average"], -item["count"]),
        )
        for item in disliked[:2]:
            if item not in selected:
                selected.append(item)

        lines = []
        for item in selected[:7]:
            average = item["average"]
            label = "偏喜欢" if average >= 7 else "不太喜欢" if average <= 4 else "感觉一般"
            lines.append(
                f"- {item['tname']}：{item['count']}个，平均{average:.1f}/10（{label}）"
            )
        return "\n".join(lines)

    def _format_recent_preference_summary(
        self, watch_log=None, days=None, lifecycle_items=None
    ):
        """Summarize concrete, evidence-bearing signals without promoting them yet."""
        from collections import defaultdict

        try:
            half_life = max(1, int(self.settings.get("PROACTIVE_PREFERENCE_HALF_LIFE_DAYS")))
        except (TypeError, ValueError):
            half_life = 7
        grouped = defaultdict(lambda: {"like": 0.0, "dislike": 0.0, "fatigue": 0.0, "curious": 0.0, "count": 0})
        for entry in self._recent_taste_entries(watch_log, days=days):
            for signal in entry.get("preference_signals", []) or []:
                if not isinstance(signal, dict):
                    continue
                signal_type = str(signal.get("type") or "other")
                value = re.sub(r"\s+", " ", str(signal.get("value") or "")).strip()
                polarity = str(signal.get("polarity") or "")
                try:
                    strength = max(0.0, min(1.0, float(signal.get("strength", 0))))
                except (TypeError, ValueError):
                    continue
                if value and polarity in {"like", "dislike", "fatigue", "curious"}:
                    item = grouped[(signal_type, value)]
                    try:
                        age = max(0, (datetime.now() - datetime.strptime(str(entry.get("time")), "%Y-%m-%d %H:%M")).total_seconds() / 86400)
                    except (TypeError, ValueError):
                        age = 0
                    item[polarity] += strength * (0.5 ** (age / half_life))
                    item["count"] += 1
        if lifecycle_items is None:
            lifecycle_items = self.preferences.cached()
        if not grouped and not lifecycle_items:
            return "- 暂无具体作品、人物、UP或主题信号，可以自由探索"
        ranked = sorted(
            grouped.items(),
            key=lambda pair: (
                max(pair[1]["like"], pair[1]["curious"], pair[1]["dislike"] + pair[1]["fatigue"]),
                pair[1]["count"],
            ),
            reverse=True,
        )
        lines = []
        for (signal_type, value), stats in ranked[:8]:
            positive = stats["like"] + stats["curious"] * 0.6
            negative = stats["dislike"] + stats["fatigue"]
            if negative > positive:
                tendency = "近期有些厌倦/不喜欢"
            elif stats["curious"] > stats["like"]:
                tendency = "近期好奇"
            else:
                tendency = "近期偏喜欢"
            lines.append(
                f"- {signal_type}:{value}（{tendency}，证据{stats['count']}次）"
            )
        existing = {(key[0], key[1]) for key in grouped}
        for item in lifecycle_items:
            signal_type = str(item.get("signal_type") or "other")
            value = re.sub(r"\s+", " ", str(item.get("value") or "")).strip()
            if not value or (signal_type, value) in existing:
                continue
            tendency = {
                "like": "喜欢", "curious": "好奇", "dislike": "不喜欢",
                "fatigue": "审美疲劳",
            }.get(str(item.get("polarity") or ""), "倾向不明")
            stage = {"candidate": "候选", "recent": "近期", "stable": "稳定"}.get(
                str(item.get("stage") or ""), "候选"
            )
            lines.append(
                f"- {signal_type}:{value}（{stage}{tendency}，证据{int(item.get('evidence_count', 0) or 0)}次）"
            )
            if len(lines) >= 8:
                break
        return "\n".join(lines)

    def _format_interest_report(self, watch_log=None, lifecycle_items=None):
        """Render recent video taste separately from evidence-backed preferences."""
        from collections import Counter, defaultdict

        window_days = self._taste_window_days()
        entries = self._recent_taste_entries(watch_log, days=window_days)
        valid = []
        for item in entries:
            try:
                score = float(item.get("score", 0) or 0)
            except (TypeError, ValueError):
                continue
            if 1 <= score <= 10:
                valid.append((item, score))

        lines = ["🎯 BiliBot 视频兴趣", "━━━━━━━━━━━━"]
        lines.append(
            f"统计窗口：最近{window_days}天｜看过{len(entries)}个｜"
            f"有效评分{len(valid)}个｜待评价{max(0, len(entries) - len(valid))}个"
        )

        lines.append("\n【近期分区口味】")
        partition_stats = sorted(
            self._get_recent_taste_stats(entries, days=window_days),
            key=lambda item: (item["average"], item["count"]),
            reverse=True,
        )
        if partition_stats:
            for item in partition_stats[:6]:
                average = float(item["average"])
                tendency = (
                    "偏喜欢"
                    if average >= 7
                    else "不太喜欢"
                    if average <= 4
                    else "感觉一般"
                )
                lines.append(
                    f"  · {item['tname']}：{item['count']}个，"
                    f"平均{average:.1f}/10（{tendency}）"
                )
        else:
            lines.append("  暂无带分区的有效评分")

        up_scores = defaultdict(list)
        for item, score in valid:
            name = re.sub(
                r"\s+", " ", str(item.get("up_name") or item.get("owner_name") or "")
            ).strip()
            if name:
                up_scores[name].append(score)
        lines.append("\n【近期 UP 样本】")
        if up_scores:
            ranked_ups = sorted(
                up_scores.items(),
                key=lambda pair: (len(pair[1]), sum(pair[1]) / len(pair[1])),
                reverse=True,
            )
            for name, scores in ranked_ups[:6]:
                sample = "，样本较少" if len(scores) == 1 else ""
                lines.append(
                    f"  · {name}：{len(scores)}个，"
                    f"平均{sum(scores) / len(scores):.1f}/10{sample}"
                )
        else:
            lines.append("  暂无可判断的 UP 评分样本")

        has_signals = any(
            isinstance(item.get("preference_signals"), list)
            and item.get("preference_signals")
            for item in entries
        )
        lines.append("\n【近期具体兴趣信号】")
        if has_signals:
            recent_summary = self._format_recent_preference_summary(
                entries, days=window_days, lifecycle_items=[]
            )
            lines.extend(
                "  · " + line[2:] if line.startswith("- ") else "  " + line
                for line in recent_summary.splitlines()
            )
        else:
            lines.append("  暂无；新版会从之后完成的视频评价中逐步积累")

        if lifecycle_items is None:
            lifecycle_items = self.preferences.cached()
        lifecycle_items = [
            item
            for item in (lifecycle_items or [])
            if isinstance(item, dict) and str(item.get("value") or "").strip()
        ]
        type_labels = {
            "up": "UP",
            "partition": "分区",
            "work": "作品",
            "character": "人物",
            "food": "食物",
            "topic": "主题",
            "theme": "主题",
            "other": "其他",
        }
        polarity_labels = {
            "like": "喜欢",
            "curious": "好奇",
            "dislike": "不喜欢",
            "fatigue": "审美疲劳",
        }
        stage_labels = {"candidate": "候选", "recent": "近期", "stable": "稳定"}
        lines.append("\n【已沉淀偏好】")
        if lifecycle_items:
            for item in lifecycle_items[:8]:
                signal_type = str(item.get("signal_type") or "other")
                stage = stage_labels.get(str(item.get("stage") or ""), "候选")
                polarity = polarity_labels.get(
                    str(item.get("polarity") or ""), "倾向不明"
                )
                evidence = int(item.get("evidence_count", 0) or 0)
                active_weeks = int(item.get("active_weeks", 0) or 0)
                week_note = f"，跨{active_weeks}周" if active_weeks else ""
                lines.append(
                    f"  · [{stage}{polarity}] "
                    f"{type_labels.get(signal_type, signal_type)}："
                    f"{str(item.get('value') or '').strip()[:60]}"
                    f"（证据{evidence}次{week_note}）"
                )
        else:
            lines.append("  尚未形成；单次观看不会直接写成稳定兴趣")

        exploration = Counter()
        for item in entries:
            for keyword in item.get("search_keywords", []) or []:
                keyword = re.sub(r"\s+", " ", str(keyword or "")).strip()[:40]
                if keyword:
                    exploration[keyword] += 1
            if str(item.get("source") or "") == "search":
                keyword = re.sub(
                    r"\s+", " ", str(item.get("source_detail") or "")
                ).strip()[:40]
                if keyword and keyword.lower() not in {"search", "bilibili"}:
                    exploration[keyword] += 1
        lines.append("\n【最近探索方向】")
        if exploration:
            lines.append(
                "  "
                + "、".join(
                    f"{keyword}×{count}" if count > 1 else keyword
                    for keyword, count in exploration.most_common(6)
                )
            )
        else:
            lines.append("  暂无明确搜索词")

        lines.append(
            "\n说明：近期口味是观察样本；同一信号反复出现才会进入近期偏好，"
            "连续跨周后才会成为稳定偏好。"
        )
        return "\n".join(lines)

    def _get_taste_tids(self, min_score=7, min_count=2):
        """从最近一周高分视频中提取偏好分区 tid 列表（按加权得分排序）。

        返回 list[int]，最多10个。空列表表示口味数据不足。
        """
        watch_log = self.activity_logs.watch.recent()
        tname_map = self.video_pools._build_tname_to_tid_map()
        tid_count = {}
        tid_score_sum = {}
        for item in self._get_recent_taste_stats(watch_log):
            tid = tname_map.get(item["tname"])
            high_scores = [score for score in item["scores"] if score >= min_score]
            if tid and high_scores:
                tid_count[tid] = len(high_scores)
                tid_score_sum[tid] = sum(high_scores)
        # 过滤：至少出现 min_count 次的分区才算稳定偏好
        qualified = {tid: cnt for tid, cnt in tid_count.items() if cnt >= min_count}
        if not qualified:
            return []
        # 加权排序：次数 × 平均分
        ranked = sorted(
            qualified.keys(),
            key=lambda t: qualified[t] * (tid_score_sum[t] / qualified[t]),
            reverse=True,
        )
        result = ranked[:10]
        for item in self.preferences.cached():
            if item.get("signal_type") != "partition":
                continue
            if item.get("polarity") not in {"like", "curious"}:
                continue
            if item.get("stage") not in {"recent", "stable"}:
                continue
            tid = tname_map.get(str(item.get("value") or "").strip())
            if tid and tid not in result:
                result.append(tid)
            if len(result) >= 10:
                break
        logger.info(
            f"[BiliBot] 🎯 口味偏好TID: {result}（最近{self._taste_window_days()}天）"
        )
        return result
