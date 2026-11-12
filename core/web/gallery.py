"""总览页的收藏图库：只读缩略图与容量，不暴露本地路径、选图向量和模型凭证。"""
from __future__ import annotations

from typing import Any

from astrbot.api import logger
from ..server_ext import gallery_workflow, image_gallery
from ._common import _failure, _response


async def handle_get_gallery(plugin: Any):
    try:
        state = image_gallery.status()
        # 只给最近 12 张缩略图，总览每次刷新都解码整个图库代价太大。
        return _response({
            "limit": state["limit"],
            "available": state["available"],
            "evicted": state["evicted"],
            "stale_days": image_gallery.STALE_AFTER_SECONDS // 86400,
            "low_weight": image_gallery.LOW_WEIGHT_SCORE,
            "items": gallery_workflow.preview(12),
        })
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] gallery failed: {exc}")
        return _failure(str(exc), 500)
