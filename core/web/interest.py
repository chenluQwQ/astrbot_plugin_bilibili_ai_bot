"""视频兴趣页：读偏好库，带 30 秒缓存；连续读失败就熔断一分钟，用上次读到的结果。"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger
from ..app import app_of
from ..domain.preferences import Preferences
from ._common import _response, _safe_display_text


WEB_INTEREST_CACHE_TTL_SECONDS = 30.0


WEB_INTEREST_BREAKER_THRESHOLD = 3


WEB_INTEREST_BREAKER_COOLDOWN_SECONDS = 60.0


WEB_INTEREST_DB_TIMEOUT_SECONDS = 1.5


def _interest_file_preferences(plugin: Any) -> list[dict[str, Any]]:
    try:
        current = Preferences(plugin).cached()
    except Exception:
        return []
    return [dict(item) for item in current[:20] if isinstance(item, dict)]


def _format_web_interest_payload(
    plugin: Any,
    lifecycle_items: list[dict[str, Any]],
    *,
    source: str,
    stale: bool = False,
) -> dict[str, Any]:
    report = app_of(plugin).interest_report(lifecycle_items)
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
    app = app_of(plugin)
    cached = app.web_interest_cache
    if isinstance(cached, tuple) and len(cached) == 2:
        cached_at, cached_payload = cached
        if now - float(cached_at or 0) < WEB_INTEREST_CACHE_TTL_SECONDS:
            result = dict(cached_payload)
            result["cached"] = True
            return result

    open_until = float(app.web_interest_circuit_open_until or 0.0)
    if open_until > now:
        last_good = app.web_interest_last_good
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
        app.web_interest_cache = (now, payload)
        return payload

    lifecycle_items: list[dict[str, Any]] = []
    source = "local_fallback"
    preferences = Preferences(plugin)
    try:
        if preferences.is_live():
            lifecycle_items = await asyncio.wait_for(
                preferences.current(limit=20), timeout=WEB_INTEREST_DB_TIMEOUT_SECONDS
            )
            source = "runtime"
        else:
            lifecycle_items = _interest_file_preferences(plugin)
        app.web_interest_failures = 0
        app.web_interest_circuit_open_until = 0.0
    except Exception as exc:
        failures = int(app.web_interest_failures or 0) + 1
        app.web_interest_failures = failures
        lifecycle_items = _interest_file_preferences(plugin)
        if failures >= WEB_INTEREST_BREAKER_THRESHOLD:
            app.web_interest_circuit_open_until = now + WEB_INTEREST_BREAKER_COOLDOWN_SECONDS
            logger.warning(
                "[BiliBot WebUI] 兴趣状态数据库连续读取失败，暂停读取60秒并使用本地副本"
            )
        else:
            logger.debug(f"[BiliBot WebUI] 兴趣状态读取失败，使用本地副本: {exc}")

    payload = _format_web_interest_payload(plugin, lifecycle_items, source=source)
    app.web_interest_cache = (now, payload)
    if source == "runtime":
        app.web_interest_last_good = dict(payload)
    return payload


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
