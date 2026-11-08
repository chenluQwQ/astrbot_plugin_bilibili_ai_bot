"""回复前查资料用的 B站只读查询，评论、私信、QQ 共用一份。

每个方法返回给模型看的一段文字；查不到时抛 ``ToolMiss`` 带原因。不可信标记由调用方的 ``ToolRun`` 加。
记忆只查 Bot 自己的经历（看过的视频、发过的动态、追的番），而且按读的人所在的域过滤。
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime

from astrbot.api import logger
from ..domain._memory_tags import BANGUMI, UP, VIDEO, tag
from ..infra.host_view import HostView
from ..infra.tool_loop import ToolMiss

UP_VIEWS = ("资料", "热门", "动态", "直播")
MEMORY_KINDS = {"video": "视频", "dynamic": "动态", "bangumi": "番剧"}
_BV = re.compile(r"(?i)(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])")

# 给模型看的说明和参数，评论和私信用同一份。
UP_TOOL_DESCRIPTION = "按UP主昵称或UID查：资料和最近投稿、播放最多的投稿、最近动态、现在是否在直播"
UP_TOOL_PARAMETERS = {
    "type": "object", "required": ["query"],
    "properties": {
        "query": {"type": "string", "description": "只写UP主昵称或UID"},
        "view": {"type": "string", "enum": list(UP_VIEWS), "description": "查什么，不填就是资料和最近投稿"},
    },
}
VIDEO_TOOL_DESCRIPTION = "按 BV 号读取视频标题、UP、简介和热评；只是资料，不算看过"
BANGUMI_SEARCH_DESCRIPTION = "按名字搜索番剧，结果里有 season_id"
MEMORY_TOOL_DESCRIPTION = "查你自己的记忆：看过的视频、发过的动态、追的番；查不到就是没有，不要编"
MEMORY_TARGET_DESCRIPTION = "可选，只看和某个东西有关的：BV号、UP主的UID、番剧的ss号或追过的番名"
MEMORY_TOOL_PARAMETERS = {
    "type": "object", "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": list(MEMORY_KINDS), "description": "video 视频 / dynamic 动态 / bangumi 番剧"},
        "query": {"type": "string", "description": "标题、UP主或话题关键词；填了 target 可以不填"},
        "target": {"type": "string", "description": MEMORY_TARGET_DESCRIPTION},
    },
}
_SEASON = re.compile(r"(?i)^ss(\d+)$")


def _date(timestamp):
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d") if timestamp else "日期未知"
    except (TypeError, ValueError, OSError, OverflowError):
        return "日期未知"


class BiliLookups(HostView):
    HOST_API = frozenset({
        "bangumi_memory", "bili_bangumi", "bili_comments", "bili_dynamics", "bili_users", "bili_videos",
        "memory_recall", "settings",
    })

    def _list_limit(self):
        try:
            return max(1, min(5, int(self.settings.get("PRIVATE_MESSAGE_BILI_SEARCH_LIMIT") or 5)))
        except (TypeError, ValueError):
            return 5

    async def resolve_up(self, query):
        """昵称或 UID → (UID, 昵称, 补充说明)。纯数字当 UID；昵称取完全一致的，没有就取最相关的并列出候选。"""
        query = str(query or "").strip()[:60]
        if not query:
            raise ToolMiss("需要UP主昵称或UID。")
        if query.isdigit():
            return query, "", ""
        users = await self.bili_users.search_bilibili_users(query, ps=3)
        if not users:
            raise ToolMiss(f"站内没有找到名为“{query}”的UP主，可以请对方提供准确昵称或UID。")
        wanted = re.sub(r"\s+", "", query).casefold()
        exact = [user for user in users if re.sub(r"\s+", "", str(user.get("uname") or "")).casefold() == wanted]
        selected = (exact or users)[0]
        note = ""
        if not exact and len(users) > 1:
            note = "昵称可能有歧义，按最相关结果查询；其他候选：" + "、".join(
                f"{user.get('uname', '')}(UID:{user.get('mid', '')})" for user in users[1:3])
        header = (f"UP：{selected.get('uname', '')}（UID：{selected.get('mid', '')}，粉丝：{selected.get('fans', 0)}，"
                  f"投稿数：{selected.get('videos', 0)}）")
        return str(selected.get("mid") or ""), header, note

    async def up(self, query, view="资料"):
        """UP主：资料和最近投稿 / 热门投稿 / 最近动态 / 是否在直播。"""
        view = view if view in UP_VIEWS else "资料"
        mid, header, note = await self.resolve_up(query)
        lines = [header] if header else [f"UP：UID {mid}"]
        limit = self._list_limit()
        if view == "动态":
            dynamics = await self.bili_dynamics.get_up_recent_dynamics(mid, limit=limit)
            if not dynamics:
                raise ToolMiss(f"没有读取到 UID {mid} 的最近动态。")
            lines.append("最近动态：")
            lines.extend(f"- [{row.get('pub_time') or '时间未知'}] {row.get('text') or '（没有文字，可能是转发或图片）'}"
                         for row in dynamics)
        elif view == "直播":
            info = await self.bili_users.get_up_info(mid)
            if not info:
                raise ToolMiss(f"没有读取到 UID {mid} 的资料。")
            if info.get("live_status") == 1:
                lines.append(f"正在直播：《{info.get('live_title') or '未命名直播'}》 {info.get('live_url') or ''}".rstrip())
            else:
                lines.append("现在没有在直播。")
        else:
            order = "click" if view == "热门" else "pubdate"
            info, videos = await asyncio.gather(
                self.bili_users.get_up_info(mid) if view == "资料" else asyncio.sleep(0),
                self.bili_users.get_up_recent_videos(mid, ps=limit, order=order),
            )
            if info:
                lines.append(f"签名：{info.get('sign') or '未填写'}；认证：{info.get('official_title') or '无'}")
            if videos:
                lines.append("播放最多的投稿：" if view == "热门" else "最近投稿：")
                lines.extend(
                    f"{index}. [{_date(video.get('created'))}]《{video.get('title', '')}》｜{video.get('bvid', '')}｜"
                    f"播放：{video.get('play', 0)}｜https://www.bilibili.com/video/{video.get('bvid', '')}"
                    for index, video in enumerate(videos, 1))
            else:
                lines.append("投稿列表为空或暂时读取失败。")
        if note:
            lines.append(note)
        logger.info(f"[BiliBot] 🔎 查UP {view}：{query} -> {mid}")
        return "\n".join(lines)

    async def video(self, bvid):
        """按 BV 号：标题、UP、简介、热评。只是资料，不算看过。"""
        match = _BV.search(str(bvid or ""))
        if not match:
            raise ToolMiss("需要有效的 BV 号。")
        oid = await self.bili_videos._get_video_oid(match.group(1))
        info = await self.bili_videos._get_video_info(oid) if oid else None
        if not isinstance(info, dict) or not info.get("bvid"):
            raise ToolMiss(f"没有读取到 {match.group(1)} 的视频信息。")
        lines = [
            f"《{str(info.get('title', ''))[:200]}》｜UP：{str(info.get('owner_name', ''))[:80]}｜{info['bvid']}",
            f"简介：{str(info.get('desc') or '无')[:800]}",
        ]
        comments = await self.bili_comments._get_hot_comments(oid, limit=5)
        if comments:
            lines.append("热评：")
            lines.extend(f"- {str(row)[:160]}" for row in comments[:5])
        lines.append("（以上是标题、简介和评论，不是实际画面；没看过不要说看过。）")
        return "\n".join(lines)

    async def bangumi_search(self, keyword):
        keyword = str(keyword or "").strip()[:60]
        if not keyword:
            raise ToolMiss("需要番剧名或关键词。")
        results = await self.bili_bangumi.search_bilibili_bangumi(keyword, ps=self._list_limit())
        if not results:
            raise ToolMiss(f"没有搜到和“{keyword}”有关的番剧。")
        lines = [f"番剧搜索“{keyword}”："]
        for index, item in enumerate(results, 1):
            score = f"，评分 {item.get('score')}" if item.get("score") else ""
            lines.append(
                f"{index}. 《{item.get('title', '')}》（{item.get('season_type_name') or '番剧'}，season_id {item.get('season_id', 0)}"
                f"{score}，{item.get('ep_size', 0)} 集）{str(item.get('desc') or '')[:100]}")
        return "\n".join(lines)

    def _memory_target(self, kind, target):
        """``target``（BV号 / UID / ss号 / 追过的番名）→ 标签；认不出抛 ``ToolMiss``。

        纯数字：查番剧时当 season_id，别的当 UP 的 UID。番名只认追过的（``bangumi_memory`` 里有的），不联网搜。
        """
        bv = _BV.search(target)
        if bv:
            return tag(VIDEO, bv.group(1))
        season = _SEASON.match(target)
        if season:
            return tag(BANGUMI, f"ss{season.group(1)}")
        if target.isdigit():
            return tag(BANGUMI, f"ss{target}") if kind == "bangumi" else tag(UP, target)
        wanted = target.casefold()
        for sid, record in (self.bangumi_memory.all() or {}).items():
            if isinstance(record, dict) and wanted in str(record.get("title", "")).casefold():
                return tag(BANGUMI, f"ss{record.get('season_id') or sid}")
        raise ToolMiss(f"认不出“{target}”：target 要写BV号、UP主的UID、番剧的ss号或追过的番名。")

    async def memory(self, kind, keyword, *, reader_scope, target=""):
        """Bot 自己的经历：看过的视频、发过的动态、追的番。只给 ``reader_scope`` 看得到的。

        给了 ``target`` 就先按它的标签缩小范围：有关键词按意思排，没有就列最近的。
        """
        kind = kind if kind in MEMORY_KINDS else "video"
        keyword = str(keyword or "").strip()[:60]
        target = str(target or "").strip()[:60]
        if not keyword and not target:
            raise ToolMiss("需要关键词或 target。")
        target_tag = self._memory_target(kind, target) if target else None
        parts = []
        if kind == "bangumi":
            for sid, record in (self.bangumi_memory.all() or {}).items():
                if not isinstance(record, dict):
                    continue
                if target_tag:
                    if target_tag != tag(BANGUMI, f"ss{record.get('season_id') or sid}"):
                        continue
                elif keyword.casefold() not in str(record.get("title", "")).casefold():
                    continue
                lines = [f"《{record.get('title')}》已看{record.get('total_watched', 0)}集，最新第{record.get('last_ep_index', '?')}话，"
                         f"评分{record.get('last_score', '?')}"]
                lines.extend(f"  第{ep.get('ep_index', '?')}话 {ep.get('mood', '')} {ep.get('review', '')}"
                             for ep in (record.get("episodes") or [])[-5:])
                parts.append("\n".join(lines))
                if len(parts) >= 3:
                    break
        recall = self.memory_recall
        if target_tag and not keyword:
            results = await recall._recent_memories(tag=target_tag, memory_types={kind}, limit=5, reader_scope=reader_scope)
        else:
            # 已经按对象缩小了范围就只排先后，不再按分数挡掉
            results = await recall._search_memories(
                keyword, limit=5, memory_types={kind}, score_threshold=0 if target_tag else 0.5,
                reader_scope=reader_scope, tag=target_tag)
        parts.extend(f"- {row}" for row in results)
        if not parts:
            about = "、".join(x for x in (target, keyword) if x)
            raise ToolMiss(f"记忆里没有和“{about}”有关的{MEMORY_KINDS[kind]}。")
        return "\n".join(parts)
