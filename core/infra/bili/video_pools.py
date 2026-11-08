"""视频池：解析配置里写的池子和分区，再从 B站对应的榜单拉候选。"""
from __future__ import annotations

import random
import re

from astrbot.api import logger

from ...config import BILI_ZONES
from .base import BiliApi


class BiliVideoPools(BiliApi):
    VIDEO_POOL_ALIASES = {
        "popular": "popular", "hot": "popular", "热门": "popular", "综合热门": "popular",
        "rcmd": "rcmd", "recommend": "rcmd", "推荐": "rcmd", "首页推荐": "rcmd", "个性推荐": "rcmd",
        "weekly": "weekly", "每周必看": "weekly", "周必看": "weekly",
        "precious": "precious", "入站必刷": "precious", "必刷": "precious",
        "ranking": "ranking", "rank": "ranking", "排行": "ranking", "排行榜": "ranking", "分区排行": "ranking",
        "newlist": "newlist", "new": "newlist", "最新": "newlist", "新稿件": "newlist", "分区最新": "newlist",
    }

    @staticmethod
    def _normalize_zone_name(name):
        return re.sub(r"[\s_\-·・/\\]+", "", str(name or "").lower())

    @staticmethod
    def _split_pool_spec(raw):
        text = str(raw or "").strip()
        for sep in (":", "："):
            if sep in text:
                left, right = text.split(sep, 1)
                return left.strip(), right.strip()
        return text, ""

    def _zone_id_maps(self):
        main_map = {"全站": (0, "全站"), "全站排行": (0, "全站")}
        child_map = {}
        id_name = {0: "全站"}
        for rid, zone in BILI_ZONES.items():
            name = zone["name"]
            main_map[self._normalize_zone_name(name)] = (rid, name)
            id_name[rid] = name
            for tid, child_name in zone.get("children", {}).items():
                child_map[self._normalize_zone_name(child_name)] = (tid, child_name)
                id_name[tid] = child_name
        return main_map, child_map, id_name

    def _lookup_zone_id(self, name, prefer="main"):
        main_map, child_map, _ = self._zone_id_maps()
        key = self._normalize_zone_name(name)
        maps = (child_map, main_map) if prefer == "child" else (main_map, child_map)
        for zone_map in maps:
            if key in zone_map:
                return zone_map[key]
        return None

    def _parse_video_pool_ids(self, raw_ids, prefer="main"):
        ids = []
        for chunk in re.split(r"[,，、\s]+", str(raw_ids or "")):
            item = chunk.strip()
            if not item:
                continue
            if item.isdigit():
                ids.append(int(item))
                continue
            matched = self._lookup_zone_id(item, prefer=prefer)
            if matched:
                ids.append(matched[0])
            else:
                logger.warning(f"[BiliBot] 未识别的视频池分区：{item}，可用 /bili分区 查看中文名称")
        return ids

    def _resolve_video_pool_spec(self, pool_raw):
        raw = str(pool_raw or "").strip()
        if not raw:
            return "popular", [], "popular"
        prefix, value = self._split_pool_spec(raw)
        alias = self.VIDEO_POOL_ALIASES.get(self._normalize_zone_name(prefix))
        if alias in ("popular", "rcmd", "weekly", "precious"):
            return alias, [], raw
        if alias == "ranking":
            ids = self._parse_video_pool_ids(value, prefer="main") if value else [0]
            return "ranking", ids or [0], raw
        if alias == "newlist":
            ids = self._parse_video_pool_ids(value, prefer="child") if value else []
            return "newlist", ids, raw
        if prefix.isdigit():
            return "ranking", [int(prefix)], raw
        main_map, child_map, _ = self._zone_id_maps()
        key = self._normalize_zone_name(prefix)
        if key in main_map:
            return "ranking", [main_map[key][0]], raw
        if key in child_map:
            return "newlist", [child_map[key][0]], raw
        return "unknown", [], raw

    def _format_resolved_video_pool(self, pool, ids, raw):
        _, _, id_name = self._zone_id_maps()
        if pool == "ranking":
            names = ",".join(id_name.get(i, str(i)) for i in (ids or [0]))
            return f"{raw}→排行:{names}" if str(raw) != f"ranking:{','.join(map(str, ids or [0]))}" else raw
        if pool == "newlist":
            names = ",".join(id_name.get(i, str(i)) for i in ids)
            return f"{raw}→最新:{names}" if names else f"{raw}→最新:未指定"
        return str(raw)

    def _format_video_pool_config(self):
        pools = self.settings.get("PROACTIVE_VIDEO_POOLS")
        if not pools:
            pools = ["popular"]
        parts = []
        for pool_raw in pools:
            pool, ids, raw = self._resolve_video_pool_spec(pool_raw)
            parts.append(self._format_resolved_video_pool(pool, ids, raw))
        return "、".join(parts)

    def _build_tname_to_tid_map(self):
        """从 BILI_ZONES 构建 tname→tid 反向映射。"""
        from ...config import BILI_ZONES
        m = {}
        for rid, zone in BILI_ZONES.items():
            m[zone["name"]] = rid
            for tid, name in zone.get("children", {}).items():
                m[name] = tid
        return m

    async def _get_hot_videos(self, min_pubdate=0):
        MIN_VIEWS = 10000
        videos = []
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/popular", params={"ps": 50, "pn": random.randint(1, 5)})
            if d["code"] == 0:
                for v in d.get("data", {}).get("list", []):
                    play = int(v.get("stat", {}).get("view", 0) or 0)
                    pubdate = v.get("pubdate", 0)
                    if play >= MIN_VIEWS and pubdate >= min_pubdate:
                        videos.append({"bvid": v.get("bvid", ""), "title": v.get("title", ""), "desc": v.get("desc", ""), "up_name": v.get("owner", {}).get("name", ""), "up_mid": v.get("owner", {}).get("mid", 0), "pubdate": pubdate, "pic": v.get("pic", ""), "view": play, "tname": v.get("tname", "")})
                logger.info(f"[BiliBot] 🔥 热门API返回 {len(videos)} 个符合条件的视频")
            else:
                logger.warning(f"[BiliBot] 热门API返回非0: code={d['code']}")
        except Exception as e:
            logger.warning(f"[BiliBot] 热门API失败: {e}")
        return videos

    async def _get_newlist_videos(self, tid, min_pubdate=0):
        MIN_VIEWS = 10000
        videos = []
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/newlist", params={"rid": tid, "ps": 50, "pn": 1, "type": 0})
            if d["code"] == 0:
                for v in d.get("data", {}).get("archives", []):
                    play = int(v.get("stat", {}).get("view", 0) or 0)
                    pubdate = v.get("pubdate", 0)
                    if play >= MIN_VIEWS and pubdate >= min_pubdate:
                        videos.append({"bvid": v["bvid"], "title": v["title"], "desc": v.get("desc", ""), "up_name": v["owner"]["name"], "up_mid": v["owner"]["mid"], "pubdate": pubdate, "pic": v.get("pic", ""), "view": play, "tname": v.get("tname", "")})
            else:
                logger.warning(f"[BiliBot] newlist返回非0: code={d['code']} tid={tid}")
        except Exception as e:
            logger.warning(f"[BiliBot] newlist失败: {e}")
        seen = set()
        unique = []
        for v in videos:
            if v["bvid"] and v["bvid"] not in seen:
                seen.add(v["bvid"])
                unique.append(v)
        unique.sort(key=lambda x: x.get("view", 0), reverse=True)
        return unique

    async def _get_weekly_videos(self):
        videos = []
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/popular/series/list", params={"page_size": 1, "page_number": 1})
            if d["code"] != 0:
                return videos
            series_list = d.get("data", {}).get("list", [])
            if not series_list:
                return videos
            latest_number = series_list[0].get("number", 1)
            d2, _ = await self._http_get("https://api.bilibili.com/x/web-interface/popular/series/one", params={"number": latest_number})
            if d2["code"] == 0:
                for v in d2.get("data", {}).get("list", []):
                    videos.append({"bvid": v.get("bvid", ""), "title": v.get("title", ""), "desc": v.get("desc", ""), "up_name": v.get("owner", {}).get("name", ""), "up_mid": v.get("owner", {}).get("mid", 0), "pubdate": v.get("pubdate", 0), "pic": v.get("pic", ""), "view": int(v.get("stat", {}).get("view", 0) or 0), "tname": v.get("tname", "")})
                logger.info(f"[BiliBot] 📅 每周必看第{latest_number}期：{len(videos)} 个视频")
        except Exception as e:
            logger.warning(f"[BiliBot] 每周必看API失败: {e}")
        return videos

    async def _get_precious_videos(self):
        videos = []
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/popular/precious", params={"page_size": 50, "page": 1})
            if d["code"] == 0:
                for v in d.get("data", {}).get("list", []):
                    videos.append({"bvid": v.get("bvid", ""), "title": v.get("title", ""), "desc": v.get("desc", ""), "up_name": v.get("owner", {}).get("name", ""), "up_mid": v.get("owner", {}).get("mid", 0), "pubdate": v.get("pubdate", 0), "pic": v.get("pic", ""), "view": int(v.get("stat", {}).get("view", 0) or 0), "tname": v.get("tname", "")})
                logger.info(f"[BiliBot] 💎 入站必刷：{len(videos)} 个视频")
        except Exception as e:
            logger.warning(f"[BiliBot] 入站必刷API失败: {e}")
        return videos

    async def _get_ranking_videos(self, rid=0):
        videos = []
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/ranking/v2", params={"rid": rid, "type": "all"})
            if d["code"] == 0:
                for v in d.get("data", {}).get("list", []):
                    videos.append({"bvid": v.get("bvid", ""), "title": v.get("title", ""), "desc": v.get("desc", ""), "up_name": v.get("owner", {}).get("name", ""), "up_mid": v.get("owner", {}).get("mid", 0), "pubdate": v.get("pubdate", 0), "pic": v.get("pic", ""), "view": int(v.get("stat", {}).get("view", 0) or 0), "tname": v.get("tname", "")})
                logger.info(f"[BiliBot] 🏆 排行榜(rid={rid})：{len(videos)} 个视频")
        except Exception as e:
            logger.warning(f"[BiliBot] 排行榜API失败: {e}")
        return videos

    async def _get_rcmd_videos(self):
        """从B站首页推荐获取视频（基于登录账号的个性化推荐）。"""
        videos = []
        try:
            d, _ = await self._http_get(
                "https://api.bilibili.com/x/web-interface/index/top/rcmd",
                # ps 超过 14 该接口直接返回 -400，取 12 留出余量
                params={"fresh_type": 4, "ps": 12, "fresh_idx": random.randint(1, 20),
                         "fresh_idx_1h": random.randint(1, 10), "version": 1},
            )
            if d.get("code") == 0:
                for v in d.get("data", {}).get("item", []):
                    if v.get("goto") != "av":
                        continue  # 跳过广告/直播等
                    videos.append({
                        "bvid": v.get("bvid", ""),
                        "title": v.get("title", ""),
                        "desc": v.get("desc", ""),
                        "up_name": v.get("owner", {}).get("name", ""),
                        "up_mid": v.get("owner", {}).get("mid", 0),
                        "pubdate": v.get("pubdate", 0),
                        "pic": v.get("pic", ""),
                        "view": int(v.get("stat", {}).get("view", 0) or 0),
                        "tname": v.get("tname", ""),
                    })
                logger.info(f"[BiliBot] 🏠 首页推荐：{len(videos)} 个视频")
            else:
                logger.warning(f"[BiliBot] 首页推荐API返回非0: code={d.get('code')}")
        except Exception as e:
            logger.warning(f"[BiliBot] 首页推荐API失败: {e}")
        return videos

    async def _get_pool_videos(self, min_pubdate=0, pools=None):
        """``pools``：只取这几个（每日计划给这段定了池子时）；不填就是配置里的全部。"""
        pools = pools or self.settings.get("PROACTIVE_VIDEO_POOLS")
        if not pools:
            pools = ["popular"]
        all_videos = []
        resolved_sources = []
        for pool_raw in pools:
            pool, ids, raw = self._resolve_video_pool_spec(pool_raw)
            resolved_sources.append(self._format_resolved_video_pool(pool, ids, raw))
            if pool == "popular":
                all_videos.extend(await self._get_hot_videos(min_pubdate))
            elif pool == "weekly":
                all_videos.extend(await self._get_weekly_videos())
            elif pool == "precious":
                all_videos.extend(await self._get_precious_videos())
            elif pool == "rcmd":
                all_videos.extend(await self._get_rcmd_videos())
            elif pool == "ranking":
                for rid in (ids or [0]):
                    all_videos.extend(await self._get_ranking_videos(rid))
            elif pool == "newlist":
                if not ids:
                    logger.warning("[BiliBot] 最新分区需要指定中文分区或 tid，如 最新:单机游戏 / newlist:17")
                for tid in ids:
                    all_videos.extend(await self._get_newlist_videos(tid, min_pubdate))
            else:
                logger.warning(f"[BiliBot] 未知视频池: {raw}，可填 热门/推荐/排行榜:游戏/最新:单机游戏")
        logger.info(f"[BiliBot] 📦 视频池合计: {len(all_videos)} 个（来源: {', '.join(resolved_sources)}）")
        return all_videos
