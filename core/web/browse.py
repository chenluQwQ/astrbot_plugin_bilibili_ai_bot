"""浏览日志的三个只读接口：列表 / 回复明细 / 评价失败明细。

老版（1.x browse_log）在 2.0.0 前端重构时被「今天」页顶掉，但按视频聚合看
「看了什么、当时的想法、留下的评论、回了谁」这个视图「今天」页给不了，
所以把接口和页面都移植回来，数据源换成 2.x 的 activity_logs（sqlite）。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.web import request

from ..app import app_of
from ..storage.browse_log import (
    collect_browse_entries,
    collect_failure_entries,
    collect_reply_entries,
)
from ._common import _failure, _response


def _query_range_limit(default_limit: int) -> tuple[str, int, bool]:
    range_key = str(request.query.get("range", "today") or "today").strip()
    try:
        limit = int(request.query.get("limit", default_limit) or default_limit)
    except (TypeError, ValueError):
        limit = default_limit
    include_failed = str(request.query.get("include_failed", "") or "").strip().lower() in {"1", "true", "yes", "on"}
    return range_key, limit, include_failed


async def handle_browse_log(plugin: Any):
    """浏览日志列表：今日 / 昨日 / 近7天，按视频聚合想法、评论与回复。"""
    try:
        range_key, limit, include_failed = _query_range_limit(300)
        return _response(collect_browse_entries(app_of(plugin), range_key, limit, include_failed))
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] browse log failed: {exc}")
        return _failure(str(exc), 500)


async def handle_browse_replies(plugin: Any):
    """评论区回复 / 私信回复明细：一条一行按时间倒序。"""
    try:
        range_key, limit, _ = _query_range_limit(500)
        return _response(collect_reply_entries(app_of(plugin), range_key, limit))
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] browse replies failed: {exc}")
        return _failure(str(exc), 500)


async def handle_browse_failed(plugin: Any):
    """评价失败明细 + 失败原因分布。"""
    try:
        range_key, limit, _ = _query_range_limit(500)
        return _response(collect_failure_entries(app_of(plugin), range_key, limit))
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] browse failed detail failed: {exc}")
        return _failure(str(exc), 500)
