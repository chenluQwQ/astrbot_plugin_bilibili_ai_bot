"""记忆类工具：用户画像、对话、今天做了什么、看过的视频、动态、番剧。模型只看得到合起来的 ``bili_recall``。"""
from datetime import datetime

from ..config import LEVEL_NAMES
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass
from ..chat_text import clip


def build(kit):
    app = kit.app
    bot_uid = kit.bot_uid
    owner_name = kit.owner_name
    owner_uid = kit.owner_uid
    plugin = kit.plugin

    @dataclass
    class RecallUserTool(FunctionTool[AstrAgentContext]):
        name: str = "recall_user"
        description: str = f"Look up a Bilibili user's profile, impression and affection. Your UID={bot_uid}, owner={owner_name}(UID:{owner_uid}). Call once per user per conversation."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "UID or username keyword"},
            },
            "required": ["user_id"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            user_id = str(kwargs.get("user_id", ""))
            if user_id in app.user_profiles or app.affection.known(user_id):
                p = app.user_profiles.raw(user_id)
                sc = app.affection.score(user_id)
                lv = app.relationship_level(sc, user_id)
                lines = [
                    f"[用户资料] UID:{user_id} 昵称:{p.get('username', '未知')}",
                    f"好感度:{sc}分 等级:{LEVEL_NAMES[lv]}",
                ]
                if p.get("impression"): lines.append(f"印象:{p['impression']}")
                if p.get("facts"): lines.append(f"已知信息:{'；'.join(p['facts'][-8:])}")
                user_mems = list(reversed(await app.find_memories(user_id=user_id, newest_first=True, limit=5)))
                if user_mems:
                    lines.append("最近交互:" + "；".join(clip(m.get('text', ''), 80) for m in user_mems))
                return "\n".join(lines)
            profiles = app.user_profiles.all()
            matches = [(uid, p) for uid, p in profiles.items() if user_id.lower() in (p.get("username", "") or "").lower()]
            if matches:
                lines = [f"找到{len(matches)}个匹配:"]
                for uid, p in matches[:5]:
                    sc = app.affection.score(uid)
                    lines.append(f"  UID:{uid} {p.get('username','')} {sc}分 {clip(p.get('impression',''), 30)}")
                return "\n".join(lines)
            return f"没有找到用户「{user_id}」的记录。"
    @dataclass
    class RecallConversationTool(FunctionTool[AstrAgentContext]):
        name: str = "recall_conversation"
        description: str = "Search chat memories by keyword. Call once, don't retry if empty."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "search keyword"},
                "user_id": {"type": "string", "description": "optional, filter by UID", "default": ""},
            },
            "required": ["keyword"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            keyword = kwargs.get("keyword", "")
            user_id = kwargs.get("user_id", "") or None
            results = await app.search_memories(keyword, limit=5, memory_types={"chat"}, user_id=user_id, score_threshold=0.5, reader_scope="admin")
            if not results:
                return f"没有找到与「{keyword}」相关的对话记忆。"
            return "对话记忆:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(results, 1))
    @dataclass
    class RecallTodayTool(FunctionTool[AstrAgentContext]):
        name: str = "recall_today"
        description: str = "查看今天/最近的所有活动记录（看视频、番剧、回复、动态），按时间列出。Use when user asks: 今天干了什么, 今天的记忆, 今天看了什么, 最近做了什么, what did you do today."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "日期YYYY-MM-DD, default today", "default": ""},
            },
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            target_date = kwargs.get("date", "") or datetime.now().strftime("%Y-%m-%d")
            return app.memory_api.activity_overview(target_date)
    @dataclass
    class RecallVideoTool(FunctionTool[AstrAgentContext]):
        name: str = "recall_video"
        description: str = "Search watched video memories. Call once, don't retry if empty."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "title, UP name, or topic"},
                "target": {"type": "string", "description": "optional: only memories about this BV id, UP UID, season ssID or followed anime title"},
            },
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return await app.recall_own_memory("video", kwargs.get("keyword", ""), kwargs.get("target", ""))
    @dataclass
    class RecallDynamicTool(FunctionTool[AstrAgentContext]):
        name: str = "recall_dynamic"
        description: str = "Search dynamic/post memories. Call once, don't retry if empty."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "keyword"},
                "target": {"type": "string", "description": "optional: only memories about this BV id, UP UID, season ssID or followed anime title"},
            },
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return await app.recall_own_memory("dynamic", kwargs.get("keyword", ""), kwargs.get("target", ""))
    @dataclass
    class RecallBangumiTool(FunctionTool[AstrAgentContext]):
        name: str = "recall_bangumi"
        description: str = "Search anime/bangumi watching memories and progress. Use when user asks about anime you've watched."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "anime title or topic"},
                "target": {"type": "string", "description": "optional: only memories about this BV id, UP UID, season ssID or followed anime title"},
            },
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return await app.recall_own_memory("bangumi", kwargs.get("keyword", ""), kwargs.get("target", ""))
    @dataclass
    class RecallBiliTool(FunctionTool[AstrAgentContext]):
        name: str = "bili_recall"
        description: str = (
            "Recall BiliBot memory. Select user profile, conversation, today's activity, "
            "watched video, dynamic/post, or bangumi memory. Call once per topic."
        )
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "memory_type": {
                    "type": "string",
                    "enum": ["user", "conversation", "today", "video", "dynamic", "bangumi"],
                    "description": "memory category",
                },
                "query": {
                    "type": "string",
                    "description": "UID/name for user, keyword for other memory types, or YYYY-MM-DD for today",
                    "default": "",
                },
                "user_id": {
                    "type": "string",
                    "description": "optional UID filter for conversation memory",
                    "default": "",
                },
                "target": {
                    "type": "string",
                    "description": "optional for video/dynamic/bangumi: only memories about this BV id, UP UID, season ssID or followed anime title",
                    "default": "",
                },
            },
            "required": ["memory_type"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            memory_type = str(kwargs.get("memory_type", "") or "").strip().lower()
            query = str(kwargs.get("query", "") or "").strip()
            if memory_type == "user":
                if not query:
                    return "请提供要查询的用户 UID 或昵称。"
                return await RecallUserTool().call(context, user_id=query)
            if memory_type == "conversation":
                if not query:
                    return "请提供对话记忆关键词。"
                return await RecallConversationTool().call(
                    context,
                    keyword=query,
                    user_id=str(kwargs.get("user_id", "") or ""),
                )
            if memory_type == "today":
                return await RecallTodayTool().call(context, date=query)
            if memory_type == "video":
                target = str(kwargs.get("target", "") or "").strip()
                if not query and not target:
                    return "请提供视频记忆关键词或 target。"
                return await RecallVideoTool().call(context, keyword=query, target=target)
            if memory_type == "dynamic":
                target = str(kwargs.get("target", "") or "").strip()
                if not query and not target:
                    return "请提供动态记忆关键词或 target。"
                return await RecallDynamicTool().call(context, keyword=query, target=target)
            if memory_type == "bangumi":
                target = str(kwargs.get("target", "") or "").strip()
                if not query and not target:
                    return "请提供番剧记忆关键词或 target。"
                return await RecallBangumiTool().call(context, keyword=query, target=target)
            return "不支持的记忆类型。"

    return RecallBiliTool,
