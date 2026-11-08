"""动态：传图、发布，读别人的动态。"""
from __future__ import annotations

import json
import os
import random
import time

import aiohttp

from astrbot.api import logger

from ...config import (
    BILI_DYNAMIC_IMAGE_URL, BILI_DYNAMIC_TEXT_URL, BILI_UPLOAD_IMAGE_URL, USER_AGENT,
)
from ...network import plugin_http_session
from ...runtime import WriteResultUnknownError
from .base import BiliApi


class BiliDynamics(BiliApi):
    async def _upload_image_to_bilibili(self, image_path):
        try:
            with open(image_path, "rb") as f:
                img_data = f.read()
            form = aiohttp.FormData()
            form.add_field('file_up', img_data, filename='image.png', content_type='image/png')
            form.add_field('category', 'daily')
            form.add_field('csrf', self.settings.get("BILI_JCT"))
            headers = {"Cookie": self._headers()["Cookie"], "User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com"}
            async with plugin_http_session(self.config) as s:
                async with s.post(BILI_UPLOAD_IMAGE_URL, headers=headers, data=form, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    result = await r.json()
            if result.get("code") == 0:
                img_info = result["data"]
                logger.info("[BiliBot] 📤 图片上传成功")
                return {"img_src": img_info["image_url"], "img_width": img_info["image_width"], "img_height": img_info["image_height"], "img_size": int(os.path.getsize(image_path))}
            else:
                logger.warning(f"[BiliBot] 图片上传失败: {result}")
                return None
        except Exception as e:
            logger.error(f"[BiliBot] 图片上传异常: {e}")
            return None

    async def _post_dynamic_text(self, text):
        data = {
            "dynamic_id": 0, "type": 4, "rid": 0, "content": text,
            "up_choose_comment": 0, "up_close_comment": 0,
            "extension": '{"emoji_type":1,"from":{"emoji_type":1},"flag_cfg":{}}',
            "at_uids": "", "ctrl": "[]",
            "csrf_token": self.settings.get("BILI_JCT"), "csrf": self.settings.get("BILI_JCT"),
        }
        try:
            result, _ = await self._http_post(BILI_DYNAMIC_TEXT_URL, data=data)
            if result.get("code") == 0:
                logger.info("[BiliBot] ✅ 纯文字动态发送成功")
                return True
            else:
                logger.warning(f"[BiliBot] 动态发送失败: {result}")
                return False
        except WriteResultUnknownError:
            # 请求可能已经到达B站；交给统一运行时记录 unknown，禁止自动重试。
            raise
        except Exception as e:
            logger.error(f"[BiliBot] 动态发送异常: {e}")
            return False

    async def _post_dynamic_with_image(self, text, img_info, image_state=None):
        params = {"csrf": self.settings.get("BILI_JCT")}
        payload = {"dyn_req": {"content": {"contents": [{"raw_text": text, "type": 1, "biz_id": ""}]}, "pics": [img_info], "scene": 2}}
        try:
            headers = {**self._headers(), "Content-Type": "application/json"}
            async with plugin_http_session(self.config) as s:
                async with s.post(BILI_DYNAMIC_IMAGE_URL, params=params, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    result = await r.json(content_type=None)
        except Exception as exc:
            logger.error("[BiliBot] 带图动态请求结果未知，不自动降级或重发")
            raise WriteResultUnknownError(
                f"image dynamic result unknown after {type(exc).__name__}"
            ) from exc
        if not isinstance(result, dict):
            logger.error("[BiliBot] 带图动态响应格式异常，结果未知，不自动降级或重发")
            raise WriteResultUnknownError("image dynamic response was not an object")
        if type(result.get("code")) is not int:
            raise WriteResultUnknownError("image dynamic response has no valid result code")
        if result.get("code") == 0:
            if isinstance(image_state, dict):
                image_state["sent"] = True
            logger.info("[BiliBot] ✅ 带图动态发送成功")
            return True

        # 只有服务器明确拒绝时才允许降级；网络/解析异常绝不能文字重发。
        logger.warning(f"[BiliBot] 带图动态被服务器明确拒绝: {result}，尝试纯文字...")
        return await self._post_dynamic_text(text)

    async def _repost_video(self, aid, text):
        """把视频转发到自己的动态，配一句话（「搬运」）。

        和带图动态同一个接口，scene 5 + ``web_repost_src.revs_id``（视频 dyn_type 8，rid 是 aid），
        照网页端的请求写的。被明确拒绝返回 False，由调用方决定要不要改发纯文字；
        网络或解析出错时结果未知，不重发。
        """
        params = {"csrf": self.settings.get("BILI_JCT")}
        payload = {
            "dyn_req": {
                "content": {"contents": [{"raw_text": text, "type": 1, "biz_id": ""}]},
                "scene": 5, "attach_card": None,
                "upload_id": f"0_{int(time.time())}_{random.randint(1000, 9999)}",
                "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
            },
            "web_repost_src": {"revs_id": {"dyn_type": 8, "rid": int(aid)}},
        }
        try:
            headers = {**self._headers(), "Content-Type": "application/json"}
            async with plugin_http_session(self.config) as s:
                async with s.post(BILI_DYNAMIC_IMAGE_URL, params=params, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    result = await r.json(content_type=None)
        except Exception as exc:
            logger.error("[BiliBot] 转发视频请求结果未知，不自动重发")
            raise WriteResultUnknownError(f"video repost result unknown after {type(exc).__name__}") from exc
        if not isinstance(result, dict) or type(result.get("code")) is not int:
            raise WriteResultUnknownError("video repost response has no valid result code")
        if result.get("code") == 0:
            logger.info("[BiliBot] ✅ 转发视频成功")
            return True
        logger.warning(f"[BiliBot] 转发视频被服务器拒绝: {result}")
        return False

    async def get_up_recent_dynamics(self, mid, limit=5):
        """获取UP主最近的动态"""
        try:
            d, _ = await self._http_get(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                params={
                    "host_mid": mid, "offset": "",
                    "timezone_offset": -480,
                    "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
                },
            )
            if d.get("code") != 0:
                return []
            results = []
            for item in ((d.get("data") or {}).get("items") or [])[:limit]:
                modules = item.get("modules") or {}
                author = modules.get("module_author") or {}
                dynamic = modules.get("module_dynamic") or {}
                desc = (dynamic.get("desc") or {}).get("text", "")
                # opus格式
                major = dynamic.get("major") or {}
                if not desc and major.get("type") == "MAJOR_TYPE_OPUS":
                    opus = major.get("opus") or {}
                    desc = (opus.get("summary") or {}).get("text", "") or opus.get("title", "")
                results.append({
                    "dynamic_id": item.get("id_str", ""),
                    "type": item.get("type", ""),
                    "text": desc[:120] if desc else "",
                    "pub_time": author.get("pub_time", ""),
                })
            return results
        except Exception as e:
            logger.error(f"[BiliBot] 获取UP主动态失败: {e}")
            return []

    async def get_following_updates(self, limit=20):
        """获取关注列表的最新动态流（今日更新）"""
        try:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = 20
            limit = max(1, min(50, limit))
            d, _ = await self._http_get(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all",
                params={
                    "timezone_offset": -480, "type": "all", "offset": "",
                    "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
                },
            )
            if d.get("code") != 0:
                logger.debug(f"[BiliBot] 关注动态流获取失败: code={d.get('code')}")
                return []
            results = []
            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")
            for item in ((d.get("data") or {}).get("items") or [])[:limit]:
                modules = item.get("modules") or {}
                author = modules.get("module_author") or {}
                dynamic = modules.get("module_dynamic") or {}
                # 时间戳
                raw_pub_ts = author.get("pub_ts", 0)
                try:
                    pub_ts = int(float(str(raw_pub_ts).strip())) if raw_pub_ts else 0
                    if pub_ts > 10_000_000_000:  # 兼容毫秒时间戳
                        pub_ts //= 1000
                except (TypeError, ValueError, OverflowError):
                    pub_ts = 0
                if pub_ts:
                    try:
                        pub_date = datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d")
                    except (OSError, OverflowError, ValueError):
                        logger.debug(f"[BiliBot] 已忽略异常动态时间戳: {raw_pub_ts!r}")
                        pub_date = today
                    if pub_date != today:
                        continue  # 只要今天的
                pub_time = author.get("pub_time", "")
                up_name = author.get("name", "")
                up_mid = str(author.get("mid", ""))
                # 动态文字
                desc = (dynamic.get("desc") or {}).get("text", "")
                major = dynamic.get("major") or {}
                major_type = major.get("type", "")
                if not desc and (major_type == "MAJOR_TYPE_OPUS" or "opus" in major):
                    opus = major.get("opus") or {}
                    desc = (opus.get("summary") or {}).get("text", "") or opus.get("title", "")
                # 视频投稿
                video_title = ""
                video_bvid = ""
                if major_type == "MAJOR_TYPE_ARCHIVE":
                    archive = major.get("archive") or {}
                    video_title = archive.get("title", "")
                    video_bvid = archive.get("bvid", "")
                # 直播动态
                live_title = ""
                if major_type in ("MAJOR_TYPE_LIVE", "MAJOR_TYPE_LIVE_RCMD"):
                    live = major.get("live") or major.get("live_rcmd") or {}
                    # live_rcmd 的内容可能嵌套在 content 里（JSON字符串）
                    if "content" in live:
                        try:
                            import json
                            live_content = json.loads(live["content"]) if isinstance(live["content"], str) else live["content"]
                            live_title = live_content.get("title", "") or live_content.get("live_play_info", {}).get("title", "")
                        except Exception:
                            live_title = ""
                    else:
                        live_title = live.get("title", "")
                dyn_type = item.get("type", "")
                # 未识别的类型打日志
                if not desc and not video_title and not live_title:
                    logger.debug(f"[BiliBot] 关注动态无内容: up={up_name} type={dyn_type} major_type={major_type} keys={list(major.keys())}")
                image_urls = []
                opus = major.get("opus") or {}
                image_urls.extend(pic.get("url", "") for pic in (opus.get("pics") or []) if pic.get("url"))
                draw = major.get("draw") or {}
                image_urls.extend(pic.get("src", "") for pic in (draw.get("items") or []) if pic.get("src"))
                results.append({
                    "dynamic_id": str(item.get("id_str") or item.get("id") or ""),
                    "up_name": up_name,
                    "up_mid": up_mid,
                    "type": dyn_type,
                    "major_type": major_type,
                    "text": desc[:500] if desc else "",
                    "image_urls": image_urls[:9],
                    "video_title": video_title,
                    "video_bvid": video_bvid,
                    "live_title": live_title,
                    "pub_time": pub_time,
                })
            return results
        except Exception as e:
            logger.error(f"[BiliBot] 获取关注动态流失败: {e}")
            return []
