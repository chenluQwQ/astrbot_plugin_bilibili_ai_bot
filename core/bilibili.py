"""B站 API 交互：Cookie管理、WBI签名、扫码登录、评论、视频信息、互动。"""
import re
import time
import hashlib
import aiohttp
from functools import reduce
from astrbot.api import logger
from .config import (
    BILI_COOKIE_CONFIRM_URL, BILI_COOKIE_INFO_URL, BILI_COOKIE_REFRESH_URL,
    BILI_DYNAMIC_IMAGE_URL, BILI_DYNAMIC_TEXT_URL, BILI_NAV_URL,
    BILI_QR_GENERATE_URL, BILI_QR_POLL_URL, BILI_REPLY_URL,
    BILI_RSA_PUBLIC_KEY, BILI_UPLOAD_IMAGE_URL,
    MIXIN_KEY_ENC_TAB, USER_AGENT,
)
import os


class BilibiliAPIMixin:
    """所有 B站 HTTP API 调用。"""

    # ── Cookie 检查 / 刷新 ──
    async def check_cookie(self):
        s = self.config.get("SESSDATA", "")
        if not s:
            return False, "SESSDATA 为空"
        try:
            d, _ = await self._http_get(BILI_NAV_URL)
            if d["code"] == 0:
                return True, f"✅ {d['data'].get('uname', '?')} (UID:{d['data'].get('mid', '')}) LV{d['data'].get('level_info', {}).get('current_level', 0)}"
            return False, f"❌ Cookie 已失效 (code:{d['code']})"
        except Exception as e:
            return False, f"❌ 检查失败: {e}"

    async def check_need_refresh(self):
        try:
            d, _ = await self._http_get(BILI_COOKIE_INFO_URL, params={"csrf": self.config.get("BILI_JCT", "")})
            if d["code"] != 0:
                return False, f"检查失败: {d.get('message', '')}"
            return (True, "需要刷新") if d["data"].get("refresh", False) else (False, "Cookie 仍然有效")
        except Exception as e:
            return False, f"检查出错: {e}"

    def _generate_correspond_path(self, ts):
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import hashes, serialization
        pk = serialization.load_pem_public_key(BILI_RSA_PUBLIC_KEY.encode())
        return pk.encrypt(
            f"refresh_{ts}".encode(),
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        ).hex()

    async def refresh_cookie(self):
        rt = self.config.get("REFRESH_TOKEN", "")
        if not rt:
            return False, "没有 REFRESH_TOKEN"
        bjct = self.config.get("BILI_JCT", "")
        if not self.config.get("SESSDATA", ""):
            return False, "SESSDATA 为空"
        try:
            need, msg = await self.check_need_refresh()
            if not need:
                return True, msg
            cp = self._generate_correspond_path(int(time.time() * 1000))
            html, _ = await self._http_get_text(f"https://www.bilibili.com/correspond/1/{cp}")
            m = re.search(r'<div\s+id="1-name"\s*>([^<]+)</div>', html)
            if not m:
                return False, "无法提取 refresh_csrf"
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    BILI_COOKIE_REFRESH_URL,
                    headers=self._headers(),
                    data={"csrf": bjct, "refresh_csrf": m.group(1).strip(), "source": "main_web", "refresh_token": rt},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    result = await resp.json(content_type=None)
                    if result["code"] != 0:
                        return False, f"刷新失败: {result.get('message', result['code'])}"
                    updates = {}
                    nrt = result["data"].get("refresh_token", "")
                    if nrt:
                        updates["REFRESH_TOKEN"] = nrt
                    for k, cookie in resp.cookies.items():
                        if k == "SESSDATA":
                            updates["SESSDATA"] = cookie.value
                        elif k == "bili_jct":
                            updates["BILI_JCT"] = cookie.value
                        elif k == "DedeUserID":
                            updates["DEDE_USER_ID"] = cookie.value
            if "SESSDATA" not in updates:
                return False, "刷新响应中未找到新 SESSDATA"
            try:
                ch = dict(self._headers())
                ch["Cookie"] = f"SESSDATA={updates['SESSDATA']}; bili_jct={updates.get('BILI_JCT', bjct)}"
                await self._http_post(BILI_COOKIE_CONFIRM_URL, headers=ch, data={"csrf": updates.get("BILI_JCT", bjct), "refresh_token": rt})
            except Exception:
                pass
            for k, v in updates.items():
                self.config[k] = v
            self.config.save_config()
            return True, "✅ Cookie 刷新成功！"
        except Exception as e:
            return False, f"刷新出错: {e}"

    # ── WBI 签名 ──
    async def _get_wbi_keys(self):
        d, _ = await self._http_get(BILI_NAV_URL)
        d = d["data"]["wbi_img"]
        return d["img_url"].rsplit("/", 1)[1].split(".")[0], d["sub_url"].rsplit("/", 1)[1].split(".")[0]

    def _get_mixin_key(self, orig):
        return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]

    async def sign_wbi_params(self, params):
        try:
            ik, sk = await self._get_wbi_keys()
            mk = self._get_mixin_key(ik + sk)
            params["wts"] = int(time.time())
            params = dict(sorted(params.items()))
            params["w_rid"] = hashlib.md5(("&".join(f"{k}={v}" for k, v in params.items()) + mk).encode()).hexdigest()
            return params
        except Exception:
            return params

    # ── 扫码登录 ──
    async def _qr_login_generate(self):
        try:
            d, _ = await self._http_get(BILI_QR_GENERATE_URL, headers={"User-Agent": USER_AGENT})
            if d["code"] == 0:
                return d["data"]["url"], d["data"]["qrcode_key"]
        except Exception as e:
            logger.error(f"生成二维码失败: {e}")
        return None, None

    async def _qr_login_poll(self, qrcode_key):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    BILI_QR_POLL_URL,
                    params={"qrcode_key": qrcode_key},
                    headers={"User-Agent": USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    d_full = await resp.json(content_type=None)
                    d = d_full["data"]
                    code = d["code"]
                    mm = {0: "登录成功", 86038: "二维码已失效", 86090: "已扫码，请在手机上确认", 86101: "等待扫码中..."}
                    cookies = {}
                    if code == 0:
                        url = d.get("url", "")
                        rt = d.get("refresh_token", "")
                        if url:
                            from urllib.parse import urlparse, parse_qs
                            p = parse_qs(urlparse(url).query)
                            cookies = {"SESSDATA": p.get("SESSDATA", [""])[0], "bili_jct": p.get("bili_jct", [""])[0], "DedeUserID": p.get("DedeUserID", [""])[0], "REFRESH_TOKEN": rt}
                        for k, cookie in resp.cookies.items():
                            if k in ("SESSDATA", "bili_jct", "DedeUserID"):
                                cookies[k] = cookie.value
                    return code, mm.get(code, f"未知({code})"), cookies
        except Exception as e:
            return -1, f"轮询失败: {e}", {}

    # ── 评论 ──
    async def _send_reply(self, oid, rpid, reply_type, content):
        try:
            d, _ = await self._http_post(
                BILI_REPLY_URL,
                data={"oid": oid, "type": reply_type, "root": rpid, "parent": rpid, "message": content, "csrf": self.config.get("BILI_JCT", "")},
            )
            if d["code"] == 0:
                return True
            elif d["code"] == -101:
                logger.error("[BiliBot] SESSDATA 失效！")
            elif d["code"] == -111:
                logger.error("[BiliBot] bili_jct 错误！")
            else:
                logger.warning(f"[BiliBot] 回复失败: {d.get('message', d['code'])}")
            return False
        except Exception as e:
            logger.error(f"[BiliBot] 回复出错: {e}")
            return False

    def _strip_at_prefix(self, content):
        content = (content or "").strip()
        content = re.sub(r'^@[^ \t\n\r]+\s*', '', content)
        return content.strip()

    async def _send_comment(self, oid, comment_text, oid_type=1):
        try:
            d, _ = await self._http_post(
                BILI_REPLY_URL,
                data={"oid": oid, "type": oid_type, "message": comment_text, "csrf": self.config.get("BILI_JCT", "")},
            )
            return d.get("code") == 0
        except Exception as e:
            logger.error(f"[BiliBot] 发送评论异常: {e}")
            return False

    # ── 关注列表 ──
    async def get_followings(self, mid=None):
        target = mid or self.config.get("DEDE_USER_ID", "")
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/relation/followings", params={"vmid": target, "ps": 50, "pn": 1})
            if d["code"] == 0:
                return [i["mid"] for i in d.get("data", {}).get("list", [])]
        except Exception as e:
            logger.error(f"[BiliBot] 获取关注列表失败: {e}")
        return []

    # ── 视频信息 ──
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
                }
        except Exception as e:
            logger.error(f"[BiliBot] 获取视频信息失败：{e}")
        return None

    async def _get_video_tags(self, bvid):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/tag/archive/tags", params={"bvid": bvid})
            if d["code"] == 0:
                return [t.get("tag_name", "") for t in d.get("data", []) if t.get("tag_name")]
        except Exception:
            pass
        return []

    async def _get_hot_comments(self, oid, limit=10):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/v2/reply/main", params={"oid": oid, "type": 1, "mode": 3, "ps": limit})
            if d["code"] == 0:
                replies = d.get("data", {}).get("replies", []) or []
                return [r.get("content", {}).get("message", "")[:100] for r in replies if r.get("content", {}).get("message")]
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

    # ── 互动 ──
    async def _like_video(self, aid):
        try:
            d, _ = await self._http_post("https://api.bilibili.com/x/web-interface/archive/like", data={"aid": aid, "like": 1, "csrf": self.config.get("BILI_JCT", "")})
            return d.get("code") == 0
        except Exception:
            return False

    async def _coin_video(self, aid, num=1):
        try:
            d, _ = await self._http_post("https://api.bilibili.com/x/web-interface/coin/add", data={"aid": aid, "multiply": num, "select_like": 0, "csrf": self.config.get("BILI_JCT", "")})
            return d.get("code") == 0
        except Exception:
            return False

    async def _fav_video(self, aid):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/v3/fav/folder/created/list-all", params={"up_mid": self.config.get("DEDE_USER_ID", ""), "type": 2})
            if d["code"] != 0:
                return False
            fav_list = d.get("data", {}).get("list") or []
            if not fav_list:
                logger.warning("[BiliBot] 收藏夹列表为空，无法收藏")
                return False
            fav_id = fav_list[0]["id"]
            d2, _ = await self._http_post("https://api.bilibili.com/x/v3/fav/resource/deal", data={"rid": aid, "type": 2, "add_media_ids": fav_id, "csrf": self.config.get("BILI_JCT", "")})
            return d2.get("code") == 0
        except Exception:
            return False

    async def _follow_user(self, mid):
        try:
            d, _ = await self._http_post("https://api.bilibili.com/x/relation/modify", data={"fid": mid, "act": 1, "re_src": 11, "csrf": self.config.get("BILI_JCT", "")})
            return d.get("code") == 0
        except Exception:
            return False

    # ── 图片上传 ──
    async def _upload_image_to_bilibili(self, image_path):
        try:
            with open(image_path, "rb") as f:
                img_data = f.read()
            form = aiohttp.FormData()
            form.add_field('file_up', img_data, filename='image.png', content_type='image/png')
            form.add_field('category', 'daily')
            form.add_field('csrf', self.config.get("BILI_JCT", ""))
            headers = {"Cookie": self._headers()["Cookie"], "User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com"}
            async with aiohttp.ClientSession() as s:
                async with s.post(BILI_UPLOAD_IMAGE_URL, headers=headers, data=form, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    result = await r.json()
            if result.get("code") == 0:
                img_info = result["data"]
                logger.info("[BiliBot] 📤 图片上传成功")
                return {"img_src": img_info["image_url"], "img_width": img_info["image_width"], "img_height": img_info["image_height"], "img_size": os.path.getsize(image_path) / 1024}
            else:
                logger.warning(f"[BiliBot] 图片上传失败: {result}")
                return None
        except Exception as e:
            logger.error(f"[BiliBot] 图片上传异常: {e}")
            return None

    # ── 动态发送 ──
    async def _post_dynamic_text(self, text):
        data = {
            "dynamic_id": 0, "type": 4, "rid": 0, "content": text,
            "up_choose_comment": 0, "up_close_comment": 0,
            "extension": '{"emoji_type":1,"from":{"emoji_type":1},"flag_cfg":{}}',
            "at_uids": "", "ctrl": "[]",
            "csrf_token": self.config.get("BILI_JCT", ""), "csrf": self.config.get("BILI_JCT", ""),
        }
        try:
            result, _ = await self._http_post(BILI_DYNAMIC_TEXT_URL, data=data)
            if result.get("code") == 0:
                logger.info("[BiliBot] ✅ 纯文字动态发送成功")
                return True
            else:
                logger.warning(f"[BiliBot] 动态发送失败: {result}")
                return False
        except Exception as e:
            logger.error(f"[BiliBot] 动态发送异常: {e}")
            return False

    async def _post_dynamic_with_image(self, text, img_info):
        params = {"csrf": self.config.get("BILI_JCT", "")}
        payload = {"dyn_req": {"content": {"contents": [{"raw_text": text, "type": 1, "biz_id": ""}]}, "pics": [img_info], "scene": 2}}
        try:
            headers = {**self._headers(), "Content-Type": "application/json"}
            async with aiohttp.ClientSession() as s:
                async with s.post(BILI_DYNAMIC_IMAGE_URL, params=params, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    result = await r.json()
            if result.get("code") == 0:
                logger.info("[BiliBot] ✅ 带图动态发送成功")
                return True
            else:
                logger.warning(f"[BiliBot] 带图动态失败: {result}，尝试纯文字...")
                return await self._post_dynamic_text(text)
        except Exception as e:
            logger.error(f"[BiliBot] 带图动态异常: {e}，尝试纯文字...")
            return await self._post_dynamic_text(text)

    # ── UP主最新视频 ──
    async def _get_up_latest_video(self, mid):
        try:
            params = await self.sign_wbi_params({"mid": mid, "ps": 1, "pn": 1, "order": "pubdate"})
            d, _ = await self._http_get("https://api.bilibili.com/x/space/wbi/arc/search", params=params)
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
