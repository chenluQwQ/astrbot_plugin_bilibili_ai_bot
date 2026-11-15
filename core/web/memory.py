"""记忆页和缓存：统计、清理过期、后台「清理无关记忆」、缓存用量和清理。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request
from ..app import app_of
from ._common import _config_value, _failure, _response


async def handle_memory_stats(plugin: Any):
    try:
        stats = app_of(plugin).memory_api.stats() if app_of(plugin).memory_api else {"total": await app_of(plugin).count_memories()}
        layered = app_of(plugin).layered_runtime
        layered_count = await layered.memories.total_count() if layered and layered.is_open else 0
        app = app_of(plugin)
        entry = app.memory_entry

        def kind(item):
            """记忆页按这个分卡片：评论、私信、总结、视频、番剧、动态、直播，其余算「其他」。"""
            memory_type = entry._normalize_memory_entry(item).get("memory_type", "chat")
            if memory_type == "user_summary" or entry._is_derived_memory(item):
                return "summary"
            if memory_type == "chat":
                return "private" if "private" in str(item.get("source", "")).lower() else "comment"
            return memory_type if memory_type in {"video", "bangumi", "dynamic", "live"} else "other"

        by_kind = {name: 0 for name in ("comment", "private", "summary", "video", "bangumi", "dynamic", "live", "other")}
        for item in await app.memories.find():
            by_kind[kind(item)] += 1
        data = {
            **stats,
            **by_kind,
            "self": await app.count_memories(user_id="self"),
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
        removed = int(await app_of(plugin).memory_consolidation().cleanup_aged()) if app_of(plugin).memory_consolidation() else 0
        layered = app_of(plugin).layered_runtime
        layered_removed = await layered.purge_expired() if layered and layered.is_open else {}
        total = removed + sum(int(value or 0) for value in layered_removed.values())
        return _response(
            {"removed": total, "legacy_removed": removed, "layered": layered_removed},
            f"已清理 {total} 条过期数据",
        )
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] memory purge failed: {exc}")
        return _failure(str(exc), 500)


async def _memory_cleanup_status(plugin: Any) -> dict[str, Any]:
    app = app_of(plugin)
    task = app.memory_cleanup_task
    status = dict(app.memory_cleanup_status or {})
    status["running"] = bool(task and not task.done())
    engine = app.memory_consolidation()
    if engine and not status["running"]:
        status["pending"] = len(await engine._cleanup_candidates())
        status["share_previews"] = len(await engine._share_preview_rpids())
    return status


async def _run_memory_cleanup(plugin: Any) -> None:
    started = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        result = await app_of(plugin).memory_consolidation().cleanup_irrelevant()
        app_of(plugin).memory_cleanup_status = {"started_at": started, "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "result": result}
        logger.info(f"[BiliBot] 无关记忆清理完成: {result}")
    except Exception as exc:
        app_of(plugin).memory_cleanup_status = {"started_at": started, "error": str(exc)}
        logger.exception(f"[BiliBot] 无关记忆清理失败: {exc}")


async def handle_memory_cleanup_status(plugin: Any):
    try:
        return _response(await _memory_cleanup_status(plugin))
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] memory cleanup status failed: {exc}")
        return _failure(str(exc), 500)


async def handle_memory_cleanup_start(plugin: Any):
    try:
        app = app_of(plugin)
        if not app.memory_consolidation():
            return _failure("记忆清算尚未初始化")
        task = app.memory_cleanup_task
        if task and not task.done():
            return _response(await _memory_cleanup_status(plugin), "清理正在进行")
        app.memory_cleanup_task = asyncio.create_task(_run_memory_cleanup(plugin))
        return _response(await _memory_cleanup_status(plugin), "已开始清理")
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] memory cleanup start failed: {exc}")
        return _failure(str(exc), 500)


async def handle_cache_stats(plugin: Any):
    try:
        return _response(app_of(plugin).cache_stats())
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] cache stats failed: {exc}")
        return _failure(str(exc), 500)


async def handle_cache_purge(plugin: Any):
    try:
        body = await request.json(default={})
        mode = str((body or {}).get("mode", "normal")).strip().lower()
        if mode not in {"normal", "deep"}:
            return _failure("未知清理模式")
        return _response(app_of(plugin).purge_cache(mode), "缓存清理完成")
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] cache purge failed: {exc}")
        return _failure(str(exc), 500)
