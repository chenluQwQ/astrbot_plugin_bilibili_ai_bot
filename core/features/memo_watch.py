"""到点去看排上的备忘：备忘 → 候选视频或 season_id，看完标做完。

主动看片（``proactive_browse``）和追番（``bangumi``）到点时如果这一格带着备忘就来这里拿东西。
拿不到（视频没了、番名搜不到）备忘就留着，第二天放回待办；视频已经看过的直接标做完。
"""
from __future__ import annotations

import re

from astrbot.api import logger
from ..domain.memos import memo_key
from ..infra.host_view import HostView
from ..infra.tool_loop import ToolMiss


class MemoWatch(HostView):
    HOST_API = frozenset({"bili_bangumi", "bili_lookups", "bili_users", "bili_videos", "memo_tool", "video_discovery"})

    def _memo(self, memo_id):
        if memo_id is None or not self.memo_tool.enabled():
            return None
        memo = self.memo_tool._memos().get(memo_id)
        return memo if memo and memo["state"] in ("pending", "scheduled") else None

    def finish(self, memo_id, result):
        if memo_id is not None and self.memo_tool._memos().finish(memo_id, result):
            logger.info(f"[BiliBot] 📝 备忘 #{memo_id} 做完了：{result}")

    async def video_candidates(self, memo_id, watched_bvids, limit):
        """视频备忘 → 那一个视频；UP 备忘 → 他最近的几条投稿。都打上 ``memo`` 来源和备忘编号。"""
        memo = self._memo(memo_id)
        if memo is None or memo["kind"] not in ("video", "up"):
            return []
        if memo["kind"] == "video":
            bvid = memo_key("video", memo["target"])
            if bvid in watched_bvids:
                self.finish(memo_id, "之前已经看过了")
                return []
            oid = await self.bili_videos._get_video_oid(bvid)
            info = await self.bili_videos._get_video_info(oid) if oid else None
            if not info:
                logger.warning(f"[BiliBot] 📝 备忘 #{memo_id} 的视频 {bvid} 拿不到，今天先不看")
                return []
            videos = [{"bvid": info["bvid"] or bvid, "title": info.get("title", ""), "desc": info.get("desc", ""),
                       "up_name": info.get("owner_name", ""), "up_mid": info.get("owner_mid", ""),
                       "pic": info.get("pic", "")}]
        else:
            try:
                mid, _header, _note = await self.bili_lookups.resolve_up(memo["target"])
            except ToolMiss as miss:
                logger.warning(f"[BiliBot] 📝 备忘 #{memo_id} 的UP主找不到：{miss.detail}")
                return []
            recent = await self.bili_users.get_up_recent_videos(mid, ps=max(3, limit + 2), order="pubdate") or []
            videos = [{**video, "up_mid": mid, "up_name": memo["target"]} for video in recent
                      if video.get("bvid") and video["bvid"] not in watched_bvids][:limit]
            if not videos:
                self.finish(memo_id, "这位UP最近的投稿都看过了")
                return []
        tagged = []
        for video in videos:
            video = self.video_discovery._tag_video_source(video, "memo", f"#{memo_id}")
            video["_memo_id"] = memo_id
            tagged.append(video)
        return tagged

    async def bangumi_season(self, memo_id):
        """番剧备忘 → season_id；搜不到返回 None（照常挑番，备忘留着）。"""
        memo = self._memo(memo_id)
        if memo is None or memo["kind"] != "bangumi":
            return None
        named = re.search(r"\bss(\d+)\b", memo["target"])  # Bot 自己记的（从第一季看起）带着 season_id
        if named:
            return int(named.group(1))
        results = await self.bili_bangumi.search_bilibili_bangumi(memo["target"], ps=1) or []
        season_id = results[0].get("season_id") if results else None
        if not season_id:
            logger.warning(f"[BiliBot] 📝 备忘 #{memo_id} 的番剧《{memo['target']}》没搜到，今天照常挑番")
        return season_id
