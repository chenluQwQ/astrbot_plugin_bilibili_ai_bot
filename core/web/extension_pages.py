"""扩展页：列出扩展、渲染某个扩展的页面、转发它的操作、重新发现。"""
from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.web import request
from ..app import app_of
from ._common import _failure, _response


async def handle_extensions_list(plugin: Any):
    """Discover extensions lazily; absence or failure never affects the base UI."""
    try:
        dispatcher = app_of(plugin).extension_dispatcher
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
        dispatcher = app_of(plugin).extension_dispatcher
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
        dispatcher = app_of(plugin).extension_dispatcher
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
        dispatcher = app_of(plugin).extension_dispatcher
        if dispatcher is None:
            return _response([])
        return _response(await dispatcher.list_extensions(), "扩展发现已刷新")
    except Exception as exc:
        logger.warning(f"[BiliBot Extensions] refresh failed: {exc}")
        return _response([], "扩展刷新失败，主插件功能不受影响")
