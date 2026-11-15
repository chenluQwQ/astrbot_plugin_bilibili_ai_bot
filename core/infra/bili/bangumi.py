"""番剧（PGC）：搜索、详情、排行、时间表、追番。"""
from __future__ import annotations

import re

from astrbot.api import logger

from ...runtime import WriteResultUnknownError
from .base import BiliApi


_DANMAKU = re.compile(r'<d p="([^"]*)">([^<]*)</d>')


def parse_danmaku_xml(text: str) -> list[tuple[float, str]]:
    """``list.so`` 返回的 XML → [(出现的秒数, 内容)]，按时间排。坏的条目跳过。"""
    items = []
    for attrs, body in _DANMAKU.findall(str(text or "")):
        try:
            second = float(attrs.split(",", 1)[0])
        except ValueError:
            continue
        body = _unescape(body).strip()
        if body:
            items.append((second, body[:60]))
    items.sort(key=lambda item: item[0])
    return items


def _unescape(text: str) -> str:
    return (text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&#39;", "'").replace("&amp;", "&"))



class BiliBangumi(BiliApi):
    @staticmethod
    def _pgc_ok(d, label="PGC"):
        """检查 PGC API 响应是否为有效 dict 且 code==0。"""
        if not isinstance(d, dict):
            logger.warning(f"[BiliBot] {label}返回非dict: type={type(d).__name__} val={str(d)[:200]}")
            return False
        code = d.get("code", -1)
        if code != 0:
            logger.debug(f"[BiliBot] {label}失败: code={code} msg={d.get('message', '')}")
            return False
        return True

    async def _pgc_get(self, url, params=None, label="PGC"):
        """专用于 PGC API 的 GET：用 text+json.loads 避免 r.json() 的玄学问题。"""
        import json as _json
        try:
            text, _ = await self._http_get_text(url, params=params, timeout=10)
            if not text:
                logger.warning(f"[BiliBot] {label} 返回空响应")
                return None
            d = _json.loads(text)
            if not isinstance(d, dict):
                logger.warning(f"[BiliBot] {label} JSON非dict: type={type(d).__name__} val={str(d)[:200]}")
                return None
            return d
        except _json.JSONDecodeError as e:
            logger.warning(f"[BiliBot] {label} JSON解析失败: {e} text={str(text)[:200]}")
            return None
        except Exception as e:
            logger.warning(f"[BiliBot] {label} 请求失败: {e}")
            return None

    async def search_bilibili_bangumi(self, keyword, ps=5):
        """搜索B站番剧，返回番剧列表。"""
        try:
            d, _ = await self._wbi_get("https://api.bilibili.com/x/web-interface/wbi/search/type", {
                "keyword": keyword, "search_type": "media_bangumi",
                "page": 1, "page_size": ps,
            })
            if not self._pgc_ok(d, "搜索番剧"):
                return []
            results = []
            for item in (d.get("data") or {}).get("result", [])[:ps]:
                title = re.sub(r"<[^>]+>", "", item.get("title", ""))
                score_info = item.get("media_score") or {}
                results.append({
                    "media_id": item.get("media_id", 0),
                    "season_id": item.get("season_id", 0),
                    "title": title,
                    "org_title": item.get("org_title", ""),
                    "season_type_name": item.get("season_type_name", "番剧"),
                    "areas": item.get("areas", ""),
                    "styles": item.get("styles", ""),
                    "cv": item.get("cv", ""),
                    "staff": item.get("staff", ""),
                    "desc": item.get("desc", "")[:150],
                    "score": score_info.get("score", 0),
                    "user_count": score_info.get("user_count", 0),
                    "ep_size": item.get("ep_size", 0),
                    "pubtime": item.get("pubtime", 0),
                    "url": item.get("url", ""),
                    "cover": item.get("cover", ""),
                })
            return results
        except Exception as e:
            logger.error(f"[BiliBot] 搜索番剧异常: {e}")
            return []

    async def get_bangumi_detail(self, season_id=None, ep_id=None):
        """获取番剧详情（剧集列表、评分、简介等）。"""
        try:
            params = {}
            if season_id:
                params["season_id"] = season_id
            elif ep_id:
                params["ep_id"] = ep_id
            else:
                return None
            d = await self._pgc_get(
                "https://api.bilibili.com/pgc/view/web/season", params=params, label="番剧详情",
            )
            if not d or not self._pgc_ok(d, "番剧详情"):
                return None
            result = d.get("result", {})
            if not isinstance(result, dict):
                logger.warning(f"[BiliBot] 番剧详情 result 非dict: type={type(result).__name__} val={str(result)[:200]}")
                return None
            rating = result.get("rating") or {}
            if not isinstance(rating, dict):
                rating = {}
            stat = result.get("stat") or {}
            if not isinstance(stat, dict):
                stat = {}
            episodes = []
            for ep in (result.get("episodes") or []):
                if not isinstance(ep, dict):
                    continue
                episodes.append({
                    "ep_id": ep.get("ep_id") or ep.get("id", 0),
                    "title": ep.get("share_copy", "") or ep.get("long_title", "") or f"第{ep.get('title', '?')}话",
                    "long_title": ep.get("long_title", ""),
                    "ep_index": ep.get("title", ""),
                    "badge": ep.get("badge", ""),
                    "duration": ep.get("duration", 0) // 60000 if ep.get("duration") else 0,
                    "aid": ep.get("aid", 0),
                    "cid": ep.get("cid", 0),
                })
            areas_raw = result.get("areas") or []
            styles_raw = result.get("styles") or []
            return {
                "season_id": result.get("season_id", 0),
                "media_id": result.get("media_id", 0),
                "title": result.get("season_title", "") or result.get("title", ""),
                "evaluate": str(result.get("evaluate", ""))[:300],
                "score": rating.get("score", 0),
                "count": rating.get("count", 0),
                "areas": ", ".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in areas_raw),
                "styles": ", ".join(s.get("name", "") if isinstance(s, dict) else str(s) for s in styles_raw),
                "total_ep": result.get("total", 0),
                "new_ep_desc": (result.get("new_ep") or {}).get("desc", "") if isinstance(result.get("new_ep"), dict) else "",
                "stat_views": stat.get("views", 0),
                "stat_danmakus": stat.get("danmakus", 0),
                "stat_favorites": stat.get("favorites", 0),
                "episodes": episodes,
                # 同系列各季，B站给的顺序（番剧从第一季看起用）
                "seasons": [
                    {"season_id": item.get("season_id", 0),
                     "title": item.get("season_title", "") or item.get("title", "")}
                    for item in (result.get("seasons") or []) if isinstance(item, dict) and item.get("season_id")
                ],
                "link": result.get("link", ""),
                "cover": result.get("cover", ""),
            }
        except Exception as e:
            logger.error(f"[BiliBot] 番剧详情异常: {e}")
            return None

    async def get_bangumi_trending(self, season_type=1, day=3):
        """获取番剧排行榜。season_type: 1=番剧 4=国创 2=电影 3=纪录片。"""
        try:
            # 番剧(1)和国创(4)用不同的API端点
            if season_type == 1:
                url = "https://api.bilibili.com/pgc/web/rank/list"
            else:
                url = "https://api.bilibili.com/pgc/season/rank/web/list"
            d = await self._pgc_get(url, params={"season_type": season_type, "day": day}, label=f"番剧排行(type={season_type})")
            if not d or not self._pgc_ok(d, f"番剧排行(type={season_type})"):
                return []
            items = (d.get("data") or d.get("result") or {}).get("list", [])
            results = []
            for item in items[:15]:
                rating = item.get("rating", "") or ""
                # 评分可能是 "9.9分" 格式，去掉非数字后缀
                rating_clean = re.sub(r'[^\d.]', '', str(rating))
                stat = item.get("stat") or {}
                new_ep = item.get("new_ep") or {}
                results.append({
                    "season_id": item.get("season_id", 0),
                    "title": item.get("title", ""),
                    "badge": item.get("badge", ""),
                    "score": float(rating_clean) if rating_clean else 0,
                    "new_ep_desc": new_ep.get("index_show", ""),
                    "stat_follow": stat.get("follow", 0),
                    "stat_view": stat.get("view", 0),
                    "stat_danmaku": stat.get("danmaku", 0),
                    "url": item.get("url", ""),
                    "cover": item.get("cover", ""),
                })
            logger.info(f"[BiliBot] 🏆 番剧排行(type={season_type}): {len(results)}条")
            return results
        except Exception as e:
            logger.error(f"[BiliBot] 番剧排行异常: {e}")
            return []

    async def get_bangumi_timeline(self, day_before=0, day_after=6):
        """获取番剧时间表（新番放送表）。"""
        # 优先用 v1 API（更稳定），失败回退 v2
        result = await self._get_bangumi_timeline_v1(day_before, day_after)
        if result:
            return result
        return await self._get_bangumi_timeline_v2(day_before, day_after)

    async def _get_bangumi_timeline_v1(self, before, after):
        """v1 API: /pgc/web/timeline，参数 types/before/after。"""
        try:
            d = await self._pgc_get(
                "https://api.bilibili.com/pgc/web/timeline",
                params={"types": 1, "before": before, "after": after}, label="番剧时间表v1",
            )
            if not d or not self._pgc_ok(d, "番剧时间表v1"):
                return []
            results = []
            for day in (d.get("result") or []):
                date = day.get("date", "")
                day_of_week = day.get("day_of_week", 0)
                for ep in day.get("episodes", []):
                    results.append({
                        "date": date, "day_of_week": day_of_week,
                        "season_id": ep.get("season_id", 0),
                        "ep_id": ep.get("episode_id", 0) or ep.get("ep_id", 0),
                        "title": ep.get("title", ""),
                        "ep_index": ep.get("pub_index", "") or ep.get("ep_index", ""),
                        "pub_ts": ep.get("pub_ts", 0),
                        "published": ep.get("published", 0) == 1,
                        "cover": ep.get("cover", ""),
                    })
            if results:
                logger.info(f"[BiliBot] 📅 番剧时间表v1: {len(results)}条")
            return results
        except Exception as e:
            logger.debug(f"[BiliBot] 番剧时间表v1异常: {e}")
            return []

    async def _get_bangumi_timeline_v2(self, day_before, day_after):
        """v2 API: /pgc/web/timeline/v2，需要 season_type 参数。"""
        try:
            d = await self._pgc_get(
                "https://api.bilibili.com/pgc/web/timeline/v2",
                params={"season_type": 1, "day_before": day_before, "day_after": day_after}, label="番剧时间表v2",
            )
            if not d or not self._pgc_ok(d, "番剧时间表v2"):
                return []
            results = []
            timeline = (d.get("data") or d.get("result") or {})
            if isinstance(timeline, dict):
                timeline = timeline.get("timeline", [])
            for day in (timeline or []):
                date = day.get("date", "")
                day_of_week = day.get("day_of_week", 0)
                for ep in day.get("episodes", []):
                    results.append({
                        "date": date, "day_of_week": day_of_week,
                        "season_id": ep.get("season_id", 0),
                        "ep_id": ep.get("episode_id", 0) or ep.get("ep_id", 0),
                        "title": ep.get("title", ""),
                        "ep_index": ep.get("pub_index", "") or ep.get("ep_index", ""),
                        "pub_ts": ep.get("pub_ts", 0),
                        "published": ep.get("published", 0) == 1,
                        "cover": ep.get("cover", ""),
                    })
            if results:
                logger.info(f"[BiliBot] 📅 番剧时间表v2: {len(results)}条")
            return results
        except Exception as e:
            logger.debug(f"[BiliBot] 番剧时间表v2异常: {e}")
            return []

    async def get_danmaku(self, cid):
        """一集（按 cid）的弹幕：[(出现的秒数, 内容)]。公开接口，不用登录；取不到返回空列表。"""
        if not cid:
            return []
        try:
            text, resp = await self._http_get_text(
                "https://api.bilibili.com/x/v1/dm/list.so", params={"oid": int(cid)}, timeout=15, retries=0,
            )
            if getattr(resp, "status", 200) != 200:
                return []
            return parse_danmaku_xml(text)
        except Exception as exc:
            logger.debug(f"[BiliBot] 弹幕取不到，跳过: {exc}")
            return []

    async def _follow_bangumi(self, season_id):
        """追番（点追番按钮）。"""
        try:
            d, _ = await self._http_post(
                "https://api.bilibili.com/pgc/web/follow/add",
                data={"season_id": season_id, "csrf": self.settings.get("BILI_JCT")},
            )
            if isinstance(d, dict) and d.get("code") == 0:
                logger.info(f"[BiliBot] 📌 追番成功: sid={season_id}")
                return True
            logger.debug(f"[BiliBot] 追番失败: {d}")
            return False
        except WriteResultUnknownError:
            raise
        except Exception as e:
            logger.warning(f"[BiliBot] 追番异常: {e}")
            return False

    async def _get_followed_bangumi(self, follow_status=0, page=1, page_size=30):
        """获取已追番列表。follow_status: 0=全部 1=想看 2=在看 3=看过。"""
        try:
            vmid = self.settings.get("DEDE_USER_ID")
            if not vmid:
                logger.warning("[BiliBot] 追番列表需要 DEDE_USER_ID")
                return []
            d, _ = await self._http_get(
                "https://api.bilibili.com/x/space/bangumi/follow/list",
                params={"type": 1, "follow_status": follow_status, "pn": page, "ps": page_size, "vmid": vmid},
            )
            if not isinstance(d, dict) or d.get("code") != 0:
                logger.debug(f"[BiliBot] 追番列表失败: {str(d)[:200]}")
                return []
            data = d.get("data") or {}
            if not isinstance(data, dict):
                return []
            items = data.get("list") or []
            followed = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                new_ep = item.get("new_ep") or {}
                if not isinstance(new_ep, dict):
                    new_ep = {}
                followed.append({
                    "season_id": item.get("season_id", 0),
                    "media_id": item.get("media_id", 0),
                    "title": item.get("title", ""),
                    "new_ep_index": new_ep.get("index_show", ""),
                    "new_ep_id": new_ep.get("id", 0),
                    "total_count": item.get("total_count", 0),
                    "progress": (item.get("progress") or {}).get("last_ep_index", "") if isinstance(item.get("progress"), dict) else "",
                })
            logger.info(f"[BiliBot] 📋 追番列表: {len(followed)}部")
            return followed
        except Exception as e:
            logger.error(f"[BiliBot] 获取追番列表异常: {e}")
            return []
