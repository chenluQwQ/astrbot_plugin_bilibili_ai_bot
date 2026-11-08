"""登录和 Cookie：buvid、Cookie 检查和刷新、扫码登录。"""
from __future__ import annotations

import json
import re
import time
import aiohttp

from astrbot.api import logger

from ...config import (
    BILI_COOKIE_CONFIRM_URL, BILI_COOKIE_INFO_URL, BILI_COOKIE_REFRESH_URL, BILI_NAV_URL, BILI_QR_GENERATE_URL, BILI_QR_POLL_URL, BILI_RSA_PUBLIC_KEY, USER_AGENT,
)
from ...network import plugin_http_session
from .base import BiliApi


class BiliAuth(BiliApi):
    async def _ensure_buvid(self):
        """获取并缓存 buvid3，减少风控触发概率"""
        if self.settings.get("BUVID3"):
            return
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/frontend/finger/spi")
            if d.get("code") == 0:
                buvid3 = d["data"].get("b_3", "")
                buvid4 = d["data"].get("b_4", "")
                if buvid3:
                    self.config["BUVID3"] = buvid3
                    if buvid4:
                        self.config["BUVID4"] = buvid4
                    self.config.save_config()
                    logger.info(f"[BiliBot] 已获取 buvid3: {buvid3[:16]}...")
        except Exception as e:
            logger.warning(f"[BiliBot] 获取 buvid3 失败（不影响基本功能）: {e}")

    async def check_cookie(self):
        s = self.settings.get("SESSDATA")
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
        # 返回 (True, msg)=需要刷新 / (False, msg)=确认无需刷新 / (None, msg)=检查出错，无法判断
        try:
            d, _ = await self._http_get(BILI_COOKIE_INFO_URL, params={"csrf": self.settings.get("BILI_JCT")})
            if d["code"] != 0:
                return None, f"检查失败: {d.get('message', '')}"
            return (True, "需要刷新") if d["data"].get("refresh", False) else (False, "Cookie 仍然有效")
        except Exception as e:
            return None, f"检查出错: {e}"

    def _generate_correspond_path(self, ts):
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import hashes, serialization
        pk = serialization.load_pem_public_key(BILI_RSA_PUBLIC_KEY.encode())
        return pk.encrypt(
            f"refresh_{ts}".encode(),
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        ).hex()

    async def generate_qr(self):
        """生成扫码登录二维码"""
        try:
            d, _ = await self._http_get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
            if d["code"] == 0:
                return {
                    "url": d["data"]["url"],
                    "key": d["data"]["qrcode_key"],
                }
            else:
                raise Exception(f"生成二维码失败: {d.get('message', d['code'])}")
        except Exception as e:
            logger.error(f"[BiliBot] generate_qr 失败: {e}")
            raise

    async def poll_qr_status(self, key):
        """轮询扫码状态"""
        try:
            d, resp = await self._http_get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": key}
            )
            code = d.get("data", {}).get("code", -1)

            if code == 0:  # 扫码成功
                # 从 Set-Cookie 中提取 Cookie
                cookies = {}
                if resp and hasattr(resp, 'cookies'):
                    for cookie in resp.cookies.jar:
                        cookies[cookie.name] = cookie.value

                return {"status": "success", "data": cookies}
            elif code == 86038:  # 二维码已失效
                return {"status": "expired"}
            elif code == 86090:  # 已扫码但未确认
                return {"status": "scanned"}
            elif code == 86101:  # 未扫码
                return {"status": "waiting"}
            else:
                return {"status": "error", "message": d.get("message", "未知错误")}
        except Exception as e:
            logger.error(f"[BiliBot] poll_qr_status 失败: {e}")
            return {"status": "error", "message": str(e)}

    async def refresh_cookie(self):
        rt = self.settings.get("REFRESH_TOKEN")
        if not rt:
            return False, "没有 REFRESH_TOKEN"
        bjct = self.settings.get("BILI_JCT")
        if not self.settings.get("SESSDATA"):
            return False, "SESSDATA 为空"
        try:
            need, msg = await self.check_need_refresh()
            if need is None:
                return False, f"无法确认是否需要刷新（{msg}），本次跳过"
            if not need:
                return True, msg
            cp = self._generate_correspond_path(int(time.time() * 1000))
            html, _ = await self._http_get_text(f"https://www.bilibili.com/correspond/1/{cp}")
            m = re.search(r'<div\s+id="1-name"\s*>([^<]+)</div>', html)
            if not m:
                return False, "无法提取 refresh_csrf"
            async with plugin_http_session(self.config) as s:
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
            async with plugin_http_session(self.config) as s:
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
