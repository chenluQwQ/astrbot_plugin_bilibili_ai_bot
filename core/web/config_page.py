"""配置页：带下拉选项的 schema、读配置（不回凭据）、保存配置。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request
from ..app import app_of
from ..network import normalize_proxy_url
from ._common import (
    AUTONOMOUS_MAX_COMPAT, AUTONOMOUS_RANGE_PAIRS, _config_value, _failure, _response,
)
from .tools_page import BILI_READONLY_ADAPTERS


# 管理页合并掉的项：页面显示的 → 跟着写的
MERGED_KEYS = {
    "PROACTIVE_TIMES_COUNT": "AUTONOMOUS_PROACTIVE_DAILY_MAX",
    "DYNAMIC_DAILY_COUNT": "AUTONOMOUS_DYNAMIC_DAILY_MAX",
}


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "_conf_schema.json"


PROTECTED_CONFIG_KEYS = {"SESSDATA", "BILI_JCT", "DEDE_USER_ID", "REFRESH_TOKEN"}


def _load_schema(*, include_protected: bool = False) -> dict[str, dict[str, Any]]:
    with SCHEMA_PATH.open("r", encoding="utf-8") as file:
        schema = json.load(file)
    if include_protected:
        return schema
    return {key: value for key, value in schema.items() if key not in PROTECTED_CONFIG_KEYS}


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


def _provider_choices(plugin: Any) -> list[tuple[str, str]]:
    """Chat providers configured in AstrBot, as (id, label) pairs."""
    getter = getattr(app_of(plugin).astrbot_context, "get_all_providers", None)
    if not callable(getter):
        return []
    choices = []
    seen = set()
    for provider in getter() or []:
        try:
            meta = provider.meta()
        except Exception:
            continue
        provider_id = str(getattr(meta, "id", "") or "").strip()
        if not provider_id:
            continue
        # 多上游插件会把同名 provider 重复注册，下拉框按 id 去重；值相同不影响选择。
        if provider_id in seen:
            continue
        seen.add(provider_id)
        model = str(getattr(meta, "model", "") or "").strip()
        # 下拉只展示模型名；provider id 仍作为 option 的值保存，无需显示前缀。
        choices.append((provider_id, model or provider_id))
    return choices


def _schema_with_choices(plugin: Any) -> dict[str, dict[str, Any]]:
    """Fill AstrBot's dynamic `select_provider` fields with real dropdown options.

    Saving still validates against the static schema, so a provider removed from
    AstrBot later does not make the stored value unsavable.
    """
    schema = _load_schema()
    try:
        providers = _provider_choices(plugin)
    except Exception as exc:
        logger.warning(f"[BiliBot WebUI] provider list failed: {exc}")
        providers = []
    if not providers:
        return schema
    for field in schema.values():
        if field.get("_special") != "select_provider":
            continue
        field["options"] = [""] + [provider_id for provider_id, _ in providers]
        field["option_labels"] = {"": "跟随 AstrBot 默认", **dict(providers)}
    return schema


async def handle_get_config_schema(plugin: Any):
    try:
        return _response(_schema_with_choices(plugin))
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
                if key == "PROXY_URL":
                    updates[key] = normalize_proxy_url(updates[key])
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
        # 管理页合并掉的几项：页面只留一个，保存时把另一个写成同样的值，代码照旧取更严的那个
        for shown, hidden in MERGED_KEYS.items():
            if shown in updates:
                updates[hidden] = updates[shown]
        if "MEMORY_ISOLATION_MODE" in updates:
            updates["ENABLE_SAFE_CROSS_PLATFORM_MEMORY"] = updates["MEMORY_ISOLATION_MODE"] == "safe_share"
        # 「每个视频最多评论几条」藏起来了：打开主动评论或番剧评论时至少是 1，不然开了也评不了
        if updates.get("PROACTIVE_COMMENT") is True or updates.get("BANGUMI_COMMENT") is True:
            if int(_config_value(plugin, "PROACTIVE_COMMENT_COUNT", 1) or 0) < 1:
                updates["PROACTIVE_COMMENT_COUNT"] = 1
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
                app_of(plugin).forget_schedule_states()
                app_of(plugin).clear_day_schedules()
        logger.info(f"[BiliBot WebUI] saved {len(updates)} settings")
        message = f"已保存 {len(updates)} 项配置"
        if "PROXY_URL" in updates:
            message += "；请重载插件使所有网络连接应用新代理"
        return _response({"saved": list(updates)}, message)
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] config save failed: {exc}")
        return _failure(str(exc), 500)
