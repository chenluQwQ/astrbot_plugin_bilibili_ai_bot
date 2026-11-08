"""首页总览、人格状态、安全统计。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from astrbot.api import logger
from ..app import app_of
from ..lifecycle.now_doing import DOING_LABELS
from ._common import _config_value, _failure, _read, _response, _today_entries
from .schedule import _next_event, _schedule_events


async def handle_get_stats(plugin: Any):
    try:
        runtime = app_of(plugin).event_runtime
        runtime_stats = await runtime.snapshot() if runtime and hasattr(runtime, "snapshot") else {}
        layered = app_of(plugin).layered_runtime
        layered_stats = await layered.snapshot() if layered and layered.is_open else {"open": False}
        event_states = runtime_stats.get("event_states", {}) or {}
        stored_events = layered_stats.get("events", {}) or {}
        failures = runtime_stats.get("recent_failures", []) or []
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        failed_today = sum(1 for item in failures if float(item.get("at", 0) or 0) >= today_start)

        reply_log = _today_entries(_read(app_of(plugin).reply_log, []), "time", "timestamp", "created_at")
        comment_replies = sum(
            1
            for item in reply_log
            if item.get("channel") not in {"private", "live"}
        )
        private_replies = sum(1 for item in reply_log if item.get("channel") == "private")
        dynamic_posts = len(_today_entries(_read(app_of(plugin).dynamic_log, []), "time", "timestamp"))
        proactive_entries = _today_entries(_read(app_of(plugin).proactive_log, []), "time", "timestamp")
        security_entries = _today_entries(app_of(plugin).security_log.entries(), "time", "timestamp")
        memory_stats = app_of(plugin).memory_api.stats() if app_of(plugin).memory_api else {"total": await app_of(plugin).count_memories()}
        profiles = app_of(plugin).user_profiles.all()
        snapshot = app_of(plugin).schedule_snapshot()
        events, _ = _schedule_events(plugin, snapshot)
        next_item = _next_event(events)

        configured = bool(_config_value(plugin, "SESSDATA", ""))
        running = app_of(plugin).is_running()
        activity = max(0, min(100, int(_config_value(plugin, "AUTONOMOUS_ACTIVITY_LEVEL", 55))))
        activity_label = "低迷" if activity < 25 else "平稳" if activity < 50 else "活跃" if activity < 75 else "高能"
        warnings = []
        if not configured:
            warnings.append({"level": "warning", "title": "账号未连接", "detail": "扫码连接 B站账号后才能启动自动互动。"})
        elif not running:
            warnings.append({"level": "warning", "title": "后台任务未运行", "detail": "账号已配置，但主循环当前未运行。可重载插件或检查 Cookie 状态。"})
        if failed_today:
            warnings.append({"level": "danger", "title": "今日存在执行失败", "detail": f"运行时记录到 {failed_today} 次失败，请查看 AstrBot 日志。"})
        if not warnings:
            warnings.append({"level": "success", "title": "未发现重大问题", "detail": "账号、调度与运行时状态未出现需要立即处理的异常。"})

        proactive_max = int(_config_value(plugin, "PROACTIVE_DAILY_LIMIT", 0) or 0)
        if _config_value(plugin, "ENABLE_AUTONOMOUS_DAILY_PLAN", False):
            autonomous_limit = int(_config_value(plugin, "AUTONOMOUS_PROACTIVE_DAILY_MAX", proactive_max) or 0)
            if autonomous_limit > 0:
                proactive_max = min([value for value in (proactive_max, autonomous_limit) if value > 0], default=autonomous_limit)

        data = {
            "running": running,
            "account_connected": configured,
            "scheduler_healthy": running and failed_today == 0,
            "pending": max(
                int(event_states.get("pending", 0)) + int(event_states.get("processing", 0)),
                int(stored_events.get("pending", 0)) + int(stored_events.get("claimed", 0)),
            ),
            "failed_today": failed_today,
            "ignored_today": int(event_states.get("ignored", 0)),
            "comment_replies_today": comment_replies,
            "private_replies_today": private_replies,
            "filtered_today": len(security_entries),
            "dynamic_posts_today": dynamic_posts,
            "videos_today": _count_today(plugin, "watch"),
            "bangumi_today": _count_today(plugin, "bangumi"),
            "proactive_used": len(proactive_entries),
            "proactive_max": proactive_max,
            "memory_total": int(memory_stats.get("total", 0)),
            "profiles_total": len(profiles) if isinstance(profiles, dict) else 0,
            "next_action": f"{next_item['time']} {next_item['label']}" if next_item else "今日暂无待执行事件",
            "activity_level": activity,
            "activity_label": activity_label,
            "warnings": warnings,
            "runtime": runtime_stats,
            "layers": layered_stats,
        }
        return _response(data)
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] stats failed: {exc}")
        return _failure(str(exc), 500)


def _count_today(plugin: Any, kind: str) -> int:
    """今天这种活动日志有几条（总览角色卡用），读不到就当 0。"""
    log = getattr(app_of(plugin).activity_logs, kind, None)
    return len(_read(lambda: log.on_day(datetime.now().strftime("%Y-%m-%d")), [])) if log else 0


async def handle_get_persona_state(plugin: Any):
    try:
        # 心情读真正在用的那份（domain/mood.py）；精力就是配置里的活跃度，插件没有别的精力。
        mood, _ = app_of(plugin).today_mood()
        personality = {}
        activity = max(0, min(100, int(_config_value(plugin, "AUTONOMOUS_ACTIVITY_LEVEL", 55))))
        hour = datetime.now().hour
        sleep_start = int(_config_value(plugin, "SLEEP_START", 2))
        sleep_end = int(_config_value(plugin, "SLEEP_END", 8))
        sleeping = (sleep_start <= hour < sleep_end) if sleep_start <= sleep_end else (hour >= sleep_start or hour < sleep_end)
        return _response({
            "energy": activity,
            "mood": re.sub(r"^[^\w\u4e00-\u9fff]+\s*", "", str(mood)),
            "current_mode": "rest" if sleeping else ("social" if activity >= 75 else "active" if activity >= 45 else "casual"),
            "current_time_range": f"{sleep_start:02d}:00-{sleep_end:02d}:00" if sleeping else "当前活跃时段",
            "autonomous": bool(_config_value(plugin, "ENABLE_AUTONOMOUS_DAILY_PLAN", False)),
            "personality": personality,
            # 总览角色卡：现在在干嘛（只在内存里，没在忙就是空字符串）、睡没睡、几点起几点睡
            "doing": _read(lambda: app_of(plugin).now_doing.describe(DOING_LABELS), ""),
            "sleeping": sleeping,
            "sleep_start": sleep_start,
            "sleep_end": sleep_end,
        })
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] persona state failed: {exc}")
        return _failure(str(exc), 500)


async def handle_security_stats(plugin: Any):
    try:
        logs = app_of(plugin).security_log.entries()
        today = _today_entries(logs, "time", "timestamp")
        counts: dict[str, int] = {}
        for item in today:
            key = str(item.get("event_type") or item.get("type") or "other")
            counts[key] = counts.get(key, 0) + 1
        layered = app_of(plugin).layered_runtime
        layered_security = await layered.security_stats() if layered and layered.is_open else {"today_total": 0, "by_type": {}}
        for key, value in layered_security.get("by_type", {}).items():
            counts[key] = counts.get(key, 0) + int(value or 0)
        return _response({
            "today_total": len(today) + int(layered_security.get("today_total", 0)),
            "by_type": counts,
            "tool_isolation": bool(_config_value(plugin, "BILI_TOOL_ISOLATION_ENABLED", True)),
            "allowed_tools": _config_value(plugin, "BILI_TOOL_ALLOWLIST", []),
            "prompt_defense": bool(_config_value(plugin, "BILI_PROMPT_INJECTION_DEFENSE", True)),
            "memory_mode": _config_value(plugin, "MEMORY_ISOLATION_MODE", "isolated"),
            "layered": layered_security,
        })
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] security stats failed: {exc}")
        return _failure(str(exc), 500)
