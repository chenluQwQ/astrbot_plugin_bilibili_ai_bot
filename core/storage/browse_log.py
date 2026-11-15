"""浏览日志（移植自 1.x 的 browse_log）：把看片、看片评论、评论区回复聚合成按视频分组的列表。

只读 ``activity_logs``（sqlite 的 ``activity_log`` 表），不写任何状态：
- ``watch``     每个看过（含评价失败）的视频：想法（score_reason）、感受（review）、当时动作
- ``proactive`` 看片后留下的公开评论
- ``reply``     评论区回复 / B站私信回复（含热评楼中楼等 server_ext 补丁写的记录）

评价失败的记录默认不进列表，只体现在「评价失败」计数里；明细抽屉单独拉。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

BILI_VIDEO_URL = "https://www.bilibili.com/video/{bvid}"

RANGE_LABELS = {"today": "今日", "yesterday": "昨日", "7d": "近7天"}
RANGE_ALIASES = {
    "today": "today", "今日": "today",
    "yesterday": "yesterday", "昨日": "yesterday", "昨天": "yesterday",
    "7d": "7d", "week": "7d", "近7天": "7d", "过去7天": "7d",
}


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def range_bounds(range_key: str) -> tuple[datetime, datetime]:
    """返回 [start, end) 的自然日边界。"""
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_key == "yesterday":
        return today - timedelta(days=1), today
    if range_key == "7d":
        return today - timedelta(days=6), now + timedelta(seconds=1)
    return today, now + timedelta(seconds=1)


def normalize_range(value: Any) -> str:
    return RANGE_ALIASES.get(str(value or "").strip(), "today")


def _in_range(value: Any, start: datetime, end: datetime) -> bool:
    parsed = _parse_time(value)
    return parsed is not None and start <= parsed < end


def _video_url(bvid: str) -> str:
    bvid = str(bvid or "").strip()
    return BILI_VIDEO_URL.format(bvid=bvid) if bvid else ""


def _score_value(rec: dict) -> float:
    raw = rec.get("score")
    if raw in (None, ""):
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _is_failed_record(rec: dict) -> bool:
    """显式 evaluation_status=failed 算失败；老记录没有该字段时用「没有正分且没有想法」兜底。"""
    if str(rec.get("evaluation_status") or "").strip().lower() == "failed":
        return True
    return _score_value(rec) <= 0 and not str(rec.get("score_reason") or "").strip()


def _new_item(rec: dict) -> dict:
    bvid = str(rec.get("bvid") or "").strip()
    failed = str(rec.get("evaluation_status") or "").strip().lower() == "failed"
    return {
        "time": str(rec.get("time") or "").strip(),
        "bvid": bvid,
        "url": _video_url(bvid),
        "title": str(rec.get("title") or "").strip() or (f"视频 {bvid}" if bvid else "未知视频"),
        "up_name": "" if failed else str(rec.get("up_name") or "").strip(),
        "tname": "" if failed else str(rec.get("tname") or "").strip(),
        "score": None if failed else rec.get("score"),
        "mood": "" if failed else str(rec.get("mood") or "").strip(),
        "thought": "" if failed else str(rec.get("score_reason") or "").strip(),
        "review": "" if failed else str(rec.get("review") or "").strip(),
        "comment": "",
        "actions": [] if failed else [str(a) for a in (rec.get("actions") or [])],
        "source": str(rec.get("source") or "").strip(),
        "source_detail": str(rec.get("source_detail") or "").strip(),
        "status": "failed" if failed else ("ok" if _score_value(rec) > 0 else ""),
        "comments": [],
        "replies": [],
    }


def _kinds(bot: Any) -> Any:
    return getattr(bot, "activity_logs", None)


def _range_slice(bot: Any, kind: str, start: datetime, end: datetime) -> list[dict]:
    """取某类日志在 [start, end) 的条目：2.x 用 on_day 逐日拼，读不到就空。"""
    logs = _kinds(bot)
    log = getattr(logs, kind, None) if logs else None
    if log is None:
        return []
    rows: list[dict] = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        stamp = day.strftime("%Y-%m-%d")
        try:
            rows.extend(item for item in (log.on_day(stamp) or []) if isinstance(item, dict))
        except Exception:
            pass
        day += timedelta(days=1)
    return [r for r in rows if _in_range(r.get("time"), start, end)]


def _all_titles(bot: Any) -> dict[str, str]:
    """bvid → 标题：reply 记录没带标题时用来补。取最近几百条 watch 就够。"""
    logs = _kinds(bot)
    log = getattr(logs, "watch", None) if logs else None
    if log is None:
        return {}
    titles: dict[str, str] = {}
    try:
        rows = log.recent() or []
    except Exception:
        return {}
    for rec in rows:
        if isinstance(rec, dict) and rec.get("bvid"):
            titles[str(rec["bvid"])] = str(rec.get("title") or "")
    return titles


def collect_browse_entries(bot: Any, range_key: str = "today", limit: int = 300, include_failed: bool = False) -> dict:
    """按范围聚合浏览记录，返回 {range, label, counts, items}。同一视频只留最新一次看片记录。"""
    key = normalize_range(range_key)
    start, end = range_bounds(key)
    limit = max(1, min(int(limit or 300), 1000))

    watch = _range_slice(bot, "watch", start, end)
    comments = _range_slice(bot, "proactive", start, end)
    replies = _range_slice(bot, "reply", start, end)
    titles = _all_titles(bot)

    items: dict[str, dict] = {}
    order: list[str] = []
    for rec in sorted(watch, key=lambda r: str(r.get("time") or "")):
        bvid = str(rec.get("bvid") or "").strip()
        item_key = bvid or f"anon-{rec.get('time')}"
        items[item_key] = _new_item(rec)  # 同一视频取最新一次记录
        if item_key not in order:
            order.append(item_key)

    def _attach(rec: dict, field: str) -> None:
        bvid = str(rec.get("bvid") or "").strip()
        item = items.get(bvid)
        if item is None:
            item = _new_item({"bvid": bvid, "time": rec.get("time"), "title": titles.get(bvid, "")})
            items[bvid or f"anon-{rec.get('time')}"] = item
            order.append(bvid or f"anon-{rec.get('time')}")
        item[field].append(rec)

    for rec in sorted(comments, key=lambda r: str(r.get("time") or "")):
        _attach(rec, "comments")
    for rec in sorted(replies, key=lambda r: str(r.get("time") or "")):
        _attach(rec, "replies")

    entries: list[dict] = []
    for item_key in order:
        item = items[item_key]
        if item["title"] == "未知视频" and item["comments"]:
            item["title"] = str(item["comments"][0].get("title") or "未知视频")
        # 看片评论可能有多条，取最后一条作为主评论
        item["comment"] = str((item["comments"][-1].get("comment") if item["comments"] else "") or "").strip()
        item["comment_count"] = len(item["comments"])
        item["reply_count"] = len(item["replies"])
        if str(item["status"]).lower() == "failed" or (_score_value(item) <= 0 and not item["thought"] and not item["comment"] and not item["replies"]):
            # 没评分也没想法/评论/回复的，等同于没进入分析流程
            item["status"] = "failed"
        entries.append(item)

    entries.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
    failed_entries = [item for item in entries if item.get("status") == "failed"]
    if not include_failed:
        entries = [item for item in entries if item.get("status") != "failed"]
    entries = entries[:limit]

    return {
        "range": key,
        "label": RANGE_LABELS.get(key, key),
        "start": start.strftime("%Y-%m-%d %H:%M"),
        "end": end.strftime("%Y-%m-%d %H:%M"),
        "include_failed": bool(include_failed),
        "counts": {
            "videos": len(entries),
            "candidates": len(watch),
            "comments": len(comments),
            "replies": len(replies),
            "failed": len(failed_entries),
        },
        "items": entries,
    }


# ── 明细视图：评论区回复 / 评价失败 ─────────────────────────────

REPLY_CHANNEL_LABELS = {
    "comment": "评论区回复",
    "private": "B站私信",
    "live": "直播间弹幕",
}

FAILURE_STAGE_LABELS = {
    "content": "内容获取",
    "fetch": "抓取视频",
    "media": "媒体解析",
    "subtitle": "字幕获取",
    "vision": "画面识别",
    "evaluation": "评价生成",
    "evaluation_schema": "评价结果解析",
    "llm": "模型调用",
    "unknown": "未分类",
}

FAILURE_REASON_LABELS = {
    "evaluation_schema_mismatch": "模型返回字段不匹配",
    "invalid_json": "模型返回非法 JSON",
    "invalid_mood": "心情取值越界",
    "invalid_preference_type": "偏好类型越界",
    "score_reason_too_long": "评分理由超长",
    "score_out_of_range": "评分超出范围",
    "empty_response": "模型返回空内容",
    "timeout": "调用超时",
    "content_unavailable": "内容不可用",
}

ANALYSIS_BASIS_LABELS = {
    "video_media_low_token": "字幕与画面信息量不足",
    "video_media": "字幕+画面",
    "video_subtitle": "仅字幕",
    "video_frames": "仅抽帧",
    "title_only": "只有标题与简介",
    "": "未记录",
}


def _label(table: dict, raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    return table.get(text) or text


def _reply_channel(rec: dict) -> str:
    raw = str(rec.get("channel") or "").strip().lower()
    if raw:
        return raw
    source = str(rec.get("source") or "").strip().lower()
    if "private" in source or str(rec.get("thread_id") or "").startswith("private:"):
        return "private"
    if "live" in source:
        return "live"
    return "comment"


def collect_reply_entries(bot: Any, range_key: str = "today", limit: int = 500) -> dict:
    """评论区回复 / 私信回复明细，一条一行按时间倒序（不去重、含热评楼中楼）。"""
    key = normalize_range(range_key)
    start, end = range_bounds(key)
    limit = max(1, min(int(limit or 500), 2000))
    titles = _all_titles(bot)
    raw = _range_slice(bot, "reply", start, end)

    items = []
    for rec in raw:
        bvid = str(rec.get("bvid") or "").strip()
        channel = _reply_channel(rec)
        items.append(
            {
                "time": str(rec.get("time") or "").strip(),
                "bvid": bvid,
                "url": _video_url(bvid),
                "title": str(rec.get("title") or rec.get("video_title") or "").strip() or titles.get(bvid, ""),
                "username": str(rec.get("username") or "").strip() or "对方",
                "user_id": str(rec.get("mid") or rec.get("user_id") or "").strip(),
                "content": str(rec.get("content") or "").strip(),
                "reply": str(rec.get("reply") or "").strip(),
                "score_delta": rec.get("score_delta"),
                "rpid": str(rec.get("rpid") or rec.get("own_rpid") or "").strip(),
                "channel": channel,
                "channel_label": REPLY_CHANNEL_LABELS.get(channel, channel),
            }
        )
    items.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
    items = items[:limit]

    by_channel: dict[str, int] = {}
    for item in items:
        by_channel[item["channel"]] = by_channel.get(item["channel"], 0) + 1

    return {
        "range": key,
        "label": RANGE_LABELS.get(key, key),
        "start": start.strftime("%Y-%m-%d %H:%M"),
        "end": end.strftime("%Y-%m-%d %H:%M"),
        "count": len(raw),
        "shown": len(items),
        "by_channel": by_channel,
        "items": items,
    }


def collect_failure_entries(bot: Any, range_key: str = "today", limit: int = 500) -> dict:
    """评价失败明细：紧凑字段 + 完整诊断（前端展开时才用）。"""
    key = normalize_range(range_key)
    start, end = range_bounds(key)
    limit = max(1, min(int(limit or 500), 2000))
    raw = [r for r in _range_slice(bot, "watch", start, end) if _is_failed_record(r)]

    items = []
    stage_counter: dict[str, int] = {}
    reason_counter: dict[str, int] = {}
    for rec in raw:
        diagnostic = rec.get("diagnostic") if isinstance(rec.get("diagnostic"), dict) else {}
        stage = str(diagnostic.get("stage") or "").strip() or "unknown"
        reason = str(rec.get("diagnostic", {}).get("reason") if isinstance(rec.get("diagnostic"), dict) else "") or str(rec.get("evaluation_status") or "")
        reason_text = str(reason or "").strip()
        basis = str(diagnostic.get("analysis_basis") or rec.get("analysis_basis") or "").strip()
        media_errors = [
            {"stage": _label(FAILURE_STAGE_LABELS, err.get("stage")), "reason": str(err.get("reason") or "").strip()}
            for err in (diagnostic.get("media_errors") or [])
            if isinstance(err, dict)
        ]
        model_call = diagnostic.get("model_call") if isinstance(diagnostic.get("model_call"), dict) else {}
        bvid = str(rec.get("bvid") or "").strip()
        stage_counter[stage] = stage_counter.get(stage, 0) + 1
        reason_counter[reason_text] = reason_counter.get(reason_text, 0) + 1
        items.append(
            {
                "time": str(rec.get("time") or "").strip(),
                "bvid": bvid,
                "url": _video_url(bvid),
                "title": str(rec.get("title") or "").strip() or (f"视频 {bvid}" if bvid else "未知视频"),
                "up_name": str(rec.get("up_name") or "").strip(),
                "tname": str(rec.get("tname") or "").strip(),
                "source": str(rec.get("source") or "").strip(),
                "source_detail": str(rec.get("source_detail") or "").strip(),
                "stage": stage,
                "stage_label": _label(FAILURE_STAGE_LABELS, stage),
                "reason": reason_text,
                "reason_label": _label(FAILURE_REASON_LABELS, reason_text) or "未记录原因",
                "analysis_basis": basis,
                "analysis_basis_label": ANALYSIS_BASIS_LABELS.get(basis, basis or "未记录"),
                "elapsed_seconds": diagnostic.get("elapsed_seconds"),
                "from_cache": bool(diagnostic.get("from_cache")),
                "response_chars": diagnostic.get("response_chars"),
                "missing_fields": [str(x) for x in (diagnostic.get("missing_fields") or []) if str(x).strip()],
                "media_errors": media_errors,
                "model_route": str(model_call.get("route") or "").strip(),
                "model_provider": str(model_call.get("provider") or "").strip(),
            }
        )
    items.sort(key=lambda x: str(x.get("time") or ""), reverse=True)

    return {
        "range": key,
        "label": RANGE_LABELS.get(key, key),
        "start": start.strftime("%Y-%m-%d %H:%M"),
        "end": end.strftime("%Y-%m-%d %H:%M"),
        "count": len(raw),
        "shown": min(len(items), limit),
        "by_stage": [
            {"stage": stage, "label": _label(FAILURE_STAGE_LABELS, stage), "count": count}
            for stage, count in sorted(stage_counter.items(), key=lambda kv: -kv[1])
        ],
        "top_reason": [
            {"reason": reason, "label": _label(FAILURE_REASON_LABELS, reason) or "未记录原因", "count": count}
            for reason, count in sorted(reason_counter.items(), key=lambda kv: -kv[1])[:5]
        ],
        "items": items[:limit],
    }
