"""私信回复前的只读查询：B站站内搜索、看片、关注和番剧概况、联网搜索；查 UP、视频资料、番剧搜索、Bot 的记忆交给 ``bili_lookups``。"""
from __future__ import annotations

import re

from astrbot.api import logger
from ..infra.host_view import HostView


class PrivateLookup(HostView):
    """全部只读：只查、只看，不动账号上的任何东西。"""

    HOST_API = frozenset({
        "bangumi_memory", "bili_bangumi", "bili_dynamics", "bili_lookups", "bili_users", "bili_videos",
        "reply_gate", "roles", "security_log", "settings", "watch_on_request", "web_search",
    })

    @staticmethod
    def _private_bili_fallback_query(text, action):
        value = str(text or "").strip()
        if action == "up_info":
            patterns = (
                # 优先取“看看/查一下/搜一下”后、最近/最新前的名字。
                # 避免把用户输入中的称呼和测试口令当成 UP 主搜索词。
                r"(?:看一下|看下|看看|查一下|查查|查询一下|查询|搜一下|搜搜|搜索一下|搜索|找一下|找找|了解一下)\s*([\w\-·\u4e00-\u9fff]{1,30}?)\s*(?:的)?\s*(?:最近|近期|最新)",
                r"(?:查|搜|搜索|看看|看下|了解)?(?:一下)?\s*([\w\-·\u4e00-\u9fff]{1,30}?)\s*(?:这个|这位)?\s*(?:UP主|up主|UP|up)",
                r"([\w\-·\u4e00-\u9fff]{1,30}?)(?:最近|近期)?(?:发了什么|有什么|有哪些)(?:新)?(?:视频|投稿)",
                r"([\w\-·\u4e00-\u9fff]{1,30}?)(?:最近|近期)?(?:有没有|有无|有)?(?:更新|新发|发新)(?:了)?(?:什么|哪些)?(?:视频|投稿)",
                r"([\w\-·\u4e00-\u9fff]{1,30}?)(?:的)?(?:最新|最近)(?:的)?(?:几个|几期|一些|什么|哪些)?(?:新)?(?:视频|投稿)",
            )
            for pattern in patterns:
                match = re.search(pattern, value)
                if match:
                    query = re.sub(r"^(?:请|麻烦|帮我|给我|一下)+", "", match.group(1))
                    if query:
                        return query[:80]
        value = re.sub(
            r"(?i)(?:请|麻烦|可以|能不能|能否|帮我|给我|一下|在B站|在b站|"
            r"B站|b站|哔哩哔哩|搜索|搜搜|搜|查找|查|找找|找|推荐|"
            r"你去|你能|看完|看看|看下|看一下|分析一下|点评一下|"
            r"有没有|有哪些|有什么|相关的|相关|几个|一些|一个|个|这个|这位|UP主|up主)",
            " ",
            value,
        )
        value = re.sub(r"(?:视频|投稿)[吗呢呀吧啊？?！!。]*$", "", value)
        value = re.sub(r"\s+", " ", value).strip(" ，,。？?！!：:")
        return value[:80]

    async def _execute_private_bili_request(self, action, query, view="资料"):
        """Execute one model-selected, read-only Bilibili lookup/watch request."""
        action = str(action or "").strip().lower()
        query = str(query or "").strip()[:100]
        if action not in {"video_search", "search_and_watch", "up_info"} or not query:
            return "后台B站查询参数不完整，未执行查询。"
        try:
            limit = max(
                1,
                min(5, int(self.settings.get("PRIVATE_MESSAGE_BILI_SEARCH_LIMIT") or 5)),
            )
        except (TypeError, ValueError):
            limit = 5

        if action in {"video_search", "search_and_watch"}:
            videos = await self.bili_videos.search_bilibili_videos(query, ps=limit)
            if not videos:
                logger.info(f"[BiliBot] 🔎 私信视频搜索无结果：{query}")
                return f"站内视频搜索“{query}”没有找到结果。请如实告诉用户，可以请其换个关键词。"
            lines = [f"站内视频搜索“{query}”结果："]
            for index, video in enumerate(videos, 1):
                bvid = str(video.get("bvid") or "")
                lines.append(
                    f"{index}. 《{video.get('title', '')}》｜UP：{video.get('author', '')}｜"
                    f"{bvid}｜播放：{video.get('play', 0)}｜时长：{video.get('duration', '')}｜"
                    f"https://www.bilibili.com/video/{bvid}"
                )
            if action == "search_and_watch":
                selected = videos[0]
                selected_bvid = str(selected.get("bvid") or "")
                if self.settings.get("PRIVATE_MESSAGE_AUTO_WATCH_VIDEO") and selected_bvid:
                    watch_result = await self.watch_on_request._watch_video_and_save_memory(
                        selected_bvid, memory_source="private_search"
                    )
                    if watch_result.get("ok"):
                        lines.extend((
                            "",
                            f"已按相关度选择第1条并实际看完：{selected_bvid}",
                            str(watch_result.get("message") or ""),
                        ))
                    else:
                        lines.extend((
                            "",
                            f"已选择第1条尝试观看，但读取失败：{selected_bvid}",
                            str(watch_result.get("message") or "未知错误"),
                        ))
                else:
                    lines.append("\n用户要求观看，但私信自动看片未开启；只能提供搜索结果，不要声称已经看过。")
            logger.info(f"[BiliBot] 🔎 私信执行 {action}：{query}（{len(videos)} 条）")
            return "\n".join(lines)

        return await self.bili_lookups.up(query, view)

    async def _execute_private_model_tool(
        self, tool_request, *, actor_id="", original_content=""
    ):
        """执行私信回复模型调的一个只读工具。查不到时可能抛 ``ToolMiss``（原因交给模型）。"""
        request = tool_request if isinstance(tool_request, dict) else {}
        name = str(request.get("name") or "none").strip().lower()
        query = str(request.get("query") or "").strip()[:100]
        if name == "none":
            return ""
        allowed = self.reply_gate._allowed_bili_tool_names()
        if name not in allowed:
            if self.settings.get("BILI_TOOL_AUDIT_ENABLED"):
                self.security_log.record("bili_tool_denied", "", "", query, f"工具 {name} 不在B站只读白名单")
            logger.warning(f"[BiliBot] B站工具请求被拒绝：{name}")
            return "该后台能力没有对B站端开放，未执行。"
        no_query_tools = {
            "check_following_updates", "check_following_live", "get_bangumi_trending",
            "get_bangumi_timeline", "get_bangumi_updates",
        }
        if not query and name not in no_query_tools and not (name == "bili_memory" and request.get("target")):
            return "后台查询缺少关键词，未执行。"
        if self.settings.get("BILI_TOOL_AUDIT_ENABLED"):
            self.security_log.record("bili_tool_requested", "", "", query or name, f"只读工具 {name}")

        def compact_result(lines, limit=4800):
            return "\n".join(str(line) for line in lines if line is not None)[:limit]

        if name == "check_following_updates":
            results = await self.bili_dynamics.get_following_updates(limit=12)
            if not results:
                return "今天关注的 UP 主暂时没有读取到新动态。"
            lines = [f"今天关注列表有 {len(results)} 条更新："]
            for item in results[:12]:
                up_name = str(item.get("up_name") or "未知 UP")[:40]
                pub_time = str(item.get("pub_time") or "时间未知")[:30]
                if item.get("video_title"):
                    lines.append(f"- {up_name} 投稿《{str(item.get('video_title'))[:80]}》 {item.get('video_bvid', '')}（{pub_time}）")
                elif item.get("live_title"):
                    lines.append(f"- {up_name} 正在直播：{str(item.get('live_title'))[:80]}（{pub_time}）")
                elif item.get("text"):
                    lines.append(f"- {up_name} 发了动态：{str(item.get('text'))[:120]}（{pub_time}）")
                else:
                    lines.append(f"- {up_name} 有一条新动态（{pub_time}）")
            return compact_result(lines)

        if name == "check_following_live":
            results = await self.bili_users.get_following_live()
            if not results:
                return "关注的人现在没有读取到正在直播的账号。"
            lines = [f"当前有 {len(results)} 个关注账号正在直播："]
            for item in results[:12]:
                lines.append(
                    f"- {str(item.get('uname') or '未知 UP')[:40]}：《{str(item.get('title') or '未命名直播')[:80]}》"
                    f"，分区 {str(item.get('area_name') or '未知')[:30]}，人气 {item.get('online', 0)}，{item.get('link', '')}"
                )
            return compact_result(lines)

        if name == "bili_video_info":
            return await self.bili_lookups.video(query)
        if name == "bili_bangumi_search":
            return await self.bili_lookups.bangumi_search(query)
        if name == "bili_memory":
            return await self.bili_lookups.memory(
                request.get("kind"), query, reader_scope="bili_dm", target=request.get("target", ""))

        if name == "get_bangumi_info":
            match = re.search(r"\d+", query)
            if not match:
                return "番剧详情工具需要数字 season_id，未执行。"
            season_id = int(match.group(0))
            detail = await self.bili_bangumi.get_bangumi_detail(season_id=season_id)
            if not detail:
                return f"没有读取到 season_id={season_id} 的番剧详情。"
            lines = [
                f"《{detail.get('title') or '未命名番剧'}》",
                f"评分 {detail.get('score', 0)}（{detail.get('count', 0)} 人评），地区 {detail.get('areas') or '未知'}，类型 {detail.get('styles') or '未知'}",
                f"集数 {detail.get('total_ep', 0)}，{detail.get('new_ep_desc') or '暂无最新集说明'}",
                f"播放 {detail.get('stat_views', 0)}，弹幕 {detail.get('stat_danmakus', 0)}，追番 {detail.get('stat_favorites', 0)}",
            ]
            if detail.get("evaluate"):
                lines.append(f"简介：{str(detail.get('evaluate'))[:240]}")
            episodes = detail.get("episodes") or []
            if episodes:
                lines.append("最近剧集：" + " / ".join(str(ep.get("title") or "")[:35] for ep in episodes[-5:]))
            if detail.get("link"):
                lines.append(str(detail.get("link")))
            return compact_result(lines)

        if name == "get_bangumi_trending":
            season_type = 4 if "国创" in query else 1
            type_name = "国创" if season_type == 4 else "番剧"
            results = await self.bili_bangumi.get_bangumi_trending(season_type=season_type)
            if not results:
                return f"暂时没有读取到 B站{type_name}排行。"
            lines = [f"B站{type_name}热度排行："]
            for index, item in enumerate(results[:10], 1):
                score = f"，评分 {item.get('score')}" if item.get("score") else ""
                lines.append(f"{index}. 《{str(item.get('title') or '未命名')[:70]}》{score}，{str(item.get('new_ep_desc') or '暂无更新说明')[:60]}")
            return compact_result(lines)

        if name == "get_bangumi_timeline":
            results = await self.bili_bangumi.get_bangumi_timeline(day_before=2, day_after=3)
            if not results:
                return "暂时没有读取到近期新番时间表。"
            days = {}
            weekday_names = {0: "周日", 1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
            for item in results:
                key = f"{item.get('date', '')}（{weekday_names.get(item.get('day_of_week', 0), '')}）"
                days.setdefault(key, []).append(item)
            lines = ["近期新番时间表："]
            for day, items in list(days.items())[:6]:
                lines.append(day)
                for item in items[:8]:
                    status = "已更新" if item.get("published") else "待更新"
                    episode = f"第{item.get('ep_index')}话" if item.get("ep_index") else ""
                    lines.append(f"- [{status}]《{str(item.get('title') or '未命名')[:60]}》{episode}")
            return compact_result(lines)

        if name == "get_bangumi_updates":
            followed = await self.bili_bangumi._get_followed_bangumi(follow_status=2)
            if not followed:
                return "当前没有读取到正在追的番剧，或账号追番列表暂时不可用。"
            memory = self.bangumi_memory.all()
            lines = [f"当前在追番剧共 {len(followed)} 部："]
            for item in followed[:20]:
                record = memory.get(str(item.get("season_id")), {}) if isinstance(memory, dict) else {}
                watched = len(record.get("episodes", [])) if isinstance(record, dict) else 0
                progress = f"已看 {watched} 集" if watched else "插件暂无观看记录"
                lines.append(f"- 《{str(item.get('title') or '未命名')[:70]}》{str(item.get('new_ep_index') or '')[:30]}，{progress}")
            return compact_result(lines)

        action_map = {
            "bili_up_info": "up_info",
            "get_up_info": "up_info",
            "bili_video_search": "video_search",
            "search_bilibili": "video_search",
            "bili_search_and_watch": "search_and_watch",
            "watch_video": "watch_direct",
        }
        if name in action_map:
            if not self.settings.get("PRIVATE_MESSAGE_BILI_SEARCH_ENABLED"):
                return "B站私信站内查询当前未开启，未执行。"
            action = action_map[name]
            if action == "up_info":
                # 即使模型偶尔留下“看看/最新视频”，也在真正调用API前做一次兜底清洗。
                extracted = self._private_bili_fallback_query(query, action)
                if extracted and re.search(r"(?:最近|近期|最新|视频|投稿|UP主|up主)", query):
                    query = extracted
            query = re.sub(
                r"^(?:(?:请|麻烦|帮我|给我|试一下|测试一下|看一下|看下|看看|"
                r"查一下|查查|查询一下|查询|搜一下|搜搜|搜索一下|搜索|"
                r"找一下|找找|了解一下)[\s，,。？?！!：:]*)+",
                "",
                query,
            ).strip(" ，,。？?！!：:")
            logger.info(f"[BiliBot] 🧰 私信回复模型选择工具 {name}：{query}")
            if action == "watch_direct":
                match = re.search(r"(?i)(BV[0-9A-Za-z]{10})", query)
                if not match:
                    return "观看工具需要有效的 BV 号，未执行。"
                force_rewatch = bool(
                    self.roles.is_owner(str(actor_id or ""))
                    and re.search(
                        r"(?:重新\s*看(?:一次|一遍|一下)?|重看)",
                        str(original_content or ""),
                    )
                )
                result = await self.watch_on_request._watch_video_and_save_memory(
                    match.group(1),
                    memory_source="private_tool",
                    force_rewatch=force_rewatch,
                )
                if result.get("ok"):
                    return str(result.get("message") or "已完成视频读取。")
                return str(result.get("message") or result.get("error") or "视频读取失败，未生成回复依据。")
            return await self._execute_private_bili_request(action, query, request.get("view") or "资料")

        if name == "web_search":
            if not self.settings.get("ENABLE_WEB_SEARCH"):
                return "联网搜索当前未开启，未执行。"
            logger.info(f"[BiliBot] 🧰 私信回复模型选择工具 web_search：{query}")
            result = await self.web_search._web_search(query)
            return (
                f"联网搜索“{query}”结果：\n{result}"
                if result else f"联网搜索“{query}”暂时没有取得结果。"
            )
        return "回复模型选择了不受支持的后台能力，未执行。"
