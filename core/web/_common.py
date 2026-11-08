"""各页共用：回包格式、读配置（含旧键兼容）、读文件出错给默认值、展示文本限长、筛今天的记录。"""
from __future__ import annotations

import inspect
import re
from datetime import datetime
from typing import Any, Awaitable, Callable

from astrbot.api.web import error_response, json_response
from ..settings import LEGACY_KEYS, schema_defaults


PLUGIN_NAME = "astrbot_plugin_bilibili_ai_bot"


AUTONOMOUS_MAX_COMPAT = {
    new_key: (legacy_key, schema_defaults()[new_key]) for new_key, legacy_key in LEGACY_KEYS.items()
}


AUTONOMOUS_RANGE_PAIRS = ()  # 旧下限字段仅兼容读取，不再参与行为计划。


Handler = Callable[[Any], Awaitable[Any]]


def _response(data: Any = None, message: str | None = None):
    payload: dict[str, Any] = {"status": "ok"}
    if data is not None:
        payload["data"] = data
    if message:
        payload["message"] = message
    return json_response(payload)


def _failure(message: str, status_code: int = 400):
    return error_response(message, status_code=status_code)


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


def _read(load: Callable[[], Any], default: Any) -> Any:
    try:
        value = load()
        return value if value is not None else default
    except Exception:
        return default


def _safe_display_text(value: Any, *, max_chars: int = 12000) -> str:
    """Limit control-panel text without interpreting user-influenced content."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text[:max(1, int(max_chars))]


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
