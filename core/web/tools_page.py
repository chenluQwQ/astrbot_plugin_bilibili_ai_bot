"""工具页：AstrBot 里有哪些工具能给 BiliBot 用，各自是只读、写操作还是私密。"""
from __future__ import annotations

from typing import Any

from astrbot.api import logger
from ._common import _failure, _response
from ..app import app_of


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
    "bili_video_info": ("视频资料", "按 BV 号读取标题、简介和热评，不算看过"),
    "bili_bangumi_search": ("番剧搜索", "按名字搜索番剧，拿到 season_id"),
    "bili_memory": ("Bot 自己的记忆", "查 Bot 看过的视频、发过的动态、追的番；只给当前对话所在的域看得到的部分"),
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


def _tool_origin(plugin: Any, tool: Any) -> tuple[str, str]:
    module_path = str(getattr(tool, "handler_module_path", "") or "")
    manager = app_of(plugin).astrbot_context.get_llm_tool_manager()
    try:
        if manager.is_builtin_tool(tool.name):
            return "builtin", "AstrBot Core"
    except Exception:
        pass
    server_name = getattr(tool, "mcp_server_name", "")
    if server_name:
        return "mcp", str(server_name)
    try:
        for star in app_of(plugin).astrbot_context.get_all_stars():
            star_path = str(getattr(star, "module_path", "") or "")
            if star_path and (module_path == star_path or module_path.startswith(f"{star_path}.")):
                return "plugin", str(getattr(star, "display_name", None) or getattr(star, "name", "插件"))
    except Exception:
        pass
    return "unknown", module_path or "未识别来源"


async def handle_available_tools(plugin: Any):
    """Expose the real registry with an explicit BiliBot security category."""
    try:
        manager = app_of(plugin).astrbot_context.get_llm_tool_manager()
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
            layered = app_of(plugin).layered_runtime
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
                    "active": True, "compatible": True, "category": "bilibot_read_builtin",
                    "reason": "插件内置只读能力，B站私信回复时由插件自己执行，不经过 AstrBot 工具注册表",
                    "security_tier": "public_read",
                })
        result.sort(key=lambda item: (not item["compatible"], item["category"], item["origin_name"], item["label"]))
        return _response(result)
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] tools discovery failed: {exc}")
        return _failure(str(exc), 500)
