"""日程页：今天的安排、统计、重新生成，以及拖动改日程后保存。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request
from ..app import app_of
from ._common import _config_value, _failure, _read, _response


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
    plan = _read(app_of(plugin).daily_plan, {})
    events.sort(key=lambda item: item["time"])
    return events, plan if isinstance(plan, dict) else {}


def _next_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    now = datetime.now().strftime("%H:%M")
    return next((item for item in events if not item.get("triggered") and item.get("time", "") >= now), None)


async def handle_get_schedule(plugin: Any):
    try:
        await app_of(plugin).ensure_daily_plan()
        snapshot = app_of(plugin).schedule_snapshot()
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
        snapshot = app_of(plugin).schedule_snapshot()
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
        app_of(plugin).forget_schedule_states()
        app_of(plugin).clear_day_schedules()
        await app_of(plugin).ensure_daily_plan(force=True)
        snapshot = app_of(plugin).schedule_snapshot()
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
            parsed = app_of(plugin).parse_time(time_value)
            if parsed is None:
                return _failure(f"无效时间：{time_value}")
            minute = parsed[0] * 60 + parsed[1]
            if kind == "proactive":
                start_raw = str(event.get("start_time") or "")
                end_raw = str(event.get("end_time") or "")
                window = app_of(plugin).parse_window(f"{start_raw}-{end_raw}")
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
                window_minutes = [(start_minute + offset) % 1440 for offset in range(0, duration + 1, 15)]
                if any(not app_of(plugin).is_awake_minute(value) for value in window_minutes):
                    return _failure("主动浏览时间段不能进入休眠时间")
                windows.append({"start_time": window["start_time"], "end_time": window["end_time"], "scheduled_time": time_value, "trigger_policy": "once_in_window"})
                normalized["proactive_times"].append(time_value)
            elif kind in point_keys:
                if not app_of(plugin).is_awake_minute(minute):
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
            plan = _read(app_of(plugin).daily_plan, {})
            if not isinstance(plan, dict) or plan.get("date") != now:
                plan = {"date": now, "config_fingerprint": app_of(plugin).daily_plan_fingerprint()}
            plan.update({key: values for key, values in normalized.items() if key != "proactive_times"})
            plan["proactive_times"] = normalized["proactive_times"]
            plan["proactive_windows"] = windows
            plan["source"] = "manual"
            plan["edited_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            app_of(plugin).save_daily_plan(plan)
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

        app_of(plugin).set_proactive_windows(windows)
        for kind, webui_kind in (
            ("proactive", "proactive"), ("dynamic", "dynamic"), ("dynamic_watch", "dynamic_watch"),
            ("bangumi", "bangumi"), ("special_follow", "follow"),
        ):
            times = [app_of(plugin).parse_time(value) for value in normalized[f"{kind}_times"] if app_of(plugin).parse_time(value)]
            # 改一次日程，追番「今天查过更新」就重置（内存和文件一起）。
            app_of(plugin).override_day_schedule(
                kind, times, triggered_by_kind[webui_kind],
                update_checked=False if kind == "bangumi" else None,
            )
        snapshot = app_of(plugin).schedule_snapshot()
        event_list, plan = _schedule_events(plugin, snapshot)
        return _response({"date": now, "events": event_list, "autonomous_plan": plan}, "日程修改已保存")
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] schedule override failed: {exc}")
        return _failure(str(exc), 500)
