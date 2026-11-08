"""番剧工具：查详情、热榜、时间表、追番更新、看一集。模型只看得到合起来的 ``bili_bangumi``。"""
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass
from ..chat_text import clip


def build(kit, SearchBilibiliTool):
    app = kit.app
    plugin = kit.plugin

    @dataclass
    class GetBangumiInfoTool(FunctionTool[AstrAgentContext]):
        name: str = "get_bangumi_info"
        description: str = "Get detailed info about a Bilibili anime/bangumi by season_id. Use after search_bilibili(search_type=bangumi) to get full details."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "season_id": {"type": "integer", "description": "season_id from search result"},
            },
            "required": ["season_id"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            season_id = kwargs.get("season_id", 0)
            if not season_id:
                return "需要提供 season_id。"
            detail = await app.bangumi_detail(season_id=season_id)
            if not detail:
                return f"获取番剧详情失败（season_id={season_id}）。"
            lines = [
                f"《{detail['title']}》",
                f"评分:{detail['score']}（{detail['count']}人评） 地区:{detail['areas']} 类型:{detail['styles']}",
                f"集数:{detail['total_ep']} {detail['new_ep_desc']}",
                f"播放:{detail['stat_views']} 弹幕:{detail['stat_danmakus']} 追番:{detail['stat_favorites']}",
            ]
            if detail['evaluate']:
                lines.append(f"简评:{clip(detail['evaluate'], 150)}")
            if detail['episodes']:
                recent = detail['episodes'][-5:]
                ep_list = " / ".join(clip(ep['title'], 25) for ep in recent)
                lines.append(f"最近剧集: {ep_list}")
            if detail['link']:
                lines.append(detail['link'])
            return "\n".join(lines)
    @dataclass
    class GetBangumiTrendingTool(FunctionTool[AstrAgentContext]):
        name: str = "get_bangumi_trending"
        description: str = "Get trending/top anime rankings on Bilibili. season_type: 1=番剧(anime) 4=国创(chinese anime)."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "season_type": {"type": "integer", "description": "1=番剧 4=国创", "default": 1},
            },
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            season_type = kwargs.get("season_type", 1)
            type_name = {1: "番剧", 2: "电影", 3: "纪录片", 4: "国创"}.get(season_type, "番剧")
            results = await app.bangumi_trending(season_type=season_type)
            if not results:
                return f"获取{type_name}排行失败。"
            lines = [f"🏆 B站{type_name}热度排行:"]
            for i, b in enumerate(results[:10], 1):
                score_str = f" ⭐{b['score']}" if b['score'] else ""
                lines.append(f"  {i}. 《{b['title']}》{score_str} {b['new_ep_desc']}")
            return "\n".join(lines)
    @dataclass
    class GetBangumiTimelineTool(FunctionTool[AstrAgentContext]):
        name: str = "get_bangumi_timeline"
        description: str = "查看当季新番/新番时间表/本周更新的番剧。Use when user asks: 新番, 本季番剧, 最近有什么新番, 番剧时间表, new anime this season."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {},
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            results = await app.bangumi_timeline(day_before=2, day_after=3)
            if not results:
                return "获取新番时间表失败。"
            from collections import OrderedDict
            days = OrderedDict()
            weekday_names = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日", 0: "周日"}
            for item in results:
                date = item.get("date", "")
                dow = weekday_names.get(item.get("day_of_week", 0), "")
                key = f"{date}({dow})"
                if key not in days:
                    days[key] = []
                status = "✅" if item.get("published") else "🔜"
                ep_str = f"第{item['ep_index']}话" if item.get("ep_index") else ""
                days[key].append(f"  {status} 《{item['title']}》{ep_str}")
            lines = ["📅 新番时间表:"]
            for day_key, eps in days.items():
                lines.append(f"\n{day_key}:")
                lines.extend(eps[:10])
            return "\n".join(lines)
    @dataclass
    class GetBangumiUpdatesTool(FunctionTool[AstrAgentContext]):
        name: str = "get_bangumi_updates"
        description: str = "查看追番更新/最近番剧更新。Use when user asks: 追番更新了吗, 最近番剧更新, 我追的番有更新吗, bangumi updates."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {},
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            followed = await app.followed_bangumi(follow_status=2)
            if not followed:
                return "没有在追的番剧，或者获取追番列表失败。"
            mem = app.bangumi_memory()
            lines = [f"📺 追番列表（{len(followed)}部）:"]
            for f in followed:
                sid = str(f["season_id"])
                record = mem.get(sid)
                watched = len(record.get("episodes", [])) if record else 0
                watched_str = f" 已看{watched}集" if watched else " 未看"
                lines.append(f"  《{f['title']}》{f.get('new_ep_index', '')}{watched_str}")
            return "\n".join(lines)
    @dataclass
    class WatchBangumiTool(FunctionTool[AstrAgentContext]):
        name: str = "bili_watch_bangumi"
        description: str = "看番/追番/看动漫。当用户要求看番剧、追番、看动漫、看动画时调用此工具。Trigger watching anime/bangumi on Bilibili. Use when user mentions: 看番, 追番, 看动漫, 看动画, watch anime, watch bangumi."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "season_id": {"type": "integer", "description": "Optional: specific season_id. Omit to auto-pick.", "default": 0},
                "ep_id": {"type": "integer", "description": "Optional: specific ep_id to start from (a specific episode).", "default": 0},
            },
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            season_id = kwargs.get("season_id", 0) or None
            ep_id = kwargs.get("ep_id", 0) or None
            return await app.watch_bangumi_report(season_id=season_id, ep_id=ep_id)
    @dataclass
    class BiliBangumiTool(FunctionTool[AstrAgentContext]):
        name: str = "bili_bangumi"
        description: str = (
            "Search or inspect anime, view trending/timeline/followed updates, or watch an episode. "
            "Use the matching action instead of choosing among several bangumi tools."
        )
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "info", "trending", "timeline", "updates", "watch"],
                },
                "keyword": {"type": "string", "description": "title keyword for search", "default": ""},
                "season_id": {"type": "integer", "description": "season ID for info/watch", "default": 0},
                "ep_id": {"type": "integer", "description": "optional episode ID for watch", "default": 0},
                "season_type": {"type": "integer", "description": "1=番剧, 4=国创 for trending", "default": 1},
            },
            "required": ["action"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            action = str(kwargs.get("action", "") or "").strip().lower()
            if action == "search":
                keyword = str(kwargs.get("keyword", "") or "").strip()
                if not keyword:
                    return "请提供番剧搜索关键词。"
                return await SearchBilibiliTool().call(context, keyword=keyword, search_type="bangumi")
            if action == "info":
                return await GetBangumiInfoTool().call(context, season_id=kwargs.get("season_id", 0))
            if action == "trending":
                return await GetBangumiTrendingTool().call(context, season_type=kwargs.get("season_type", 1))
            if action == "timeline":
                return await GetBangumiTimelineTool().call(context)
            if action == "updates":
                return await GetBangumiUpdatesTool().call(context)
            if action == "watch":
                return await WatchBangumiTool().call(
                    context,
                    season_id=kwargs.get("season_id", 0),
                    ep_id=kwargs.get("ep_id", 0),
                )
            return "不支持的番剧操作。"

    return BiliBangumiTool,
