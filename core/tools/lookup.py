"""B站站内只读查询：搜索、UP 主信息、公开收藏夹、关注的人最近更新和直播。"""
from datetime import datetime

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
    class SearchBilibiliTool(FunctionTool[AstrAgentContext]):
        name: str = "search_bilibili"
        description: str = (
            "Search Bilibili videos, users, UP details, anime, a user's public favorite folders, "
            "followed-UP updates, or current live streams. Also use when the user wants content recommendations."
        )
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "search keyword"},
                "search_type": {
                    "type": "string",
                    "enum": [
                        "video", "user", "up_info", "bangumi", "favorites",
                        "following_updates", "following_live",
                    ],
                    "description": "what to search; favorites takes a UID or name as keyword; following_* types don't require a keyword",
                    "default": "video",
                },
                "folder": {"type": "string", "description": "favorites only: folder title (or part of it) to open; omit to open the first folder"},
            },
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            keyword = kwargs.get("keyword", "")
            search_type = kwargs.get("search_type", "video")
            if search_type == "following_updates":
                return await CheckFollowingUpdatesTool().call(context)
            if search_type == "following_live":
                return await CheckFollowingLiveTool().call(context)
            if not keyword:
                return "请提供搜索关键词。"
            if search_type == "up_info":
                return await GetUpInfoTool().call(context, query=keyword)
            if search_type == "favorites":
                return await public_favorites(keyword, str(kwargs.get("folder", "") or "").strip())
            if search_type == "user":
                results = await app.search_users(keyword, ps=5)
                if not results:
                    return f"没搜到「{keyword}」相关的UP主。"
                lines = [f"UP主搜索「{keyword}」:"]
                for u in results:
                    lines.append(f"  {u['uname']}(UID:{u['mid']}) 粉丝:{u['fans']} 视频:{u['videos']}个 签名:{clip(u['sign'], 40)}")
                return "\n".join(lines)
            elif search_type == "bangumi":
                results = await app.search_bangumi(keyword, ps=5)
                if not results:
                    return f"没搜到「{keyword}」相关的番剧/动漫。"
                lines = [f"番剧搜索「{keyword}」:"]
                for b in results:
                    score_str = f" 评分:{b['score']}" if b['score'] else ""
                    ep_str = f" {b['ep_size']}话" if b['ep_size'] else ""
                    lines.append(f"  《{b['title']}》{b['season_type_name']}{ep_str}{score_str}")
                    if b['areas'] or b['styles']:
                        lines.append(f"    {b['areas']} {b['styles']}")
                    if b['desc']:
                        lines.append(f"    简介:{clip(b['desc'], 80)}")
                    if b['url']:
                        lines.append(f"    {b['url']}")
                return "\n".join(lines)
            else:
                results = await app.search_videos(keyword, ps=5)
                if not results:
                    return f"没搜到「{keyword}」相关的视频。"
                lines = [f"视频搜索「{keyword}」:"]
                for v in results:
                    lines.append(f"  《{v['title']}》by {v['author']} | {v['bvid']} | 播放:{v['play']} | {v['duration']} | https://www.bilibili.com/video/{v['bvid']}")
                return "\n".join(lines)
    async def resolve_mid(query):
        """UID 原样返回；名字先搜UP主。返回 ``(mid, None)``，找不到或有多个时 ``(None, 给模型的说明)``。"""
        if query.isdigit():
            return query, None
        users = await app.search_users(query, ps=3)
        if not users:
            return None, f"没有找到名为「{query}」的UP主。"
        exact = [u for u in users if u["uname"] == query]
        if len(users) == 1 or exact:
            return str((exact or users)[0]["mid"]), None
        lines = [f"搜到多个UP主，你可以指定UID再查:"]
        for u in users:
            lines.append(f"  {u['uname']}(UID:{u['mid']}) 粉丝:{u['fans']}")
        return None, "\n".join(lines)

    async def public_favorites(query, folder_query):
        """列出某人的公开收藏夹，并打开其中一个看最近收藏的视频。私密收藏夹读不到。"""
        mid, problem = await resolve_mid(query)
        if problem:
            return problem
        folders = await app.public_fav_folders(mid, limit=10)
        if not folders:
            return f"UID:{mid} 没有公开的收藏夹（私密收藏夹读不到）。"
        chosen = folders[0]
        if folder_query:
            matched = [f for f in folders if folder_query.lower() in f["title"].lower()]
            if not matched:
                names = "、".join(f"「{f['title']}」" for f in folders)
                return f"UID:{mid} 的公开收藏夹里没有「{folder_query}」，有这些：{names}"
            chosen = matched[0]
        lines = [f"UID:{mid} 的公开收藏夹:"]
        for f in folders:
            lines.append(f"  「{f['title']}」{f.get('count', 0)}个视频")
        videos = await app.fav_folder_videos(chosen["id"], ps=10)
        if videos:
            lines.append(f"「{chosen['title']}」最近收藏:")
            for v in videos:
                date_str = datetime.fromtimestamp(v["fav_time"]).strftime("%m-%d") if v.get("fav_time") else "?"
                lines.append(f"  [{date_str}]《{v['title']}》by {v['up_name']} | {v['bvid']}")
        else:
            lines.append(f"「{chosen['title']}」里没读到视频。")
        if len(folders) > 1:
            lines.append("（要看别的收藏夹，带上 folder 参数再查一次）")
        return "\n".join(lines)

    @dataclass
    class GetUpInfoTool(FunctionTool[AstrAgentContext]):
        name: str = "get_up_info"
        description: str = f"Get UP's detailed info, recent uploads and dynamics. Your UID={bot_uid}, owner={owner_name}(UID:{owner_uid})."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "UID or name"},
            },
            "required": ["query"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            query = str(kwargs.get("query", ""))
            mid, problem = await resolve_mid(query)
            if problem:
                return problem
            parts = []
            info = await app.up_info(mid)
            if info:
                parts.append(f"UP主: {info['name']}(UID:{info['mid']}) LV{info['level']} {info['official_title']} {info['vip_label']}\n签名:{info['sign']}")
            else:
                parts.append(f"UP主 UID:{mid} 获取失败")
            videos = await app.up_recent_videos(mid, ps=5)
            if videos:
                vlines = ["最近投稿:"]
                for v in videos:
                    ts = v.get("created", 0)
                    date_str = datetime.fromtimestamp(ts).strftime("%m-%d") if ts else "?"
                    vlines.append(f"  [{date_str}]《{v['title']}》{v['bvid']} 播放:{v['play']}")
                parts.append("\n".join(vlines))
            dynamics = await app.up_recent_dynamics(mid, limit=3)
            if dynamics:
                dlines = ["最近动态:"]
                for d in dynamics:
                    dlines.append(f"  [{d['pub_time']}] {clip(d['text'], 60) or '(无文字)'}")
                parts.append("\n".join(dlines))
            parts.append(f"（UID={mid}，如需关注可调用 bili_action(action=follow)）")
            return "\n\n".join(parts)
    @dataclass
    class CheckFollowingUpdatesTool(FunctionTool[AstrAgentContext]):
        name: str = "check_following_updates"
        description: str = "Check today's updates from followed UPs. Call once per conversation."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {},
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            results = await app.following_updates(limit=20)
            if not results:
                return "今天关注的UP主都没有更新。"
            lines = [f"今天关注列表有 {len(results)} 条更新:"]
            for r in results:
                if r["video_title"]:
                    lines.append(f"  {r['up_name']} 投稿了视频《{r['video_title']}》{r['video_bvid']} ({r['pub_time']})")
                elif r.get("live_title"):
                    lines.append(f"  {r['up_name']} 在直播：{r['live_title']} ({r['pub_time']})")
                elif r["text"]:
                    lines.append(f"  {r['up_name']} 发了动态：{clip(r['text'], 60)} ({r['pub_time']})")
                else:
                    lines.append(f"  {r['up_name']} 有新动态 ({r['pub_time']})")
            return "\n".join(lines)
    @dataclass
    class CheckFollowingLiveTool(FunctionTool[AstrAgentContext]):
        name: str = "check_following_live"
        description: str = "Check which followed UPs are currently live streaming."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {},
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            results = await app.following_live()
            if not results:
                return "关注的人现在都没有在直播。"
            lines = [f"有 {len(results)} 个关注的人在直播:"]
            for r in results:
                lines.append(f"  {r['uname']} 在播：{r['title']} | 分区:{r['area_name']} | 人气:{r['online']} | {r['link']}")
            return "\n".join(lines)

    return SearchBilibiliTool,
