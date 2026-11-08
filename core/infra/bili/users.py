"""用户：关注列表、关注、搜索、主页信息和投稿、直播。"""
from __future__ import annotations

from astrbot.api import logger

from ...runtime import WriteResultUnknownError
from .base import BiliApi


class BiliUsers(BiliApi):
    async def get_followings(self, mid=None):
        target = mid or self.settings.get("DEDE_USER_ID")
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/relation/followings", params={"vmid": target, "ps": 50, "pn": 1})
            if d["code"] == 0:
                return [i["mid"] for i in d.get("data", {}).get("list", [])]
        except Exception as e:
            logger.error(f"[BiliBot] 获取关注列表失败: {e}")
        return []

    async def _follow_user(self, mid):
        try:
            d, _ = await self._http_post("https://api.bilibili.com/x/relation/modify", data={"fid": mid, "act": 1, "re_src": 11, "csrf": self.settings.get("BILI_JCT")})
            code = d.get("code", -1)
            if code == 0:
                return True
            if code == -101:
                logger.warning("[BiliBot] 关注失败：Cookie 已失效")
            elif code == -403:
                logger.warning("[BiliBot] 关注失败：访问被限制（可能触发风控）")
            else:
                logger.warning(f"[BiliBot] 关注失败({mid}): code={code} {d.get('message', '')}")
            return False
        except WriteResultUnknownError:
            # 请求可能已经到达B站；交给统一运行时记录 unknown，禁止自动重试。
            raise
        except Exception as e:
            logger.warning(f"[BiliBot] 关注异常({mid}): {e}")
            return False

    async def search_bilibili_users(self, keyword, ps=3):
        """搜索B站用户/UP主"""
        try:
            d, _ = await self._wbi_get("https://api.bilibili.com/x/web-interface/wbi/search/type", {
                "keyword": keyword, "search_type": "bili_user",
                "page": 1, "page_size": ps,
            })
            if d.get("code") != 0:
                return []
            results = []
            for u in (d.get("data") or {}).get("result", [])[:ps]:
                results.append({
                    "mid": u.get("mid", ""),
                    "uname": u.get("uname", ""),
                    "fans": u.get("fans", 0),
                    "videos": u.get("videos", 0),
                    "sign": u.get("usign", "")[:80],
                    "level": u.get("level", 0),
                })
            return results
        except Exception as e:
            logger.error(f"[BiliBot] 搜索用户异常: {e}")
            return []

    async def get_up_info(self, mid):
        """获取UP主详细信息"""
        try:
            d, _ = await self._wbi_get("https://api.bilibili.com/x/space/wbi/acc/info", {"mid": mid})
            if d.get("code") != 0:
                return None
            data = d.get("data") or {}
            return {
                "mid": data.get("mid"),
                "name": data.get("name", ""),
                "sign": data.get("sign", ""),
                "level": data.get("level", 0),
                "fans_badge": data.get("fans_badge", False),
                "official_title": (data.get("official") or {}).get("title", ""),
                "vip_label": (data.get("vip") or {}).get("label", {}).get("text", ""),
                "live_status": (data.get("live_room") or {}).get("liveStatus", 0),
                "live_title": (data.get("live_room") or {}).get("title", ""),
                "live_url": (data.get("live_room") or {}).get("url", ""),
            }
        except Exception as e:
            logger.error(f"[BiliBot] 获取UP主信息失败: {e}")
            return None

    async def get_up_recent_videos(self, mid, ps=5, order="pubdate"):
        """获取UP主的N个视频：``order`` 是 pubdate（最新）或 click（播放最多）。"""
        order = order if order in ("pubdate", "click") else "pubdate"
        try:
            d, _ = await self._wbi_get("https://api.bilibili.com/x/space/wbi/arc/search", {
                "mid": mid, "ps": ps, "pn": 1, "order": order,
            })
            if d.get("code") != 0:
                return []
            vlist = (d.get("data") or {}).get("list", {}).get("vlist", [])
            results = []
            for v in vlist[:ps]:
                results.append({
                    "bvid": v.get("bvid", ""),
                    "title": v.get("title", ""),
                    "desc": v.get("description", "")[:80],
                    "play": v.get("play", 0),
                    "created": v.get("created", 0),
                })
            return results
        except Exception as e:
            logger.error(f"[BiliBot] 获取UP主视频列表失败: {e}")
            return []

    async def _get_up_latest_video(self, mid):
        try:
            d, _ = await self._wbi_get("https://api.bilibili.com/x/space/wbi/arc/search", {"mid": mid, "ps": 1, "pn": 1, "order": "pubdate"})
            if d.get("code") != 0:
                return None
            vlist = d.get("data", {}).get("list", {}).get("vlist", [])
            if not vlist:
                return None
            v = vlist[0]
            return {"bvid": v["bvid"], "title": v["title"], "desc": v.get("description", ""), "up_name": v["author"], "up_mid": mid, "pubdate": v["created"], "pic": v.get("pic", "")}
        except Exception as e:
            logger.error(f"[BiliBot] 获取UP主最新视频失败: {e}")
            return None

    async def get_following_live(self):
        """查看关注的人谁在直播"""
        try:
            d, _ = await self._http_get(
                "https://api.live.bilibili.com/xlive/web-ucenter/v1/xfetter/FeedList",
                params={"page": 1, "pagesize": 20},
            )
            if not isinstance(d, dict) or d.get("code") != 0:
                logger.debug(f"[BiliBot] 直播列表获取失败: {d.get('code') if isinstance(d, dict) else type(d)}")
                return []
            results = []
            for item in ((d.get("data") or {}).get("list") or []):
                results.append({
                    "uname": item.get("uname", ""),
                    "mid": str(item.get("uid", "")),
                    "title": item.get("title", ""),
                    "room_id": item.get("roomid", ""),
                    "area_name": item.get("area_v2_name", "") or item.get("area_name", ""),
                    "online": item.get("online", 0),
                    "link": f"https://live.bilibili.com/{item.get('roomid', '')}",
                })
            return results
        except Exception as e:
            logger.error(f"[BiliBot] 获取关注直播列表失败: {e}")
            return []

    async def _block_user(self, mid):
        try:
            d, _ = await self._http_post(
                "https://api.bilibili.com/x/relation/modify",
                data={"fid": mid, "act": 5, "re_src": 11, "csrf": self.settings.get("BILI_JCT")},
                retries=0,
            )
            return d["code"] == 0
        except WriteResultUnknownError:
            raise
        except Exception:
            return False

    async def _unblock_user(self, mid):
        """解除拉黑。出错不吞：调用方自己决定怎么说。"""
        d, _ = await self._http_post("https://api.bilibili.com/x/relation/modify", data={"fid": mid, "act": 6, "re_src": 11, "csrf": self.settings.get("BILI_JCT")})
        return d["code"] == 0
