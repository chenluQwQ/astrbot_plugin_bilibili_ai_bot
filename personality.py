"""BiliBot LLM 工具定义（FunctionTool 模式）。
工具返回的字符串会回到 LLM，由 LLM 用人设风格重新生成回复。
"""
from datetime import datetime
from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.api import logger
from .config import USER_PROFILE_FILE, AFFECTION_FILE, LEVEL_NAMES


def create_tools(plugin):
    """创建所有工具实例，通过闭包捕获 plugin 引用。"""

    # Bot 自身身份信息，供工具描述和调用使用
    bot_uid = plugin.config.get("DEDE_USER_ID", "未知")
    owner_uid = plugin.config.get("OWNER_MID", "未知")
    owner_name = plugin.config.get("OWNER_NAME", "未知")

    # ── 记忆类工具 ──

    @dataclass
    class RecallUserTool(FunctionTool[AstrAgentContext]):
        name: str = "recall_user"
        description: str = f"查询某个B站用户的画像、印象、好感度和已知信息。每次对话对同一用户只查一次，查不到就说不了解。提示：你自己的B站UID是{bot_uid}，主人的UID是{owner_uid}（{owner_name}）。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "B站用户UID或用户名关键词"},
            },
            "required": ["user_id"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            user_id = kwargs.get("user_id", "")
            profiles = plugin._load_json(USER_PROFILE_FILE, {})
            if user_id in profiles or user_id in plugin._affection:
                p = profiles.get(user_id, {})
                sc = plugin._affection.get(user_id, 0)
                lv = plugin._get_level(sc, user_id)
                lines = [
                    f"[用户资料] UID:{user_id} 昵称:{p.get('username', '未知')}",
                    f"好感度:{sc}分 等级:{LEVEL_NAMES[lv]}",
                ]
                if p.get("impression"): lines.append(f"印象:{p['impression']}")
                if p.get("facts"): lines.append(f"已知信息:{'；'.join(p['facts'][-8:])}")
                user_mems = [m for m in plugin._memory if m.get("user_id") == user_id][-5:]
                if user_mems:
                    lines.append("最近交互:" + "；".join(m.get('text', '')[:80] for m in user_mems))
                return "\n".join(lines)
            matches = [(uid, p) for uid, p in profiles.items() if user_id.lower() in (p.get("username", "") or "").lower()]
            if matches:
                lines = [f"找到{len(matches)}个匹配:"]
                for uid, p in matches[:5]:
                    sc = plugin._affection.get(uid, 0)
                    lines.append(f"  UID:{uid} {p.get('username','')} {sc}分 {p.get('impression','')[:30]}")
                return "\n".join(lines)
            return f"没有找到用户「{user_id}」的记录。"

    @dataclass
    class RecallConversationTool(FunctionTool[AstrAgentContext]):
        name: str = "recall_conversation"
        description: str = "搜索交流/对话记忆，回忆和某个用户聊过什么。每次对话只调用一次，查不到就说没有相关记忆，不要重复调用。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
                "user_id": {"type": "string", "description": "可选，限定某个用户UID", "default": ""},
            },
            "required": ["keyword"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            keyword = kwargs.get("keyword", "")
            user_id = kwargs.get("user_id", "") or None
            results = await plugin._search_memories(keyword, limit=5, memory_types={"chat"}, user_id=user_id, score_threshold=0.5)
            if not results:
                return f"没有找到与「{keyword}」相关的对话记忆。"
            return "对话记忆:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(results, 1))

    @dataclass
    class RecallVideoTool(FunctionTool[AstrAgentContext]):
        name: str = "recall_video"
        description: str = "搜索看过的视频记忆，回忆某个视频的内容和感想。每次对话只调用一次，查不到就说没有相关记忆，不要重复调用。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词，如视频标题、UP主名、内容关键词"},
            },
            "required": ["keyword"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            keyword = kwargs.get("keyword", "")
            results = await plugin._search_memories(keyword, limit=5, memory_types={"video"}, score_threshold=0.5)
            if not results:
                return f"没有找到与「{keyword}」相关的视频记忆。"
            return "视频记忆:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(results, 1))

    @dataclass
    class RecallDynamicTool(FunctionTool[AstrAgentContext]):
        name: str = "recall_dynamic"
        description: str = "搜索动态相关记忆，回忆发过或看过的动态。每次对话只调用一次，查不到就说没有相关记忆，不要重复调用。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["keyword"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            keyword = kwargs.get("keyword", "")
            results = await plugin._search_memories(keyword, limit=5, memory_types={"dynamic"}, score_threshold=0.5)
            if not results:
                return f"没有找到与「{keyword}」相关的动态记忆。"
            return "动态记忆:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(results, 1))

    # ── B站查询工具 ──

    @dataclass
    class SearchBilibiliTool(FunctionTool[AstrAgentContext]):
        name: str = "search_bilibili"
        description: str = "在B站搜索视频或UP主。当用户想看某类内容（如'我想看猫咪'、'有没有好看的游戏视频'）时也用这个搜索并推荐。不确定用户想搜什么时先问清楚。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
                "search_type": {"type": "string", "description": "搜索类型：video=视频，user=UP主", "default": "video"},
            },
            "required": ["keyword"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            keyword = kwargs.get("keyword", "")
            search_type = kwargs.get("search_type", "video")
            if search_type == "user":
                results = await plugin.search_bilibili_users(keyword, ps=5)
                if not results:
                    return f"没搜到「{keyword}」相关的UP主。"
                lines = [f"UP主搜索「{keyword}」:"]
                for u in results:
                    lines.append(f"  {u['uname']}(UID:{u['mid']}) 粉丝:{u['fans']} 视频:{u['videos']}个 签名:{u['sign'][:40]}")
                return "\n".join(lines)
            else:
                results = await plugin.search_bilibili_videos(keyword, ps=5)
                if not results:
                    return f"没搜到「{keyword}」相关的视频。"
                lines = [f"视频搜索「{keyword}」:"]
                for v in results:
                    lines.append(f"  《{v['title']}》by {v['author']} | {v['bvid']} | 播放:{v['play']} | {v['duration']} | https://www.bilibili.com/video/{v['bvid']}")
                return "\n".join(lines)

    @dataclass
    class GetUpInfoTool(FunctionTool[AstrAgentContext]):
        name: str = "get_up_info"
        description: str = f"查询B站UP主的详细信息、最近投稿和动态。不确定用户是否需要时先问。提示：你自己的B站UID是{bot_uid}，主人的UID是{owner_uid}（{owner_name}）。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "UP主的UID或名字都可以"},
            },
            "required": ["query"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            query = kwargs.get("query", "")
            mid = query
            # 如果不是纯数字，先搜索UP主名字
            if not query.isdigit():
                users = await plugin.search_bilibili_users(query, ps=3)
                if not users:
                    return f"没有找到名为「{query}」的UP主。"
                if len(users) == 1:
                    mid = str(users[0]["mid"])
                else:
                    # 多个结果，先看有没有精确匹配
                    exact = [u for u in users if u["uname"] == query]
                    if exact:
                        mid = str(exact[0]["mid"])
                    else:
                        lines = [f"搜到多个UP主，你可以指定UID再查:"]
                        for u in users:
                            lines.append(f"  {u['uname']}(UID:{u['mid']}) 粉丝:{u['fans']}")
                        return "\n".join(lines)
            parts = []
            info = await plugin.get_up_info(mid)
            if info:
                parts.append(f"UP主: {info['name']}(UID:{info['mid']}) LV{info['level']} {info['official_title']} {info['vip_label']}\n签名:{info['sign']}")
            else:
                parts.append(f"UP主 UID:{mid} 获取失败")
            videos = await plugin.get_up_recent_videos(mid, ps=5)
            if videos:
                vlines = ["最近投稿:"]
                for v in videos:
                    ts = v.get("created", 0)
                    date_str = datetime.fromtimestamp(ts).strftime("%m-%d") if ts else "?"
                    vlines.append(f"  [{date_str}]《{v['title']}》{v['bvid']} 播放:{v['play']}")
                parts.append("\n".join(vlines))
            dynamics = await plugin.get_up_recent_dynamics(mid, limit=3)
            if dynamics:
                dlines = ["最近动态:"]
                for d in dynamics:
                    dlines.append(f"  [{d['pub_time']}] {d['text'][:60] or '(无文字)'}")
                parts.append("\n".join(dlines))
            parts.append(f"（UID={mid}，如需关注可调用follow_up）")
            return "\n\n".join(parts)

    @dataclass
    class WatchVideoTool(FunctionTool[AstrAgentContext]):
        name: str = "watch_video"
        description: str = "去看一个B站视频，了解内容并存入记忆。不确定用户是否想让你看时先问。看完后可以选择调用like_video/coin_video/fav_video/follow_up/post_comment。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "视频的BV号，如 BV1xx411x7xx"},
            },
            "required": ["bvid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            bvid = kwargs.get("bvid", "")
            try:
                oid = await plugin._get_video_oid(bvid)
                if not oid:
                    return f"找不到视频 {bvid}"
                vi = await plugin._get_video_info(oid)
                if not vi:
                    return f"获取视频信息失败 {bvid}"
                analysis_info = {
                    "bvid": vi.get("bvid", bvid), "title": vi.get("title", ""),
                    "desc": vi.get("desc", ""), "up_name": vi.get("owner_name", ""),
                    "up_mid": vi.get("owner_mid", ""), "tname": vi.get("tname", ""),
                    "duration": vi.get("duration", 0), "pic": vi.get("pic", ""),
                    "cid": vi.get("cid", 0), "oid": oid,
                }
                video_description = await plugin._analyze_video_with_vision(analysis_info)
                evaluation = await plugin._evaluate_video(analysis_info, video_description)
                score = (evaluation or {}).get("score", 5)
                mood = (evaluation or {}).get("mood", "平静")
                review = (evaluation or {}).get("review", "")
                # 存记忆
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                memory_text = (
                    f"[{now_str}] Bot看了视频《{vi.get('title', '')}》"
                    f"(UP主:{vi.get('owner_name', '')}) "
                    f"评分:{score}/10 心情:{mood} "
                    f"感想:{review[:80]} 内容:{video_description[:500]}"
                )
                await plugin._save_self_memory_record(
                    f"tool_watch:{bvid}", memory_text, memory_type="video",
                    extra={"bvid": bvid, "owner_mid": str(vi.get("owner_mid", "")), "video_title": vi.get("title", "")},
                )
                link = f"https://www.bilibili.com/video/{bvid}"
                return (
                    f"[已看完视频]\n"
                    f"标题：{vi.get('title', '')}\n"
                    f"UP主：{vi.get('owner_name', '')}(UID:{vi.get('owner_mid', '')}) | 分区：{vi.get('tname', '')}\n"
                    f"链接：{link}\n"
                    f"视频简介：{vi.get('desc', '')[:150]}\n"
                    f"内容详情：{video_description[:800]}\n"
                    f"我的感受：{review[:200]}\n"
                    f"个人评分：{score}/10 | 看完心情：{mood}\n"
                    f"oid={oid}\n"
                    f"（如需互动可调用：like_video点赞/coin_video投币/fav_video收藏/follow_up关注UP主/post_comment评论）"
                )
            except Exception as e:
                logger.error(f"[BiliBot] watch_video工具异常: {e}")
                return f"看视频时出错了: {e}"

    # ── B站操作工具 ──

    @dataclass
    class PostCommentTool(FunctionTool[AstrAgentContext]):
        name: str = "post_comment"
        description: str = "在B站视频下发一条评论。需要用户同意或主动要求时才使用。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "视频的oid（从watch_video结果中获取）"},
                "comment_text": {"type": "string", "description": "要发的评论内容"},
            },
            "required": ["oid", "comment_text"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            try:
                oid = kwargs.get("oid", "")
                comment_text = kwargs.get("comment_text", "")
                success = await plugin._send_comment(int(oid), comment_text)
                return f"评论成功：{comment_text}" if success else "评论发送失败。"
            except Exception as e:
                return f"评论出错：{e}"

    @dataclass
    class LikeVideoTool(FunctionTool[AstrAgentContext]):
        name: str = "like_video"
        description: str = "给B站视频点赞。需要用户同意或主动要求时才使用。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "视频的oid（从watch_video结果中获取）"},
            },
            "required": ["oid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            success = await plugin._like_video(int(kwargs.get("oid", "0")))
            return "点赞成功。" if success else "点赞失败。"

    @dataclass
    class CoinVideoTool(FunctionTool[AstrAgentContext]):
        name: str = "coin_video"
        description: str = "给B站视频投币。需要用户同意或主动要求时才使用。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "视频的oid（从watch_video结果中获取）"},
                "num": {"type": "string", "description": "投币数量，1或2", "default": "1"},
            },
            "required": ["oid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            num = int(kwargs.get("num", "1"))
            success = await plugin._coin_video(int(kwargs.get("oid", "0")), num=num)
            return f"投了{num}个币。" if success else "投币失败。"

    @dataclass
    class FavVideoTool(FunctionTool[AstrAgentContext]):
        name: str = "fav_video"
        description: str = "收藏B站视频到默认收藏夹。需要用户同意或主动要求时才使用。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "视频的oid（从watch_video结果中获取）"},
            },
            "required": ["oid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            success = await plugin._fav_video(int(kwargs.get("oid", "0")))
            return "收藏成功。" if success else "收藏失败。"

    @dataclass
    class FollowUpTool(FunctionTool[AstrAgentContext]):
        name: str = "follow_up"
        description: str = "关注B站UP主。需要用户同意或主动要求时才使用。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "UP主的UID或名字"},
            },
            "required": ["query"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            query = kwargs.get("query", "0")
            mid = query
            if not query.isdigit():
                users = await plugin.search_bilibili_users(query, ps=1)
                if not users:
                    return f"没有找到名为「{query}」的UP主，无法关注。"
                mid = str(users[0]["mid"])
            success = await plugin._follow_user(int(mid))
            return f"已关注UP主(UID:{mid})。" if success else "关注失败。"

    @dataclass
    class CheckFollowingUpdatesTool(FunctionTool[AstrAgentContext]):
        name: str = "check_following_updates"
        description: str = "查看今天关注的UP主有没有人更新（发视频、发动态）。每次对话只调用一次。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {},
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            results = await plugin.get_following_updates(limit=20)
            if not results:
                return "今天关注的UP主都没有更新。"
            lines = [f"今天关注列表有 {len(results)} 条更新:"]
            for r in results:
                if r["video_title"]:
                    lines.append(f"  {r['up_name']} 投稿了视频《{r['video_title']}》{r['video_bvid']} ({r['pub_time']})")
                elif r.get("live_title"):
                    lines.append(f"  {r['up_name']} 在直播：{r['live_title']} ({r['pub_time']})")
                elif r["text"]:
                    lines.append(f"  {r['up_name']} 发了动态：{r['text'][:60]} ({r['pub_time']})")
                else:
                    lines.append(f"  {r['up_name']} 有新动态 ({r['pub_time']})")
            return "\n".join(lines)

    @dataclass
    class CheckFollowingLiveTool(FunctionTool[AstrAgentContext]):
        name: str = "check_following_live"
        description: str = "查看关注的UP主谁在直播。每次对话只调用一次。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {},
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            results = await plugin.get_following_live()
            if not results:
                return "关注的人现在都没有在直播。"
            lines = [f"有 {len(results)} 个关注的人在直播:"]
            for r in results:
                lines.append(f"  {r['uname']} 在播：{r['title']} | 分区:{r['area_name']} | 人气:{r['online']} | {r['link']}")
            return "\n".join(lines)

    @dataclass
    class WatchVideosTool(FunctionTool[AstrAgentContext]):
        name: str = "bili_watch_videos"
        description: str = "触发一次主动看B站视频流程。用户明确要求去看看/刷刷视频时使用。"
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {},
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return await plugin._tool_bili_watch_videos_result()

    # 返回所有工具实例
    return [
        # 记忆类
        RecallUserTool(),
        RecallConversationTool(),
        RecallVideoTool(),
        RecallDynamicTool(),
        # B站查询
        SearchBilibiliTool(),
        GetUpInfoTool(),
        WatchVideoTool(),
        # B站操作
        PostCommentTool(),
        LikeVideoTool(),
        CoinVideoTool(),
        FavVideoTool(),
        FollowUpTool(),
        CheckFollowingUpdatesTool(),
        CheckFollowingLiveTool(),
        WatchVideosTool(),
    ]
