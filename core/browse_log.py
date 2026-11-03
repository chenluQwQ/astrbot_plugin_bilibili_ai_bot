"""浏览日志：把看片、主动评论、评论区回复整理成控制台列表数据。

只读取现有日志文件，不写任何状态：
- ``watch_log.json``      每个看过（含分析失败）的视频
- ``proactive_log.json``  看片后留下的公开评论
- ``reply_log.json``      评论区回复 / B站私信回复
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import PROACTIVE_LOG_FILE, REPLY_LOG_FILE, WATCH_LOG_FILE

RANGE_LABELS = {"today": "今日", "yesterday": "昨日", "7d": "近7天"}
RANGE_ALIASES = {
    "today": "today",
    "今日": "today",
    "yesterday": "yesterday",
    "昨日": "yesterday",
    "昨天": "yesterday",
    "7d": "7d",
    "week": "7d",
    "近7天": "7d",
    "过去7天": "7d",
}

BILI_VIDEO_URL = "https://www.bilibili.com/video/{bvid}"


def _parse_time(value) -> datetime | None:
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


def normalize_range(value) -> str:
    key = str(value or "").strip()
    return RANGE_ALIASES.get(key, "today")


def _load(bot, path) -> list:
    try:
        data = bot._load_json(path, [])
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _in_range(value, start: datetime, end: datetime) -> bool:
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
    """判定一条看片记录是否属于「没真正进入分析流程」。

    ``evaluation_status == failed`` 是显式标记；早期记录没有这个字段，
    用「没有正分且没有想法」兜底（注意 score 可能是 None，不能直接比大小）。
    """
    if str(rec.get("evaluation_status") or "").strip().lower() == "failed":
        return True
    return _score_value(rec) <= 0 and not str(rec.get("score_reason") or "").strip()



def _new_item(rec: dict) -> dict:
    bvid = str(rec.get("bvid") or "").strip()
    return {
        "time": str(rec.get("time") or "").strip(),
        "bvid": bvid,
        "url": _video_url(bvid),
        "title": str(rec.get("title") or "").strip() or (f"视频 {bvid}" if bvid else "未知视频"),
        "up_name": str(rec.get("up_name") or "").strip(),
        "tname": str(rec.get("tname") or "").strip(),
        "score": rec.get("score"),
        "mood": str(rec.get("mood") or "").strip(),
        "thought": str(rec.get("score_reason") or "").strip(),
        "review": str(rec.get("review") or "").strip(),
        "comment": "",
        "actions": list(rec.get("actions") or []),
        "source": str(rec.get("source") or "").strip(),
        "source_detail": str(rec.get("source_detail") or "").strip(),
        "status": str(rec.get("evaluation_status") or "").strip() or ("ok" if rec.get("score") else ""),
        "comments": [],
        "replies": [],
    }


def collect_browse_entries(bot, range_key: str = "today", limit: int = 300, include_failed: bool = False) -> dict:
    """按范围聚合浏览记录，返回 {range, label, counts, items}。

    评价失败的记录默认不返回：它们没有想法也没有评论，只在计数里体现。
    """
    key = normalize_range(range_key)
    start, end = range_bounds(key)
    limit = max(1, min(int(limit or 300), 1000))

    watch = [r for r in _load(bot, WATCH_LOG_FILE) if isinstance(r, dict) and _in_range(r.get("time"), start, end)]
    comments = [r for r in _load(bot, PROACTIVE_LOG_FILE) if isinstance(r, dict) and _in_range(r.get("time"), start, end)]
    replies = [r for r in _load(bot, REPLY_LOG_FILE) if isinstance(r, dict) and _in_range(r.get("time"), start, end)]

    titles = {}
    for rec in _load(bot, WATCH_LOG_FILE):
        if isinstance(rec, dict) and rec.get("bvid"):
            titles[str(rec["bvid"])] = str(rec.get("title") or "")

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

    entries = []
    for item_key in order:
        item = items[item_key]
        if not item["title"] and item["comments"]:
            item["title"] = str(item["comments"][0].get("title") or "未知视频")
        # 主动评论可能有多条，取最后一条作为主评论
        item["comment"] = str((item["comments"][-1].get("comment") if item["comments"] else "") or "").strip()
        item["comment_count"] = len(item["comments"])
        item["reply_count"] = len(item["replies"])
        try:
            score_value = float(item["score"]) if item["score"] not in (None, "") else 0.0
        except (TypeError, ValueError):
            score_value = 0.0
        # 没有评分、也没有任何想法/评论的记录，等同于没进入分析流程
        if str(item["status"]).lower() == "failed" or (score_value <= 0 and not item["thought"]):
            item["status"] = "failed"
        entries.append(item)

    entries.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
    failed_entries = [item for item in entries if item.get("status") == "failed"]
    if not include_failed:
        entries = [item for item in entries if item.get("status") != "failed"]
    hidden_failed = len(failed_entries)
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
            "failed": hidden_failed,
        },
        "items": entries,
    }


# ── 明细视图：评论区回复 / 评价失败 ─────────────────────────────

REPLY_CHANNEL_LABELS = {
    "comment": "评论区回复",
    "private": "B站私信",
    "live": "直播间弹幕",
    "qq_group": "QQ 群",
    "qq_private": "QQ 私信",
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


def _label(table: dict, raw) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    return table.get(text) or text


def _video_titles(bot) -> dict:
    titles: dict[str, str] = {}
    for rec in _load(bot, WATCH_LOG_FILE):
        if isinstance(rec, dict) and rec.get("bvid"):
            titles[str(rec["bvid"])] = str(rec.get("title") or "")
    return titles


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


def collect_reply_entries(bot, range_key: str = "today", limit: int = 500) -> dict:
    """评论区回复 / 私信回复明细。

    与浏览列表不同，这里不做视频去重：一条回复就是一行，按时间倒序。
    """
    key = normalize_range(range_key)
    start, end = range_bounds(key)
    limit = max(1, min(int(limit or 500), 2000))
    titles = _video_titles(bot)
    raw = [r for r in _load(bot, REPLY_LOG_FILE) if isinstance(r, dict) and _in_range(r.get("time"), start, end)]

    items = []
    for rec in raw:
        bvid = str(rec.get("bvid") or "").strip()
        channel = _reply_channel(rec)
        items.append(
            {
                "time": str(rec.get("time") or "").strip(),
                "bvid": bvid,
                "url": _video_url(bvid),
                "title": str(rec.get("title") or "").strip() or titles.get(bvid, ""),
                "username": str(rec.get("username") or "").strip() or "对方",
                "user_id": str(rec.get("mid") or rec.get("user_id") or "").strip(),
                "content": str(rec.get("content") or "").strip(),
                "reply": str(rec.get("reply") or "").strip(),
                "native_emotes": rec.get("native_emotes") if isinstance(rec.get("native_emotes"),list) else [],
                "score_delta": rec.get("score_delta"),
                "oid": str(rec.get("oid") or "").strip(),
                "rpid": str(rec.get("rpid") or "").strip(),
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


def collect_failure_entries(bot, range_key: str = "today", limit: int = 500) -> dict:
    """评价失败明细：紧凑字段 + 完整诊断（展开时才用）。"""
    key = normalize_range(range_key)
    start, end = range_bounds(key)
    limit = max(1, min(int(limit or 500), 2000))
    raw = [
        r
        for r in _load(bot, WATCH_LOG_FILE)
        if isinstance(r, dict) and _in_range(r.get("time"), start, end) and _is_failed_record(r)
    ]

    items = []
    stage_counter: dict[str, int] = {}
    reason_counter: dict[str, int] = {}
    for rec in raw:
        diagnostic = rec.get("diagnostic") if isinstance(rec.get("diagnostic"), dict) else {}
        stage = str(diagnostic.get("stage") or "").strip() or "unknown"
        reason = diagnostic.get("reason") or rec.get("evaluation_status") or ""
        reason_text = str(reason or "").strip()
        basis = str(diagnostic.get("analysis_basis") or rec.get("analysis_basis") or "").strip()
        media_errors = [
            {
                "stage": _label(FAILURE_STAGE_LABELS, err.get("stage")),
                "reason": str(err.get("reason") or "").strip(),
            }
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
                "content_analyzed": bool(rec.get("content_analyzed")),
                "discovery_mode": str(rec.get("discovery_mode") or "").strip(),
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
