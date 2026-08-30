"""Authenticated Page bridge for the BiliBot management UI.

The page is discovered from ``pages/bilibot/index.html``. The bridge combines
the established JSON-backed feature state with the persistent layered runtime;
account credentials remain available only through the dedicated login routes.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.web import error_response, json_response, request

from .config import (
    AFFECTION_FILE,
    AUTONOMOUS_PLAN_FILE,
    BANGUMI_SCHEDULE_FILE,
    DYNAMIC_LOG_FILE,
    DYNAMIC_SCHEDULE_FILE,
    DYNAMIC_WATCH_SCHEDULE_FILE,
    PREFERENCE_STATE_FILE,
    PROACTIVE_LOG_FILE,
    REPLY_LOG_FILE,
    SCHEDULE_FILE,
    SECURITY_LOG_FILE,
    SPECIAL_FOLLOW_SCHEDULE_FILE,
    USER_PROFILE_FILE,
)

PLUGIN_NAME = "astrbot_plugin_bilibili_ai_bot"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"
PROTECTED_CONFIG_KEYS = {"SESSDATA", "BILI_JCT", "DEDE_USER_ID", "REFRESH_TOKEN"}

# Only these names are safe Bilibot-side read adapters.  The AstrBot registry
# also contains write tools, private-memory tools, built-ins, other plugins and
# MCP tools; those must never be presented as selectable Bilibili read tools.
BILI_READONLY_ADAPTERS = {
    "bili_up_info": ("UP 主信息", "读取公开 UP 主资料"),
    "get_up_info": ("UP 主信息", "读取公开 UP 主资料"),
    "bili_video_search": ("视频搜索", "查询公开 B站视频"),
    "search_bilibili": ("视频搜索", "查询公开 B站视频"),
    "bili_search_and_watch": ("搜索并观看", "搜索并分析公开视频，不执行互动写操作"),
    "watch_video": ("观看视频", "读取并分析指定 BV 号的公开视频"),
    "check_following_updates": ("关注更新查询", "B站端私信回复模型按需查看今天关注 UP 主的新动态与投稿"),
    "check_following_live": ("关注开播查询", "B站端私信回复模型按需查看关注列表中当前正在直播的 UP 主"),
    "get_bangumi_info": ("番剧详情", "按 season_id 读取番剧公开资料与最近剧集"),
    "get_bangumi_trending": ("番剧排行", "只读查看 B站番剧或国创热度排行"),
    "get_bangumi_timeline": ("新番时间表", "只读查看近期番剧更新日程"),
    "get_bangumi_updates": ("追番更新", "只读查看账号当前在追番剧的更新概况"),
    "web_search": ("联网搜索", "通过插件当前配置的只读搜索接口检索公开网页"),
}
BILI_WRITE_TOOLS = {
    "bili_action", "bili_block_user", "bili_parse_video", "watch_and_share_video_private",
}
BILI_PRIVATE_TOOLS = {
    "bili_recall", "recall_user", "recall_conversation", "recall_today", "recall_video",
    "recall_dynamic", "recall_bangumi",
}
BILI_COMPOSITE_TOOLS = {"bili_bangumi", "bili_watch_videos"}
AUTONOMOUS_MAX_COMPAT = {
    "AUTONOMOUS_REPLY_DAILY_MAX": ("AUTONOMOUS_REPLY_DAILY_LIMIT", 80),
    "AUTONOMOUS_PRIVATE_DAILY_MAX": ("AUTONOMOUS_PRIVATE_DAILY_LIMIT", 30),
    "AUTONOMOUS_DYNAMIC_DAILY_MAX": ("AUTONOMOUS_DYNAMIC_DAILY_LIMIT", 2),
    "AUTONOMOUS_PROACTIVE_DAILY_MAX": ("AUTONOMOUS_PROACTIVE_DAILY_LIMIT", 4),
}
AUTONOMOUS_RANGE_PAIRS = ()  # 旧下限字段仅兼容读取，不再参与行为计划。
Handler = Callable[[Any], Awaitable[Any]]

WEB_INTEREST_CACHE_TTL_SECONDS = 30.0
WEB_INTEREST_BREAKER_THRESHOLD = 3
WEB_INTEREST_BREAKER_COOLDOWN_SECONDS = 60.0
WEB_INTEREST_DB_TIMEOUT_SECONDS = 1.5


def _response(data: Any = None, message: str | None = None):
    payload: dict[str, Any] = {"status": "ok"}
    if data is not None:
        payload["data"] = data
    if message:
        payload["message"] = message
    return json_response(payload)


def _failure(message: str, status_code: int = 400):
    return error_response(message, status_code=status_code)


def _bind(plugin: Any, handler: Handler):
    async def bound_handler():
        return await handler(plugin)

    bound_handler.__name__ = f"bilibot_{handler.__name__}"
    return bound_handler


def register_webui(plugin_instance: Any, context: Context):
    if getattr(plugin_instance, "_bilibot_extension_dispatcher", None) is None:
        from .extensions import ExtensionDispatcher, ExtensionRegistry

        async def reject_extension_write(**_kwargs: Any):
            raise PermissionError(
                "Bilibili upload and publish are not enabled by Extension API v1"
            )

        registry = ExtensionRegistry(context, plugin_instance, reject_extension_write)
        plugin_instance._bilibot_extension_registry = registry
        plugin_instance._bilibot_extension_dispatcher = ExtensionDispatcher(registry)

    routes: list[tuple[str, str, Handler, str]] = [
        ("stats", "GET", handle_get_stats, "BiliBot monitoring overview"),
        ("persona/state", "GET", handle_get_persona_state, "BiliBot persona state"),
        ("interest/status", "GET", handle_get_interest_status, "BiliBot video interest status"),
        ("config/schema", "GET", handle_get_config_schema, "BiliBot configuration schema"),
        ("config", "GET", handle_get_config, "Read BiliBot configuration"),
        ("config", "POST", handle_save_config, "Save BiliBot configuration"),
        ("account/info", "GET", handle_account_info, "Bilibili account status"),
        ("account/logout", "POST", handle_account_logout, "Clear Bilibili credentials"),
        ("account/qr/generate", "GET", handle_qr_generate, "Generate Bilibili login QR"),
        ("account/qr/poll", "GET", handle_qr_poll, "Poll Bilibili login QR"),
        ("memory/stats", "GET", handle_memory_stats, "BiliBot memory statistics"),
        ("memory/purge", "POST", handle_memory_purge, "Purge aged BiliBot memories"),
        ("cache/stats", "GET", handle_cache_stats, "BiliBot cache usage"),
        ("cache/purge", "POST", handle_cache_purge, "Purge BiliBot disposable cache"),
        ("profiles", "GET", handle_get_profiles, "BiliBot relationship profiles"),
        ("schedule/today", "GET", handle_get_schedule, "BiliBot daily schedule"),
        ("schedule/stats", "GET", handle_get_schedule_stats, "BiliBot scheduler statistics"),
        ("schedule/regenerate", "POST", handle_schedule_regenerate, "Regenerate today's schedule"),
        ("schedule/override", "POST", handle_schedule_override, "Save edited today's schedule"),
        ("security/stats", "GET", handle_security_stats, "BiliBot security statistics"),
        ("tools/available", "GET", handle_available_tools, "Available AstrBot tools for BiliBot"),
        ("extensions", "GET", handle_extensions_list, "List isolated BiliBot extensions"),
        ("extensions/page", "GET", handle_extension_page, "Render one extension page schema"),
        ("extensions/action", "POST", handle_extension_action, "Dispatch one extension action"),
        ("extensions/refresh", "POST", handle_extension_refresh, "Refresh extension discovery"),
    ]
    for endpoint, method, handler, description in routes:
        route = f"/{PLUGIN_NAME}/{endpoint}"
        try:
            context.register_web_api(route, _bind(plugin_instance, handler), [method], description)
            logger.info(f"[BiliBot WebUI] registered {method} {route}")
        except Exception as exc:
            logger.error(f"[BiliBot WebUI] failed to register {method} {route}: {exc}")


def _load_schema(*, include_protected: bool = False) -> dict[str, dict[str, Any]]:
    with SCHEMA_PATH.open("r", encoding="utf-8") as file:
        schema = json.load(file)
    if include_protected:
        return schema
    return {key: value for key, value in schema.items() if key not in PROTECTED_CONFIG_KEYS}


def _config_value(plugin: Any, key: str, default: Any = None) -> Any:
    try:
        if key == "ENABLE_OWNER_RECOMMEND" and str(plugin.config.get("RECOMMEND_OWNER_DELIVERY", "")).lower() == "off":
            return False
        value = plugin.config.get(key, default)
        if key == "RECOMMEND_OWNER_DELIVERY" and str(value).lower() == "off":
            return "private_message"
        compat = AUTONOMOUS_MAX_COMPAT.get(key)
        if compat:
            legacy_key, schema_default = compat
            legacy_value = plugin.config.get(legacy_key, None)
            if legacy_value is not None and value == schema_default and legacy_value != schema_default:
                return legacy_value
        return value
    except Exception:
        data = getattr(plugin.config, "data", {})
        if isinstance(data, dict):
            value = data.get(key, default)
            compat = AUTONOMOUS_MAX_COMPAT.get(key)
            if compat and value == compat[1] and data.get(compat[0]) not in (None, compat[1]):
                return data[compat[0]]
            return value
        return default


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _load_json(plugin: Any, path: str, default: Any) -> Any:
    try:
        value = plugin._load_json(path, default)
        return value if value is not None else default
    except Exception:
        return default


def _safe_display_text(value: Any, *, max_chars: int = 12000) -> str:
    """Limit control-panel text without interpreting user-influenced content."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text[:max(1, int(max_chars))]


def _interest_file_preferences(plugin: Any) -> list[dict[str, Any]]:
    snapshot = _load_json(plugin, PREFERENCE_STATE_FILE, {})
    current = snapshot.get("current", []) if isinstance(snapshot, dict) else []
    if not isinstance(current, list):
        return []
    return [dict(item) for item in current[:20] if isinstance(item, dict)]


def _format_web_interest_payload(
    plugin: Any,
    lifecycle_items: list[dict[str, Any]],
    *,
    source: str,
    stale: bool = False,
) -> dict[str, Any]:
    formatter = getattr(plugin, "_format_interest_report", None)
    report = formatter(lifecycle_items=lifecycle_items) if callable(formatter) else "视频兴趣状态暂不可用"
    return {
        "report": _safe_display_text(report),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "cached": False,
        "stale": bool(stale),
        "read_only": True,
    }


async def _get_web_interest_payload(plugin: Any) -> dict[str, Any]:
    """Read the interest report with a small cache and a gentle DB breaker.

    This endpoint never invokes an LLM or an external API.  A broken/locked
    SQLite runtime therefore cannot stall the control panel or cause request
    amplification; the JSON lifecycle snapshot remains a read-only fallback.
    """
    now = time.monotonic()
    cached = getattr(plugin, "_web_interest_cache", None)
    if isinstance(cached, tuple) and len(cached) == 2:
        cached_at, cached_payload = cached
        if now - float(cached_at or 0) < WEB_INTEREST_CACHE_TTL_SECONDS:
            result = dict(cached_payload)
            result["cached"] = True
            return result

    open_until = float(getattr(plugin, "_web_interest_circuit_open_until", 0.0) or 0.0)
    if open_until > now:
        last_good = getattr(plugin, "_web_interest_last_good", None)
        if isinstance(last_good, dict):
            result = dict(last_good)
            result.update({"cached": True, "stale": True, "source": "stale_cache"})
            return result
        payload = _format_web_interest_payload(
            plugin,
            _interest_file_preferences(plugin),
            source="circuit_fallback",
            stale=True,
        )
        plugin._web_interest_cache = (now, payload)
        return payload

    lifecycle_items: list[dict[str, Any]] = []
    source = "local_fallback"
    layered = getattr(plugin, "layered_runtime", None)
    store = getattr(layered, "preferences", None)
    try:
        if store is not None and getattr(layered, "is_open", False):
            lifecycle_items = await asyncio.wait_for(
                store.current(limit=20), timeout=WEB_INTEREST_DB_TIMEOUT_SECONDS
            )
            source = "runtime"
        else:
            lifecycle_items = _interest_file_preferences(plugin)
        plugin._web_interest_failures = 0
        plugin._web_interest_circuit_open_until = 0.0
    except Exception as exc:
        failures = int(getattr(plugin, "_web_interest_failures", 0) or 0) + 1
        plugin._web_interest_failures = failures
        lifecycle_items = _interest_file_preferences(plugin)
        if failures >= WEB_INTEREST_BREAKER_THRESHOLD:
            plugin._web_interest_circuit_open_until = now + WEB_INTEREST_BREAKER_COOLDOWN_SECONDS
            logger.warning(
                "[BiliBot WebUI] 兴趣状态数据库连续读取失败，暂停读取60秒并使用本地副本"
            )
        else:
            logger.debug(f"[BiliBot WebUI] 兴趣状态读取失败，使用本地副本: {exc}")

    payload = _format_web_interest_payload(plugin, lifecycle_items, source=source)
    plugin._web_interest_cache = (now, payload)
    if source == "runtime":
        plugin._web_interest_last_good = dict(payload)
    return payload


def _today_entries(items: Any, *fields: str) -> list[dict[str, Any]]:
    today = datetime.now().strftime("%Y-%m-%d")
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        stamp = next((str(item.get(key) or "") for key in fields if item.get(key)), "")
        if stamp.startswith(today):
            result.append(item)
    return result


def _coerce_config_value(key: str, field: dict[str, Any], value: Any) -> Any:
    field_type = field.get("type", "string")
    if field_type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        result = value
    elif field_type == "int":
        if isinstance(value, bool):
            raise ValueError(f"{key} 必须是整数")
        result = int(value)
    elif field_type == "float":
        if isinstance(value, bool):
            raise ValueError(f"{key} 必须是数字")
        result = float(value)
    elif field_type == "list":
        if isinstance(value, str):
            result = [part.strip() for part in re.split(r"[,，\n]", value) if part.strip()]
        elif isinstance(value, list):
            result = [str(item).strip() for item in value if str(item).strip()]
        else:
            raise ValueError(f"{key} 必须是列表")
    elif field_type in {"string", "text"}:
        result = "" if value is None else str(value)
    else:
        result = value
    options = field.get("options")
    if options and result not in options:
        raise ValueError(f"{key} 的值不在允许范围内")
    if field_type in {"int", "float"}:
        if field.get("min") is not None and result < field["min"]:
            raise ValueError(f"{key} 不能小于 {field['min']}")
        if field.get("max") is not None and result > field["max"]:
            raise ValueError(f"{key} 不能大于 {field['max']}")
    return result


def _schedule_events(plugin: Any, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    definitions = tuple(item for item in (
        ("proactive_times", "主动浏览", "proactive", "在时间段内浏览视频并选择感兴趣的内容", bool(_config_value(plugin, "ENABLE_PROACTIVE", False))),
        ("dynamic_times", "发布动态", "dynamic", "根据今日状态发布一条动态", bool(_config_value(plugin, "ENABLE_DYNAMIC", False))),
        ("bangumi_times", "追番", "bangumi", "检查更新或观看番剧", bool(_config_value(plugin, "ENABLE_BANGUMI", False) and _config_value(plugin, "BANGUMI_PROACTIVE", False))),
        ("special_follow_times", "特别关注", "follow", "巡视特别关注用户的新内容", bool(_config_value(plugin, "SPECIAL_FOLLOW_ENABLED", False))),
        ("dynamic_watch_times", "查看关注动态", "dynamic_watch", "查看关注用户的新动态图文", bool(_config_value(plugin, "ENABLE_DYNAMIC_WATCH", False))),
    ) if item[4])
    events: list[dict[str, Any]] = []
    proactive_windows = snapshot.get("proactive_windows", []) or []
    for key, label, kind, description, _enabled in definitions:
        triggered = set(snapshot.get(key.replace("_times", "_triggered"), []))
        values = snapshot.get(key, []) or []
        for index, value in enumerate(values):
            item = {
                "time": str(value),
                "label": label,
                "kind": kind,
                "description": description,
                "triggered": str(value) in triggered,
            }
            if kind == "proactive" and index < len(proactive_windows):
                window = proactive_windows[index] if isinstance(proactive_windows[index], dict) else {}
                item.update({
                    "start_time": str(window.get("start_time") or ""),
                    "end_time": str(window.get("end_time") or ""),
                    "trigger_policy": str(window.get("trigger_policy") or "once_in_window"),
                })
            events.append(item)
    plan = _load_json(plugin, AUTONOMOUS_PLAN_FILE, {})
    events.sort(key=lambda item: item["time"])
    return events, plan if isinstance(plan, dict) else {}


def _next_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    now = datetime.now().strftime("%H:%M")
    return next((item for item in events if not item.get("triggered") and item.get("time", "") >= now), None)


async def handle_get_stats(plugin: Any):
    try:
        runtime = getattr(plugin, "event_runtime", None)
        runtime_stats = await runtime.snapshot() if runtime and hasattr(runtime, "snapshot") else {}
        layered = getattr(plugin, "layered_runtime", None)
        layered_stats = await layered.snapshot() if layered and layered.is_open else {"open": False}
        event_states = runtime_stats.get("event_states", {}) or {}
        stored_events = layered_stats.get("events", {}) or {}
        failures = runtime_stats.get("recent_failures", []) or []
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        failed_today = sum(1 for item in failures if float(item.get("at", 0) or 0) >= today_start)

        reply_log = _today_entries(_load_json(plugin, REPLY_LOG_FILE, []), "time", "timestamp", "created_at")
        comment_replies = sum(
            1
            for item in reply_log
            if item.get("channel") not in {"private", "live"}
        )
        private_replies = sum(1 for item in reply_log if item.get("channel") == "private")
        dynamic_posts = len(_today_entries(_load_json(plugin, DYNAMIC_LOG_FILE, []), "time", "timestamp"))
        proactive_entries = _today_entries(_load_json(plugin, PROACTIVE_LOG_FILE, []), "time", "timestamp")
        security_entries = _today_entries(_load_json(plugin, SECURITY_LOG_FILE, []), "time", "timestamp")
        memory_stats = plugin.memory_api.stats() if getattr(plugin, "memory_api", None) else {"total": len(getattr(plugin, "_memory", []))}
        profiles = _load_json(plugin, USER_PROFILE_FILE, {})
        snapshot = plugin._get_schedule_snapshot() if hasattr(plugin, "_get_schedule_snapshot") else {}
        events, _ = _schedule_events(plugin, snapshot)
        next_item = _next_event(events)

        configured = bool(_config_value(plugin, "SESSDATA", ""))
        running = bool(getattr(plugin, "_running", False))
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


async def handle_get_persona_state(plugin: Any):
    try:
        layered = getattr(plugin, "layered_runtime", None)
        if layered and layered.is_open:
            state = await layered.persona.snapshot()
            segment = await layered.persona.current_segment()
            return _response({
                "energy": round(float(state.energy) * 100),
                "mood": state.mood,
                "current_mode": segment.activity if segment else state.phase,
                "current_time_range": (
                    f"{segment.start_min // 60:02d}:{segment.start_min % 60:02d}-"
                    f"{segment.end_min // 60:02d}:{segment.end_min % 60:02d}"
                    if segment else "当前未安排时段"
                ),
                "autonomous": bool(_config_value(plugin, "ENABLE_AUTONOMOUS_DAILY_PLAN", False)),
                "personality": {
                    "social": round(float(state.social) * 100),
                    "note": state.note,
                },
            })
        mood, _ = plugin._get_today_mood() if hasattr(plugin, "_get_today_mood") else ("平静", "")
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
        })
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] persona state failed: {exc}")
        return _failure(str(exc), 500)


async def handle_get_interest_status(plugin: Any):
    """Return a bounded, read-only view of the Bot's learned video interests."""
    try:
        return _response(await _get_web_interest_payload(plugin))
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] interest status failed: {exc}")
        return _response({
            "report": "视频兴趣状态暂时不可用，主动看片与回复功能不受影响。",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "safe_fallback",
            "cached": False,
            "stale": True,
            "read_only": True,
        })


async def handle_memory_stats(plugin: Any):
    try:
        stats = plugin.memory_api.stats() if getattr(plugin, "memory_api", None) else {"total": len(getattr(plugin, "_memory", []))}
        layered = getattr(plugin, "layered_runtime", None)
        layered_count = await layered.memories.total_count() if layered and layered.is_open else 0
        data = {
            **stats,
            "comment": int(stats.get("type_chat", 0)),
            "private": sum(1 for item in getattr(plugin, "_memory", []) if str(item.get("source", "")).startswith("private")),
            "self": sum(1 for item in getattr(plugin, "_memory", []) if str(item.get("user_id", "")) == "self"),
            "isolation_mode": _config_value(plugin, "MEMORY_ISOLATION_MODE", "isolated"),
            "safe_share": bool(_config_value(plugin, "ENABLE_SAFE_CROSS_PLATFORM_MEMORY", False)),
            "layered_total": layered_count,
        }
        return _response(data)
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] memory stats failed: {exc}")
        return _failure(str(exc), 500)


async def handle_memory_purge(plugin: Any):
    try:
        removed = int(await plugin._consolidation.cleanup_aged()) if getattr(plugin, "_consolidation", None) else 0
        layered = getattr(plugin, "layered_runtime", None)
        layered_removed = await layered.purge_expired() if layered and layered.is_open else {}
        total = removed + sum(int(value or 0) for value in layered_removed.values())
        return _response(
            {"removed": total, "legacy_removed": removed, "layered": layered_removed},
            f"已清理 {total} 条过期数据",
        )
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] memory purge failed: {exc}")
        return _failure(str(exc), 500)


async def handle_cache_stats(plugin: Any):
    try:
        return _response(plugin._cache_stats())
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] cache stats failed: {exc}")
        return _failure(str(exc), 500)


async def handle_cache_purge(plugin: Any):
    try:
        body = await request.json(default={})
        mode = str((body or {}).get("mode", "normal")).strip().lower()
        if mode not in {"normal", "deep"}:
            return _failure("未知清理模式")
        return _response(plugin._purge_cache(mode), "缓存清理完成")
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] cache purge failed: {exc}")
        return _failure(str(exc), 500)


async def handle_get_profiles(plugin: Any):
    try:
        profiles = _load_json(plugin, USER_PROFILE_FILE, {})
        affection = getattr(plugin, "_affection", {}) or _load_json(plugin, AFFECTION_FILE, {})
        data = []
        for uid, raw in (profiles.items() if isinstance(profiles, dict) else []):
            profile = plugin._normalize_user_profile(raw) if hasattr(plugin, "_normalize_user_profile") else dict(raw or {})
            live = profile.get("live", {}) if isinstance(profile.get("live"), dict) else {}
            refs = profile.get("video_refs", []) if isinstance(profile.get("video_refs"), list) else []
            last_values = [str(item.get("time", "")) for item in refs if isinstance(item, dict) and item.get("time")]
            if live.get("last_seen"):
                last_values.append(str(live["last_seen"]))
            score = int(affection.get(str(uid), 0) or 0)
            data.append({
                "user_id": str(uid),
                "name": profile.get("username") or f"UID {uid}",
                "affection": score,
                "relationship": plugin._get_level(score, uid) if hasattr(plugin, "_get_level") else "unknown",
                "impression": profile.get("impression", ""),
                "tags": profile.get("tags", [])[-6:],
                "facts_count": len(profile.get("facts", []) or []),
                "video_refs_count": len(refs),
                "last_interaction": max(last_values, default=""),
            })
        known = {item["user_id"] for item in data}
        for uid, score in affection.items() if isinstance(affection, dict) else []:
            if str(uid) not in known:
                data.append({"user_id": str(uid), "name": f"UID {uid}", "affection": int(score or 0), "relationship": plugin._get_level(score, uid) if hasattr(plugin, "_get_level") else "unknown", "impression": "", "tags": [], "facts_count": 0, "video_refs_count": 0, "last_interaction": ""})
        data.sort(key=lambda item: (item["affection"], item["last_interaction"]), reverse=True)
        return _response(data[:50])
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] profiles failed: {exc}")
        return _failure(str(exc), 500)


async def handle_get_schedule(plugin: Any):
    try:
        if hasattr(plugin, "_ensure_autonomous_daily_plan"):
            await plugin._ensure_autonomous_daily_plan()
        snapshot = plugin._get_schedule_snapshot() if hasattr(plugin, "_get_schedule_snapshot") else {}
        events, autonomous_plan = _schedule_events(plugin, snapshot)
        return _response({
            "date": snapshot.get("date", datetime.now().strftime("%Y-%m-%d")),
            "events": events,
            "sleep_start": int(_config_value(plugin, "SLEEP_START", 2)),
            "sleep_end": int(_config_value(plugin, "SLEEP_END", 8)),
            "activity_level": int(_config_value(plugin, "AUTONOMOUS_ACTIVITY_LEVEL", 55)),
            "autonomous_enabled": bool(_config_value(plugin, "ENABLE_AUTONOMOUS_DAILY_PLAN", False)),
            "autonomous_plan": autonomous_plan,
        })
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] schedule failed: {exc}")
        return _failure(str(exc), 500)


async def handle_get_schedule_stats(plugin: Any):
    try:
        snapshot = plugin._get_schedule_snapshot() if hasattr(plugin, "_get_schedule_snapshot") else {}
        events, _ = _schedule_events(plugin, snapshot)
        completed = sum(1 for event in events if event.get("triggered"))
        next_item = _next_event(events)
        return _response({
            "total": len(events),
            "completed": completed,
            "remaining": max(0, len(events) - completed),
            "next": next_item,
            "minimum_gap_minutes": int(_config_value(plugin, "AUTONOMOUS_MIN_ACTION_GAP_MINUTES", 45)),
        })
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] schedule stats failed: {exc}")
        return _failure(str(exc), 500)


async def handle_schedule_regenerate(plugin: Any):
    try:
        for path in (SCHEDULE_FILE, DYNAMIC_SCHEDULE_FILE, DYNAMIC_WATCH_SCHEDULE_FILE, BANGUMI_SCHEDULE_FILE, SPECIAL_FOLLOW_SCHEDULE_FILE, AUTONOMOUS_PLAN_FILE):
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
        plugin._proactive_times, plugin._proactive_triggered = [], set()
        plugin._dynamic_times, plugin._dynamic_triggered = [], set()
        plugin._dynamic_watch_times, plugin._dynamic_watch_triggered = [], set()
        plugin._bangumi_times, plugin._bangumi_triggered, plugin._bangumi_update_checked = [], set(), False
        plugin._special_follow_times, plugin._special_follow_triggered = [], set()
        if hasattr(plugin, "_ensure_autonomous_daily_plan"):
            await plugin._ensure_autonomous_daily_plan(force=True)
        snapshot = plugin._get_schedule_snapshot()
        events, autonomous_plan = _schedule_events(plugin, snapshot)
        return _response({"date": snapshot.get("date"), "events": events, "autonomous_plan": autonomous_plan}, "今日计划已重新生成")
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] schedule regeneration failed: {exc}")
        return _failure(str(exc), 500)


async def handle_schedule_override(plugin: Any):
    """Persist a user's drag/resize edits without changing unrelated settings."""
    try:
        body = await request.json(default={})
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list):
            return _failure("日程修改必须提供 events 数组")
        now = datetime.now().strftime("%Y-%m-%d")
        min_gap = max(15, int(_config_value(plugin, "AUTONOMOUS_MIN_ACTION_GAP_MINUTES", 45)))
        point_keys = {
            "dynamic": "dynamic_times", "dynamic_watch": "dynamic_watch_times",
            "bangumi": "bangumi_times", "follow": "special_follow_times",
        }
        normalized: dict[str, list[str]] = {key: [] for key in ("proactive_times", "dynamic_times", "dynamic_watch_times", "bangumi_times", "special_follow_times")}
        windows: list[dict[str, str]] = []
        all_minutes: list[int] = []
        triggered_by_kind: dict[str, set[str]] = {kind: set() for kind in ("proactive", "dynamic", "dynamic_watch", "bangumi", "follow")}
        for event in events:
            if not isinstance(event, dict):
                return _failure("事件格式无效")
            kind = str(event.get("kind") or "")
            time_value = str(event.get("time") or "")
            parsed = plugin._parse_time_value(time_value) if hasattr(plugin, "_parse_time_value") else None
            if parsed is None:
                return _failure(f"无效时间：{time_value}")
            minute = parsed[0] * 60 + parsed[1]
            if kind == "proactive":
                start_raw = str(event.get("start_time") or "")
                end_raw = str(event.get("end_time") or "")
                window = plugin._parse_window_value(f"{start_raw}-{end_raw}") if hasattr(plugin, "_parse_window_value") else None
                if not window:
                    return _failure("主动浏览必须提供有效的 start_time 与 end_time")
                start_minute = window["start_minute"]
                end_minute = window["end_minute"]
                in_window = (start_minute <= minute <= end_minute) if start_minute < end_minute else (minute >= start_minute or minute <= end_minute)
                if not in_window:
                    return _failure("主动浏览的触发时刻必须位于时间段内")
                # Every saved schedule event must stay awake. Check the trigger
                # and each quarter-hour in the window so a drag cannot straddle
                # the configured sleep interval.
                duration = window["duration_minutes"]
                if hasattr(plugin, "_is_awake_minute"):
                    window_minutes = [(start_minute + offset) % 1440 for offset in range(0, duration + 1, 15)]
                    if any(not plugin._is_awake_minute(value) for value in window_minutes):
                        return _failure("主动浏览时间段不能进入休眠时间")
                windows.append({"start_time": window["start_time"], "end_time": window["end_time"], "scheduled_time": time_value, "trigger_policy": "once_in_window"})
                normalized["proactive_times"].append(time_value)
            elif kind in point_keys:
                if hasattr(plugin, "_is_awake_minute") and not plugin._is_awake_minute(minute):
                    return _failure("事件时刻不能安排在休眠时间")
                normalized[point_keys[kind]].append(time_value)
            else:
                return _failure(f"不支持修改的事件类型：{kind}")
            if event.get("triggered") and kind in triggered_by_kind:
                triggered_by_kind[kind].add(time_value)
            all_minutes.append(minute)
        if any(b - a < min_gap for a, b in zip(sorted(all_minutes), sorted(all_minutes)[1:])):
            return _failure(f"相邻事件至少需要间隔 {min_gap} 分钟")

        autonomous = bool(_config_value(plugin, "ENABLE_AUTONOMOUS_DAILY_PLAN", False))
        if autonomous:
            plan = _load_json(plugin, AUTONOMOUS_PLAN_FILE, {})
            if not isinstance(plan, dict) or plan.get("date") != now:
                plan = {"date": now, "config_fingerprint": plugin._autonomous_config_fingerprint() if hasattr(plugin, "_autonomous_config_fingerprint") else ""}
            plan.update({key: values for key, values in normalized.items() if key != "proactive_times"})
            plan["proactive_times"] = normalized["proactive_times"]
            plan["proactive_windows"] = windows
            plan["source"] = "manual"
            plan["edited_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            plugin._save_json(AUTONOMOUS_PLAN_FILE, plan)
        else:
            updates = {
                "FIXED_PROACTIVE_WINDOWS": [f"{item['start_time']}-{item['end_time']}" for item in windows],
                "FIXED_DYNAMIC_TIMES": normalized["dynamic_times"],
                "FIXED_DYNAMIC_WATCH_TIMES": normalized["dynamic_watch_times"],
                "FIXED_BANGUMI_TIMES": normalized["bangumi_times"],
                "FIXED_SPECIAL_FOLLOW_TIMES": normalized["special_follow_times"],
            }
            for key, value in updates.items():
                plugin.config[key] = value
            plugin.config.save_config()

        plugin._proactive_windows = windows
        plugin._proactive_times = [plugin._parse_time_value(value) for value in normalized["proactive_times"] if plugin._parse_time_value(value)]
        plugin._dynamic_times = [plugin._parse_time_value(value) for value in normalized["dynamic_times"] if plugin._parse_time_value(value)]
        plugin._dynamic_watch_times = [plugin._parse_time_value(value) for value in normalized["dynamic_watch_times"] if plugin._parse_time_value(value)]
        plugin._bangumi_times = [plugin._parse_time_value(value) for value in normalized["bangumi_times"] if plugin._parse_time_value(value)]
        plugin._special_follow_times = [plugin._parse_time_value(value) for value in normalized["special_follow_times"] if plugin._parse_time_value(value)]
        plugin._proactive_triggered = set(triggered_by_kind["proactive"])
        plugin._dynamic_triggered = set(triggered_by_kind["dynamic"])
        plugin._dynamic_watch_triggered = set(triggered_by_kind["dynamic_watch"])
        plugin._bangumi_triggered = set(triggered_by_kind["bangumi"])
        plugin._special_follow_triggered = set(triggered_by_kind["follow"])
        plugin._save_schedule_state(plugin._proactive_times, plugin._proactive_triggered)
        plugin._save_dynamic_schedule_state(plugin._dynamic_times, plugin._dynamic_triggered)
        plugin._save_dynamic_watch_schedule_state(plugin._dynamic_watch_times, plugin._dynamic_watch_triggered)
        plugin._save_bangumi_schedule_state(plugin._bangumi_times, plugin._bangumi_triggered, False)
        plugin._save_special_follow_schedule_state(plugin._special_follow_times, plugin._special_follow_triggered)
        snapshot = plugin._get_schedule_snapshot()
        event_list, plan = _schedule_events(plugin, snapshot)
        return _response({"date": now, "events": event_list, "autonomous_plan": plan}, "日程修改已保存")
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] schedule override failed: {exc}")
        return _failure(str(exc), 500)


async def handle_security_stats(plugin: Any):
    try:
        logs = _load_json(plugin, SECURITY_LOG_FILE, [])
        today = _today_entries(logs, "time", "timestamp")
        counts: dict[str, int] = {}
        for item in today:
            key = str(item.get("event_type") or item.get("type") or "other")
            counts[key] = counts.get(key, 0) + 1
        layered = getattr(plugin, "layered_runtime", None)
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


def _tool_origin(plugin: Any, tool: Any) -> tuple[str, str]:
    module_path = str(getattr(tool, "handler_module_path", "") or "")
    manager = plugin.context.get_llm_tool_manager()
    try:
        if manager.is_builtin_tool(tool.name):
            return "builtin", "AstrBot Core"
    except Exception:
        pass
    server_name = getattr(tool, "mcp_server_name", "")
    if server_name:
        return "mcp", str(server_name)
    try:
        for star in plugin.context.get_all_stars():
            star_path = str(getattr(star, "module_path", "") or "")
            if star_path and (module_path == star_path or module_path.startswith(f"{star_path}.")):
                return "plugin", str(getattr(star, "display_name", None) or getattr(star, "name", "插件"))
    except Exception:
        pass
    return "unknown", module_path or "未识别来源"


async def handle_available_tools(plugin: Any):
    """Expose the real registry with an explicit BiliBot security category."""
    try:
        manager = plugin.context.get_llm_tool_manager()
        registered = list(getattr(manager, "func_list", []) or [])
        try:
            for tool in manager.iter_builtin_tools():
                if not any(getattr(item, "name", None) == getattr(tool, "name", None) for item in registered):
                    registered.append(tool)
        except Exception:
            pass
        result = []
        seen = set()
        for tool in registered:
            name = str(getattr(tool, "name", "") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            origin, origin_name = _tool_origin(plugin, tool)
            is_compatible = name in BILI_READONLY_ADAPTERS
            layered = getattr(plugin, "layered_runtime", None)
            spec = layered.tool_gate.get(name) if layered else None
            if is_compatible:
                category = "bilibot_read"
                reason = "已提供 B站只读安全适配器"
            elif name in BILI_WRITE_TOOLS:
                category = "bilibot_write"
                reason = "写操作工具，需要确认或能力票据，不属于只读白名单"
            elif name in BILI_PRIVATE_TOOLS:
                category = "bilibot_private"
                reason = "私域记忆工具，需要会话 scope，不属于公开 B站只读适配器"
            elif name in BILI_COMPOSITE_TOOLS:
                category = "bilibot_composite"
                reason = "复合工具可能包含观看、追番或主动行为，不作为只读工具开放"
            else:
                category = "other_registered"
                reason = "来自 AstrBot、其他插件或 MCP，未提供 B站只读适配器"
            result.append({
                "name": name,
                "label": BILI_READONLY_ADAPTERS.get(name, (name, ""))[0],
                "description": str(getattr(tool, "description", "") or BILI_READONLY_ADAPTERS.get(name, ("", "暂无说明"))[1]),
                "origin": origin,
                "origin_name": origin_name,
                "active": bool(getattr(tool, "active", True)),
                "compatible": is_compatible,
                "category": category,
                "reason": reason,
                "security_tier": spec.tier.value if spec else "unclassified",
            })
        for name, (label, description) in BILI_READONLY_ADAPTERS.items():
            if name not in seen:
                result.append({
                    "name": name, "label": label, "description": description,
                    "origin": "bilibot", "origin_name": "B站端私信回复工具",
                    "active": False, "compatible": False, "category": "bilibot_read_missing",
                    "reason": "已定义只读能力，但当前工具注册表没有可用的对应适配器",
                    "security_tier": "unavailable",
                })
        result.sort(key=lambda item: (not item["compatible"], item["category"], item["origin_name"], item["label"]))
        return _response(result)
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] tools discovery failed: {exc}")
        return _failure(str(exc), 500)


async def handle_get_config_schema(plugin: Any):
    try:
        return _response(_load_schema())
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] schema failed: {exc}")
        return _failure(str(exc), 500)


async def handle_get_config(plugin: Any):
    try:
        schema = _load_schema()
        return _response({key: _config_value(plugin, key, field.get("default")) for key, field in schema.items()})
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] config read failed: {exc}")
        return _failure(str(exc), 500)


async def handle_save_config(plugin: Any):
    try:
        body = await request.json(default={})
        if not isinstance(body, dict):
            return _failure("配置请求必须是 JSON 对象")
        schema = _load_schema()
        updates: dict[str, Any] = {}
        for key, value in body.items():
            if key in PROTECTED_CONFIG_KEYS:
                return _failure(f"{key} 只能通过账号连接页面更新")
            field = schema.get(key)
            if field is None:
                return _failure(f"未知配置项: {key}")
            try:
                updates[key] = _coerce_config_value(key, field, value)
            except (TypeError, ValueError) as exc:
                return _failure(str(exc))
        if updates.get("ENABLE_OWNER_RECOMMEND") is True and str(_config_value(plugin, "RECOMMEND_OWNER_DELIVERY", "private_message")).lower() == "off":
            updates.setdefault("RECOMMEND_OWNER_DELIVERY", "private_message")
        for min_key, max_key, label in AUTONOMOUS_RANGE_PAIRS:
            minimum = updates.get(min_key, _config_value(plugin, min_key, 0))
            maximum = updates.get(max_key, _config_value(plugin, max_key, 0))
            if int(minimum or 0) > int(maximum or 0):
                return _failure(f"{label}下限不能大于上限")
        if "BILI_TOOL_ALLOWLIST" in updates:
            updates["BILI_TOOL_ALLOWLIST"] = [
                name for name in updates["BILI_TOOL_ALLOWLIST"] if name in BILI_READONLY_ADAPTERS
            ]
        # Keep the deprecated scalar keys synchronized for older runtime code
        # and existing installations while the WebUI uses the new range fields.
        for max_key, (legacy_key, _default) in AUTONOMOUS_MAX_COMPAT.items():
            if max_key in updates:
                updates[legacy_key] = updates[max_key]
        for key, value in updates.items():
            plugin.config[key] = value
        if updates:
            plugin.config.save_config()
            schedule_config_keys = {
                "ENABLE_AUTONOMOUS_DAILY_PLAN", "AUTONOMOUS_PLAN_GENERATION_MODE",
                "AUTONOMOUS_PLAN_AFTER_SLEEP_MINUTES", "AUTONOMOUS_PLAN_GENERATION_TIME",
                "AUTONOMOUS_PLAN_RETRY_MINUTES", "AUTONOMOUS_PROACTIVE_WINDOW_MINUTES",
                "ENABLE_PROACTIVE", "PROACTIVE_TIMES_COUNT", "PROACTIVE_DAILY_LIMIT",
                "ENABLE_DYNAMIC", "DYNAMIC_TIMES_COUNT", "DYNAMIC_DAILY_COUNT",
                "ENABLE_DYNAMIC_WATCH", "DYNAMIC_WATCH_TIMES_COUNT", "DYNAMIC_WATCH_DAILY_LIMIT",
                "DYNAMIC_WATCH_SPECIAL_ONLY", "DYNAMIC_WATCH_INCLUDE_VIDEO_POSTS",
                "ENABLE_BANGUMI", "BANGUMI_PROACTIVE", "BANGUMI_DAILY_LIMIT",
                "SPECIAL_FOLLOW_ENABLED", "SPECIAL_FOLLOW_MODE", "SPECIAL_FOLLOW_TIMES_COUNT",
                "SPECIAL_FOLLOW_FIXED_TIMES", "SLEEP_START", "SLEEP_END",
                "FIXED_REPLY_DAILY_TARGET", "FIXED_PRIVATE_DAILY_TARGET",
                "FIXED_PROACTIVE_WINDOWS", "FIXED_PROACTIVE_TIMES", "FIXED_DYNAMIC_TIMES", "FIXED_BANGUMI_TIMES",
                "FIXED_SPECIAL_FOLLOW_TIMES", "FIXED_DYNAMIC_WATCH_TIMES",
            }
            schedule_keys = {key for key in updates if key.startswith("AUTONOMOUS_") or key in schedule_config_keys}
            if schedule_keys:
                for path in (AUTONOMOUS_PLAN_FILE, SCHEDULE_FILE, DYNAMIC_SCHEDULE_FILE, DYNAMIC_WATCH_SCHEDULE_FILE, BANGUMI_SCHEDULE_FILE, SPECIAL_FOLLOW_SCHEDULE_FILE):
                    try:
                        Path(path).unlink(missing_ok=True)
                    except Exception:
                        pass
                plugin._proactive_times, plugin._proactive_triggered = [], set()
                plugin._dynamic_times, plugin._dynamic_triggered = [], set()
                plugin._dynamic_watch_times, plugin._dynamic_watch_triggered = [], set()
                plugin._bangumi_times, plugin._bangumi_triggered, plugin._bangumi_update_checked = [], set(), False
                plugin._special_follow_times, plugin._special_follow_triggered = [], set()
        logger.info(f"[BiliBot WebUI] saved {len(updates)} settings")
        return _response({"saved": list(updates)}, f"已保存 {len(updates)} 项配置")
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] config save failed: {exc}")
        return _failure(str(exc), 500)


async def handle_account_info(plugin: Any):
    if not _config_value(plugin, "SESSDATA", ""):
        return _response({"logged_in": False, "configured": False, "reason": "尚未连接 B站账号"})
    try:
        nav_data, _ = await plugin._http_get("https://api.bilibili.com/x/web-interface/nav")
        if nav_data.get("code") != 0:
            return _response({"logged_in": False, "configured": True, "reason": nav_data.get("message", "Cookie 已失效")})
        user = nav_data.get("data", {})
        reply_log = _today_entries(_load_json(plugin, REPLY_LOG_FILE, []), "time", "timestamp", "created_at")
        memory_stats = plugin.memory_api.stats() if getattr(plugin, "memory_api", None) else {"total": len(getattr(plugin, "_memory", []))}
        return _response({
            "logged_in": True,
            "configured": True,
            "name": user.get("uname", "Bilibili 用户"),
            "uid": str(user.get("mid", _config_value(plugin, "DEDE_USER_ID", ""))),
            "level": int(user.get("level_info", {}).get("current_level", 0)),
            "avatar": user.get("face", ""),
            "reply_count": len(reply_log),
            "comment_reply_count": sum(1 for item in reply_log if item.get("channel") != "private"),
            "private_reply_count": sum(1 for item in reply_log if item.get("channel") == "private"),
            "affection_total": sum(int(value or 0) for value in getattr(plugin, "_affection", {}).values()),
            "memory_count": int(memory_stats.get("total", 0)),
            "running": bool(getattr(plugin, "_running", False)),
        })
    except Exception as exc:
        logger.warning(f"[BiliBot WebUI] account info unavailable: {exc}")
        return _response({"logged_in": False, "configured": True, "reason": f"账号状态检查失败：{exc}"})


async def handle_account_logout(plugin: Any):
    try:
        for key in PROTECTED_CONFIG_KEYS:
            plugin.config[key] = ""
        plugin.config.save_config()
        logger.info("[BiliBot WebUI] Bilibili account logged out")
        return _response(message="已退出登录并清空 Cookie")
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] logout failed: {exc}")
        return _failure(str(exc), 500)


async def handle_qr_generate(plugin: Any):
    try:
        import qrcode

        qr_url, key = await plugin._qr_login_generate()
        if not qr_url or not key:
            return _failure("B站未返回二维码，请检查网络连接后重试", 502)
        qr = qrcode.QRCode(version=None, box_size=9, border=2)
        qr.add_data(qr_url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="#1d2433", back_color="#ffffff")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return _response({"image": f"data:image/png;base64,{encoded}", "key": key, "expires_in": 180})
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] QR generation failed: {exc}")
        return _failure(f"二维码生成失败：{exc}", 500)


async def handle_qr_poll(plugin: Any):
    try:
        key = request.query.get("key", "").strip()
        if not key:
            return _failure("缺少二维码 key")
        code, message, cookies = await plugin._qr_login_poll(key)
        status_map = {0: "success", 86038: "expired", 86090: "scanned", 86101: "waiting"}
        status = status_map.get(code, "error")
        if status == "success":
            mappings = {"SESSDATA": "SESSDATA", "BILI_JCT": "bili_jct", "DEDE_USER_ID": "DedeUserID", "REFRESH_TOKEN": "REFRESH_TOKEN"}
            for config_key, cookie_key in mappings.items():
                if cookies.get(cookie_key):
                    plugin.config[config_key] = cookies[cookie_key]
            plugin.config.save_config()
            if not getattr(plugin, "_running", False):
                await plugin._start_bot()
            logger.info("[BiliBot WebUI] QR login succeeded")
        return _response({"status": status, "message": message})
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] QR polling failed: {exc}")
        return _failure(f"登录状态获取失败：{exc}", 500)


async def handle_extensions_list(plugin: Any):
    """Discover extensions lazily; absence or failure never affects the base UI."""
    try:
        dispatcher = getattr(plugin, "_bilibot_extension_dispatcher", None)
        if dispatcher is None:
            return _response([])
        return _response(await dispatcher.list_extensions())
    except Exception as exc:
        logger.warning(f"[BiliBot Extensions] list failed: {exc}")
        return _response([])


async def handle_extension_page(plugin: Any):
    try:
        extension_id = str(request.query.get("extension_id", "") or "").strip()
        page_id = str(request.query.get("page_id", "dashboard") or "dashboard").strip()
        if not extension_id:
            return _failure("缺少 extension_id")
        dispatcher = getattr(plugin, "_bilibot_extension_dispatcher", None)
        if dispatcher is None:
            return _failure("扩展 Host 尚未初始化", 503)
        result = await dispatcher.dispatch(
            extension_id,
            f"page:{page_id}",
            actor={"source": "bilibot-webui", "role": "admin"},
        )
        return _response(result)
    except KeyError as exc:
        return _failure(str(exc), 404)
    except Exception as exc:
        logger.warning(f"[BiliBot Extensions] page dispatch failed: {exc}")
        return _failure("扩展页面暂时不可用", 502)


async def handle_extension_action(plugin: Any):
    try:
        body = await request.json(default={})
        if not isinstance(body, dict):
            return _failure("扩展动作请求必须是 JSON 对象")
        extension_id = str(body.get("extension_id", "") or "").strip()
        action_id = str(body.get("action_id", "") or "").strip()
        payload = body.get("payload") or {}
        if not extension_id or not action_id or not isinstance(payload, dict):
            return _failure("extension_id、action_id 和对象 payload 均为必填")
        dispatcher = getattr(plugin, "_bilibot_extension_dispatcher", None)
        if dispatcher is None:
            return _failure("扩展 Host 尚未初始化", 503)
        result = await dispatcher.dispatch(
            extension_id,
            f"action:{action_id}",
            payload=payload,
            actor={"source": "bilibot-webui", "role": "admin"},
        )
        return _response(result)
    except KeyError as exc:
        return _failure(str(exc), 404)
    except Exception as exc:
        logger.warning(f"[BiliBot Extensions] action dispatch failed: {exc}")
        return _failure("扩展动作执行失败", 502)


async def handle_extension_refresh(plugin: Any):
    try:
        dispatcher = getattr(plugin, "_bilibot_extension_dispatcher", None)
        if dispatcher is None:
            return _response([])
        return _response(await dispatcher.list_extensions(), "扩展发现已刷新")
    except Exception as exc:
        logger.warning(f"[BiliBot Extensions] refresh failed: {exc}")
        return _response([], "扩展刷新失败，主插件功能不受影响")
