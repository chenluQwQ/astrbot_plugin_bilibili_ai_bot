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
from .config import USER_PROFILE_FILE, AFFECTION_FILE, LEVEL_NAMES, DATA_DIR


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
        description: str = f"Look up a Bilibili user's profile, impression and affection. Your UID={bot_uid}, owner={owner_name}(UID:{owner_uid}). Call once per user per conversation."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "UID or username keyword"},
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
            results = await plugin._search_memories(keyword, limit=5, memory_types={"chat"}, user_id=user_id, score_threshold=0.5)
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
            parts = []
            # 1. 视频观看日志
            from .config import WATCH_LOG_FILE, BANGUMI_WATCH_LOG_FILE, REPLY_LOG_FILE, PROACTIVE_LOG_FILE, DYNAMIC_LOG_FILE
            wl = plugin._load_json(WATCH_LOG_FILE, [])
            today_watch = [l for l in wl if l.get("time", "").startswith(target_date)]
            if today_watch:
                vlines = [f"🎬 看了 {len(today_watch)} 个视频:"]
                for w in today_watch:
                    vlines.append(f"  [{w.get('time','?').split(' ',1)[-1]}] 「{w.get('title','?')[:30]}」{w.get('score','?')}分 {w.get('mood','?')} {w.get('review','')[:40]}")
                parts.append("\n".join(vlines))
            # 2. 番剧日志
            bl = plugin._load_json(BANGUMI_WATCH_LOG_FILE, [])
            today_bangumi = [l for l in bl if l.get("time", "").startswith(target_date)]
            if today_bangumi:
                blines = [f"📺 看了 {len(today_bangumi)} 集番剧:"]
                for b in today_bangumi:
                    blines.append(f"  [{b.get('time','?').split(' ',1)[-1]}] 《{b.get('title','?')}》第{b.get('ep_index','?')}话 {b.get('score','?')}分 {b.get('mood','?')}")
                parts.append("\n".join(blines))
            # 3. 回复日志
            rl = plugin._load_json(REPLY_LOG_FILE, [])
            today_reply = [r for r in rl if r.get("time", "").startswith(target_date)]
            if today_reply:
                rlines = [f"💬 回复了 {len(today_reply)} 条评论:"]
                for r in today_reply:
                    rlines.append(f"  [{r.get('time','?').split(' ',1)[-1]}] {r.get('username','?')}: {r.get('content','')[:30]} → {r.get('reply','')[:30]}")
                parts.append("\n".join(rlines))
            # 4. 主动评论
            pl = plugin._load_json(PROACTIVE_LOG_FILE, [])
            today_comment = [c for c in pl if c.get("time", "").startswith(target_date)]
            if today_comment:
                clines = [f"📝 发了 {len(today_comment)} 条主动评论:"]
                for c in today_comment:
                    clines.append(f"  [{c.get('time','?').split(' ',1)[-1]}] 「{c.get('title','')[:20]}」{c.get('comment','')[:40]}")
                parts.append("\n".join(clines))
            # 5. 动态
            dl = plugin._load_json(DYNAMIC_LOG_FILE, [])
            today_dynamic = [d for d in dl if d.get("time", "").startswith(target_date)]
            if today_dynamic:
                dlines = [f"📢 发了 {len(today_dynamic)} 条动态:"]
                for d in today_dynamic:
                    dlines.append(f"  [{d.get('time','?').split(' ',1)[-1]}] {d.get('content','')[:50]}")
                parts.append("\n".join(dlines))
            if not parts:
                return f"{target_date} 没有任何活动记录。"
            return f"📋 {target_date} 活动总览:\n\n" + "\n\n".join(parts)

    @dataclass
    class RecallVideoTool(FunctionTool[AstrAgentContext]):
        name: str = "recall_video"
        description: str = "Search watched video memories. Call once, don't retry if empty."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "title, UP name, or topic"},
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
        description: str = "Search dynamic/post memories. Call once, don't retry if empty."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "keyword"},
            },
            "required": ["keyword"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            keyword = kwargs.get("keyword", "")
            results = await plugin._search_memories(keyword, limit=5, memory_types={"dynamic"}, score_threshold=0.5)
            if not results:
                return f"没有找到与「{keyword}」相关的动态记忆。"
            return "动态记忆:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(results, 1))

    @dataclass
    class RecallBangumiTool(FunctionTool[AstrAgentContext]):
        name: str = "recall_bangumi"
        description: str = "Search anime/bangumi watching memories and progress. Use when user asks about anime you've watched."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "anime title or topic"},
            },
            "required": ["keyword"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            keyword = kwargs.get("keyword", "")
            # 先查番剧专属记忆文件
            from .config import BANGUMI_MEMORY_FILE
            mem = plugin._load_json(BANGUMI_MEMORY_FILE, {})
            matched = []
            for sid, record in mem.items():
                if keyword.lower() in record.get("title", "").lower():
                    eps = record.get("episodes", [])
                    total = record.get("total_watched", 0)
                    last_ep = record.get("last_ep_index", "?")
                    last_score = record.get("last_score", "?")
                    lines = [f"《{record['title']}》已看{total}集 最新:第{last_ep}话 评分:{last_score}"]
                    for e in eps[-5:]:
                        lines.append(f"  第{e.get('ep_index', '?')}话 {e.get('mood', '')} {e.get('review', '')}")
                    matched.append("\n".join(lines))
            # 再查通用记忆
            results = await plugin._search_memories(keyword, limit=3, memory_types={"bangumi"}, score_threshold=0.5)
            parts = []
            if matched:
                parts.append("番剧追番记录:\n" + "\n---\n".join(matched[:3]))
            if results:
                parts.append("番剧记忆:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(results, 1)))
            if not parts:
                return f"没有找到与「{keyword}」相关的番剧记忆。"
            return "\n\n".join(parts)

    # ── B站查询工具 ──

    @dataclass
    class SearchBilibiliTool(FunctionTool[AstrAgentContext]):
        name: str = "search_bilibili"
        description: str = "Search Bilibili for videos, UPs, or anime/bangumi. Also use when user wants content recommendations."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "search keyword"},
                "search_type": {"type": "string", "description": "video, user, or bangumi", "default": "video"},
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
            elif search_type == "bangumi":
                results = await plugin.search_bilibili_bangumi(keyword, ps=5)
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
                        lines.append(f"    简介:{b['desc'][:80]}")
                    if b['url']:
                        lines.append(f"    {b['url']}")
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
        description: str = f"Get UP's detailed info, recent uploads and dynamics. Your UID={bot_uid}, owner={owner_name}(UID:{owner_uid})."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "UID or name"},
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
        description: str = "Watch a Bilibili video and save to memory. Can then use like/coin/fav/follow/comment tools."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "BV ID, e.g. BV1xx411x7xx"},
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
        description: str = "Post a comment on a Bilibili video. Only when user agrees or asks."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "video oid from watch_video"},
                "comment_text": {"type": "string", "description": "comment content"},
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
        description: str = "Like a Bilibili video. Only when user agrees or asks."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "video oid from watch_video"},
            },
            "required": ["oid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            success = await plugin._like_video(int(kwargs.get("oid", "0")))
            return "点赞成功。" if success else "点赞失败。"

    @dataclass
    class CoinVideoTool(FunctionTool[AstrAgentContext]):
        name: str = "coin_video"
        description: str = "Give coins to a Bilibili video. Only when user agrees or asks."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "video oid from watch_video"},
                "num": {"type": "string", "description": "1 or 2", "default": "1"},
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
        description: str = "Favorite a Bilibili video. Only when user agrees or asks."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "video oid from watch_video"},
            },
            "required": ["oid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            success = await plugin._fav_video(int(kwargs.get("oid", "0")))
            return "收藏成功。" if success else "收藏失败。"

    @dataclass
    class FollowUpTool(FunctionTool[AstrAgentContext]):
        name: str = "follow_up"
        description: str = "Follow a Bilibili UP. Only when user agrees or asks."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "UID or name"},
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
        description: str = "Check today's updates from followed UPs. Call once per conversation."
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
        description: str = "Check which followed UPs are currently live streaming."
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
        description: str = "Trigger proactive video browsing on Bilibili. Use when user asks to watch/browse videos."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {},
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return await plugin._tool_bili_watch_videos_result()

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
            detail = await plugin.get_bangumi_detail(season_id=season_id)
            if not detail:
                return f"获取番剧详情失败（season_id={season_id}）。"
            lines = [
                f"《{detail['title']}》",
                f"评分:{detail['score']}（{detail['count']}人评） 地区:{detail['areas']} 类型:{detail['styles']}",
                f"集数:{detail['total_ep']} {detail['new_ep_desc']}",
                f"播放:{detail['stat_views']} 弹幕:{detail['stat_danmakus']} 追番:{detail['stat_favorites']}",
            ]
            if detail['evaluate']:
                lines.append(f"简评:{detail['evaluate'][:150]}")
            if detail['episodes']:
                recent = detail['episodes'][-5:]
                ep_list = " / ".join(ep['title'][:25] for ep in recent)
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
            results = await plugin.get_bangumi_trending(season_type=season_type)
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
            results = await plugin.get_bangumi_timeline(day_before=2, day_after=3)
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
            followed = await plugin._get_followed_bangumi(follow_status=2)
            if not followed:
                return "没有在追的番剧，或者获取追番列表失败。"
            mem = plugin._load_bangumi_memory()
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
            return await plugin._tool_bili_watch_bangumi_result(season_id=season_id, ep_id=ep_id)

    # ── B站用户拉黑 ──

    @dataclass
    class BlockBiliUserTool(FunctionTool[AstrAgentContext]):
        name: str = "bili_block_user"
        description: str = (
            "拉黑一个B站用户。当主人同意/要求拉黑某个B站用户时调用。"
            "Block a Bilibili user by UID. Use when the owner confirms blocking someone."
        )
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "uid": {"type": "string", "description": "要拉黑的B站用户 UID"},
                "reason": {"type": "string", "description": "拉黑原因（简短描述）", "default": "恶意评论"},
            },
            "required": ["uid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            uid = str(kwargs.get("uid", "")).strip()
            reason = kwargs.get("reason", "恶意评论") or "恶意评论"
            if not uid or not uid.isdigit():
                return ToolExecResult(is_success=False, description="UID 无效，请提供纯数字的B站UID。")

            # 检查是否是主人
            if plugin._is_owner(uid):
                return ToolExecResult(is_success=False, description="不能拉黑主人！")

            import os, json
            from datetime import datetime as dt

            # 调用B站API拉黑
            api_ok = await plugin._block_user(int(uid))

            # 写入本地黑名单
            bl_path = os.path.join(DATA_DIR, "block_log.json")
            bl = plugin._load_json(bl_path, {})
            bl[uid] = {
                "username": reason,
                "reason": reason,
                "time": dt.now().strftime("%Y-%m-%d %H:%M"),
            }
            plugin._save_json(bl_path, bl)

            if api_ok:
                logger.info(f"[BiliBot] 🚫 工具拉黑 UID:{uid}（{reason}）")
                return ToolExecResult(
                    is_success=True,
                    description=f"已拉黑 UID:{uid}，B站API调用成功。",
                )
            else:
                logger.warning(f"[BiliBot] ⚠️ 工具拉黑 UID:{uid} B站API失败，已加本地黑名单")
                return ToolExecResult(
                    is_success=True,
                    description=f"UID:{uid} 已加入本地黑名单，但B站API调用失败（可能需要重新登录）。",
                )

    # 返回所有工具实例
    return [
        # 记忆类
        RecallUserTool(),
        RecallConversationTool(),
        RecallTodayTool(),
        RecallVideoTool(),
        RecallDynamicTool(),
        RecallBangumiTool(),
        # B站查询
        SearchBilibiliTool(),
        GetUpInfoTool(),
        WatchVideoTool(),
        # 番剧
        GetBangumiInfoTool(),
        GetBangumiTrendingTool(),
        GetBangumiTimelineTool(),
        GetBangumiUpdatesTool(),
        WatchBangumiTool(),
        # B站操作
        PostCommentTool(),
        LikeVideoTool(),
        CoinVideoTool(),
        FavVideoTool(),
        FollowUpTool(),
        CheckFollowingUpdatesTool(),
        CheckFollowingLiveTool(),
        WatchVideosTool(),
        # 用户管理
        BlockBiliUserTool(),
    ]
