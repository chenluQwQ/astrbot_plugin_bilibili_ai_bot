"""BiliBot 管理页的后端：页面在 ``pages/bilibot/index.html``，这里注册它调的接口。

每页一个模块；共用的回包、读配置在 ``_common``。插件只经 ``BotApp``（``core/app.py``）用，
账号凭据只在登录那几个接口里出现。"""
from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context
from ..app import app_of
from ._common import Handler, PLUGIN_NAME
from .account import handle_account_info, handle_account_logout, handle_qr_generate, handle_qr_poll
from .config_page import handle_get_config, handle_get_config_schema, handle_save_config
from .extension_pages import (
    handle_extension_action, handle_extension_page, handle_extension_refresh,
    handle_extensions_list,
)
from .interest import handle_get_interest_status
from .memory import (
    handle_cache_purge, handle_cache_stats, handle_memory_cleanup_start,
    handle_memory_cleanup_status, handle_memory_purge, handle_memory_stats,
)
from .profiles import handle_get_profiles
from .schedule import (
    handle_get_schedule, handle_get_schedule_stats, handle_schedule_override,
    handle_schedule_regenerate,
)
from .status import handle_get_persona_state, handle_get_stats, handle_security_stats
from .today import handle_delete_comment, handle_get_today
from .tools_page import handle_available_tools


def _bind(plugin: Any, handler: Handler):
    async def bound_handler():
        return await handler(plugin)

    bound_handler.__name__ = f"bilibot_{handler.__name__}"
    return bound_handler


def register_webui(plugin_instance: Any, context: Context):
    app = app_of(plugin_instance)
    if app.extension_dispatcher is None:
        from ..extensions import ExtensionDispatcher, ExtensionRegistry

        async def reject_extension_write(**_kwargs: Any):
            raise PermissionError(
                "Bilibili upload and publish are not enabled by Extension API v1"
            )

        registry = ExtensionRegistry(context, plugin_instance, reject_extension_write)
        app.extension_registry = registry
        app.extension_dispatcher = ExtensionDispatcher(registry)

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
        ("memory/cleanup", "GET", handle_memory_cleanup_status, "Irrelevant memory cleanup status"),
        ("memory/cleanup", "POST", handle_memory_cleanup_start, "Start irrelevant memory cleanup"),
        ("cache/stats", "GET", handle_cache_stats, "BiliBot cache usage"),
        ("cache/purge", "POST", handle_cache_purge, "Purge BiliBot disposable cache"),
        ("profiles", "GET", handle_get_profiles, "BiliBot relationship profiles"),
        ("schedule/today", "GET", handle_get_schedule, "BiliBot daily schedule"),
        ("schedule/stats", "GET", handle_get_schedule_stats, "BiliBot scheduler statistics"),
        ("schedule/regenerate", "POST", handle_schedule_regenerate, "Regenerate today's schedule"),
        ("schedule/override", "POST", handle_schedule_override, "Save edited today's schedule"),
        ("today", "GET", handle_get_today, "What BiliBot did on one day"),
        ("today/comment/delete", "POST", handle_delete_comment, "Delete one comment BiliBot posted"),
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
