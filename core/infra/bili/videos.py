"""视频：信息、字幕、标签、搜索，点赞投币收藏。"""
from __future__ import annotations

import re

from astrbot.api import logger

from ...runtime import WriteResultUnknownError
from .base import BiliApi

_SUBTITLE_HASH_LEN = 32


def _own_subtitle_tracks(tracks, aid, cid, bvid=""):
    """去掉没有地址的轨道和明显属于别的视频的自动字幕。

    自动字幕的地址多数是 ``.../ai_subtitle/prod/{aid}{cid}{哈希}``，开头对不上就是别的视频的。
    AI 翻译轨道（``ai_type=1``）的地址只有 32 位哈希（2026-11-06 服务器实测，哈希算不出来），
    和人工字幕一样验不了，留给后面的归属校验。有的视频只有翻译轨道、没有 ai-zh。
    """
    own = []
    for track in tracks or []:
        url = str(track.get("subtitle_url") or "")
        if not url:
            continue
        name = url.split("?", 1)[0].rsplit("/", 1)[-1].split(".", 1)[0]
        if "/ai_subtitle/" in url and len(name) > _SUBTITLE_HASH_LEN and not name.startswith(f"{aid}{cid}"):
            logger.warning(
                f"[BiliBot] 丢弃别的视频的自动字幕: bvid={bvid} lan={track.get('lan')} "
                f"url={url.split('?', 1)[0].rsplit('/', 1)[-1][:40]}"
            )
            continue
        own.append(track)
    return own


class BiliVideos(BiliApi):
    async def _oid_to_bvid(self, oid):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/view", params={"aid": oid})
            if d["code"] == 0:
                return d["data"].get("bvid", "")
        except Exception:
            pass
        return ""

    async def _get_video_info(self, oid):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/view", params={"aid": oid})
            if d["code"] == 0:
                v = d["data"]
                return {
                    "bvid": v.get("bvid", ""), "title": v.get("title", ""), "desc": v.get("desc", ""),
                    "owner_name": v.get("owner", {}).get("name", ""), "owner_mid": v.get("owner", {}).get("mid", ""),
                    "tname": v.get("tname", ""), "duration": v.get("duration", 0), "pic": v.get("pic", ""),
                    "cid": v.get("cid", 0),
                }
        except Exception as e:
            logger.error(f"[BiliBot] 获取视频信息失败：{e}")
        return None

    async def _get_video_subtitles(self, bvid, cid):
        """获取视频字幕文本。B站偶尔返回别的视频的字幕，见 ``_own_subtitle_tracks``。"""
        if not bvid or not cid:
            return ""
        try:
            aid = await self._get_video_oid(bvid)
            if not aid:
                logger.warning(f"[BiliBot] 字幕跳过：查不到 aid bvid={bvid}")
                return ""
            # 实测（2026-11-01）：不签名的 player/v2 会串别的视频的字幕，wbi/v2 没有串过。
            d, _ = await self._wbi_get(
                "https://api.bilibili.com/x/player/wbi/v2",
                {"aid": aid, "cid": cid},
            )
            if not isinstance(d, dict) or d.get("code") != 0:
                code = d.get("code") if isinstance(d, dict) else "?"
                logger.warning(f"[BiliBot] 字幕接口报错: bvid={bvid} code={code}")
                return ""
            data = d.get("data") or {}
            subtitles = _own_subtitle_tracks(
                data.get("subtitle", {}).get("subtitles", []), aid, cid, bvid
            )
            if not subtitles:
                logger.info(f"[BiliBot] 没有可用字幕: bvid={bvid} need_login={data.get('need_login_subtitle')}")
                return ""
            # 优先中文字幕，并保留轨道信息供日志排查 B站自动字幕错配。
            selected_subtitle = None
            for s in subtitles:
                lan = s.get("lan", "")
                if "zh" in lan or "cn" in lan:
                    selected_subtitle = s
                    break
            if selected_subtitle is None:
                selected_subtitle = subtitles[0]
            sub_url = selected_subtitle["subtitle_url"]
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            # 获取字幕JSON
            sub_data, _ = await self._http_get(sub_url)
            if not isinstance(sub_data, dict):
                return ""
            body = sub_data.get("body", [])
            if not body:
                return ""
            # 拼接字幕文本，限制长度
            lines = [item.get("content", "") for item in body if item.get("content")]
            full_text = " ".join(lines)
            if len(full_text) > 2000:
                full_text = full_text[:2000] + "…（字幕过长已截断）"
            lan = selected_subtitle.get("lan", "?")
            subtitle_id = selected_subtitle.get("id_str") or selected_subtitle.get("id", "?")
            logger.info(
                f"[BiliBot] 📝 获取候选字幕: bvid={bvid} cid={cid} "
                f"lan={lan} id={subtitle_id} {len(lines)}条 {len(full_text)}字"
            )
            return full_text
        except Exception as e:
            logger.warning(f"[BiliBot] 字幕获取失败: bvid={bvid} {e}")
            return ""

    async def _get_video_tags(self, bvid):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/tag/archive/tags", params={"bvid": bvid})
            if d["code"] == 0:
                return [t.get("tag_name", "") for t in d.get("data", []) if t.get("tag_name")]
        except Exception:
            pass
        return []

    async def _get_video_oid(self, bvid):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid})
            if d.get("code") == 0:
                return d["data"]["aid"]
        except Exception:
            pass
        return None

    async def _like_video(self, aid):
        try:
            d, _ = await self._http_post("https://api.bilibili.com/x/web-interface/archive/like", data={"aid": aid, "like": 1, "csrf": self.settings.get("BILI_JCT")})
            code = d.get("code", -1)
            if code == 0:
                return True
            if code == -101:
                logger.warning("[BiliBot] 点赞失败：Cookie 已失效")
            elif code == -403:
                logger.warning("[BiliBot] 点赞失败：访问被限制（可能触发风控）")
            else:
                logger.warning(f"[BiliBot] 点赞失败({aid}): code={code} {d.get('message', '')}")
            return False
        except WriteResultUnknownError:
            # 请求可能已经到达B站；交给统一运行时记录 unknown，禁止自动重试。
            raise
        except Exception as e:
            logger.warning(f"[BiliBot] 点赞异常({aid}): {e}")
            return False

    async def _coin_video(self, aid, num=1):
        try:
            d, _ = await self._http_post("https://api.bilibili.com/x/web-interface/coin/add", data={"aid": aid, "multiply": num, "select_like": 0, "csrf": self.settings.get("BILI_JCT")})
            code = d.get("code", -1)
            if code == 0:
                return True
            if code == -101:
                logger.warning("[BiliBot] 投币失败：Cookie 已失效")
            elif code == -403:
                logger.warning("[BiliBot] 投币失败：访问被限制（可能触发风控）")
            else:
                logger.warning(f"[BiliBot] 投币失败({aid}): code={code} {d.get('message', '')}")
            return False
        except WriteResultUnknownError:
            # 请求可能已经到达B站；交给统一运行时记录 unknown，禁止自动重试。
            raise
        except Exception as e:
            logger.warning(f"[BiliBot] 投币异常({aid}): {e}")
            return False

    async def _get_public_fav_folders(self, mid, limit=3):
        """某人的公开收藏夹（别人的私密收藏夹本来就读不到；保险起见再按 attr 滤一次），最多 ``limit`` 个。"""
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/v3/fav/folder/created/list-all",
                                        params={"up_mid": mid, "type": 2})
            if d.get("code") != 0:
                logger.info(f"[BiliBot] 读 {mid} 的收藏夹失败: code={d.get('code')} {d.get('message', '')}")
                return []
            folders = [f for f in (d.get("data") or {}).get("list") or []
                       if isinstance(f, dict) and not int(f.get("attr") or 0) & 1 and int(f.get("media_count") or 0)]
            return [{"id": f["id"], "title": str(f.get("title") or ""), "count": int(f.get("media_count") or 0)}
                    for f in folders[:limit]]
        except Exception as e:
            logger.warning(f"[BiliBot] 读 {mid} 的收藏夹异常: {e}")
            return []

    async def _get_fav_folder_videos(self, media_id, ps=20):
        """收藏夹里最近收藏的视频（按收藏时间倒序），失效的不要。"""
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/v3/fav/resource/list",
                                        params={"media_id": media_id, "pn": 1, "ps": ps, "order": "mtime",
                                                "platform": "web"})
            if d.get("code") != 0:
                logger.info(f"[BiliBot] 读收藏夹 {media_id} 失败: code={d.get('code')} {d.get('message', '')}")
                return []
            videos = []
            for m in (d.get("data") or {}).get("medias") or []:
                if not isinstance(m, dict) or not m.get("bvid") or str(m.get("title")) == "已失效视频":
                    continue
                videos.append({"bvid": m["bvid"], "title": str(m.get("title") or ""),
                               "up_name": str((m.get("upper") or {}).get("name") or ""),
                               "fav_time": int(m.get("fav_time") or 0)})
            return videos
        except Exception as e:
            logger.warning(f"[BiliBot] 读收藏夹 {media_id} 异常: {e}")
            return []

    async def _fav_video(self, aid):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/v3/fav/folder/created/list-all", params={"up_mid": self.settings.get("DEDE_USER_ID"), "type": 2})
            if d["code"] != 0:
                if d["code"] == -101:
                    logger.warning("[BiliBot] 获取收藏夹失败：Cookie 已失效")
                return False
            fav_list = d.get("data", {}).get("list") or []
            if not fav_list:
                logger.warning("[BiliBot] 收藏夹列表为空，无法收藏")
                return False
            fav_id = fav_list[0]["id"]
            d2, _ = await self._http_post("https://api.bilibili.com/x/v3/fav/resource/deal", data={"rid": aid, "type": 2, "add_media_ids": fav_id, "csrf": self.settings.get("BILI_JCT")})
            code = d2.get("code", -1)
            if code == 0:
                return True
            logger.warning(f"[BiliBot] 收藏失败({aid}): code={code} {d2.get('message', '')}")
            return False
        except WriteResultUnknownError:
            # 请求可能已经到达B站；交给统一运行时记录 unknown，禁止自动重试。
            raise
        except Exception as e:
            logger.warning(f"[BiliBot] 收藏异常({aid}): {e}")
            return False

    async def search_bilibili_videos(self, keyword, ps=5):
        """搜索B站视频，返回视频列表"""
        try:
            d, _ = await self._wbi_get("https://api.bilibili.com/x/web-interface/wbi/search/type", {
                "keyword": keyword, "search_type": "video",
                "page": 1, "page_size": ps, "order": "totalrank",
            })
            if d.get("code") != 0:
                logger.debug(f"[BiliBot] 搜索视频失败: code={d.get('code')} msg={d.get('message')}")
                return []
            results = []
            for v in (d.get("data") or {}).get("result", [])[:ps]:
                title = re.sub(r"<[^>]+>", "", v.get("title", ""))
                results.append({
                    "bvid": v.get("bvid", ""),
                    "title": title,
                    "author": v.get("author", ""),
                    "mid": v.get("mid", ""),
                    "play": v.get("play", 0),
                    "danmaku": v.get("video_review", 0),
                    "desc": v.get("description", "")[:100],
                    "duration": v.get("duration", ""),
                    "pubdate": v.get("pubdate", 0),
                })
            return results
        except Exception as e:
            logger.error(f"[BiliBot] 搜索视频异常: {e}")
            return []
