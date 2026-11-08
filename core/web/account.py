"""账号页：登录状态、退出、扫码登录。"""
from __future__ import annotations

import base64
import io
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request
from ..app import app_of
from ._common import _config_value, _failure, _read, _response, _today_entries
from .config_page import PROTECTED_CONFIG_KEYS


async def handle_account_info(plugin: Any):
    if not _config_value(plugin, "SESSDATA", ""):
        return _response({"logged_in": False, "configured": False, "reason": "尚未连接 B站账号"})
    try:
        nav_data, _ = await app_of(plugin).bili_nav()
        if nav_data.get("code") != 0:
            return _response({"logged_in": False, "configured": True, "reason": nav_data.get("message", "Cookie 已失效")})
        user = nav_data.get("data", {})
        reply_log = _today_entries(_read(app_of(plugin).reply_log, []), "time", "timestamp", "created_at")
        memory_stats = app_of(plugin).memory_api.stats() if app_of(plugin).memory_api else {"total": await app_of(plugin).count_memories()}
        return _response({
            "logged_in": True,
            "configured": True,
            "name": user.get("uname", "Bilibili 用户"),
            "uid": str(user.get("mid", _config_value(plugin, "DEDE_USER_ID", ""))),
            "level": int(user.get("level_info", {}).get("current_level", 0)),
            "avatar": user.get("face", ""),
            "reply_count": len(reply_log),
            "comment_reply_count": sum(1 for item in reply_log if item.get("channel") != "private"),
            "private_reply_count": sum(1 for item in reply_log if item.get("channel") == "private"),
            "affection_total": app_of(plugin).affection.total(),
            "memory_count": int(memory_stats.get("total", 0)),
            "running": app_of(plugin).is_running(),
        })
    except Exception as exc:
        logger.warning(f"[BiliBot WebUI] account info unavailable: {exc}")
        return _response({"logged_in": False, "configured": True, "reason": f"账号状态检查失败：{exc}"})


async def handle_account_logout(plugin: Any):
    try:
        for key in PROTECTED_CONFIG_KEYS:
            plugin.config[key] = ""
        plugin.config.save_config()
        logger.info("[BiliBot WebUI] Bilibili account logged out")
        return _response(message="已退出登录并清空 Cookie")
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] logout failed: {exc}")
        return _failure(str(exc), 500)


async def handle_qr_generate(plugin: Any):
    try:
        import qrcode

        qr_url, key = await app_of(plugin).qr_login_generate()
        if not qr_url or not key:
            return _failure("B站未返回二维码，请检查网络连接后重试", 502)
        qr = qrcode.QRCode(version=None, box_size=9, border=2)
        qr.add_data(qr_url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="#1d2433", back_color="#ffffff")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return _response({"image": f"data:image/png;base64,{encoded}", "key": key, "expires_in": 180})
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] QR generation failed: {exc}")
        return _failure(f"二维码生成失败：{exc}", 500)


async def handle_qr_poll(plugin: Any):
    try:
        key = request.query.get("key", "").strip()
        if not key:
            return _failure("缺少二维码 key")
        code, message, cookies = await app_of(plugin).qr_login_poll(key)
        status_map = {0: "success", 86038: "expired", 86090: "scanned", 86101: "waiting"}
        status = status_map.get(code, "error")
        if status == "success":
            mappings = {"SESSDATA": "SESSDATA", "BILI_JCT": "bili_jct", "DEDE_USER_ID": "DedeUserID", "REFRESH_TOKEN": "REFRESH_TOKEN"}
            for config_key, cookie_key in mappings.items():
                if cookies.get(cookie_key):
                    plugin.config[config_key] = cookies[cookie_key]
            plugin.config.save_config()
            if not app_of(plugin).is_running():
                await app_of(plugin).start()
            logger.info("[BiliBot WebUI] QR login succeeded")
        return _response({"status": status, "message": message})
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] QR polling failed: {exc}")
        return _failure(f"登录状态获取失败：{exc}", 500)
