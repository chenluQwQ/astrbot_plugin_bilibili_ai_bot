"""账号和运行：扫码登录、启停、刷新 Cookie、记下 UMO、QQ 和B站 UID 绑定。"""
from __future__ import annotations

import asyncio
import io
import os

from astrbot.api.message_components import Image, Plain
from ..config import DATA_DIR


async def login(app, event):
    qr_url, qrcode_key = await app.qr_login_generate()
    if not qr_url:
        yield event.plain_result("❌ 生成二维码失败")
        return
    app.login_qrcode_key = qrcode_key
    import qrcode
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    qr_path = os.path.join(DATA_DIR, "login_qr.png")
    with open(qr_path, "wb") as f:
        f.write(buf.getvalue())
    yield event.chain_result([Plain("📱 请用B站APP扫描下方二维码：\n扫码后发送 /bili确认"), Image.fromFileSystem(qr_path)])


async def login_confirm(app, event):
    if not app.login_qrcode_key:
        yield event.plain_result("❌ 没有待确认的登录")
        return
    for i in range(3):
        code,msg,cookies = await app.qr_login_poll(app.login_qrcode_key)
        if code==0:
            for k,ck in [("SESSDATA","SESSDATA"),("BILI_JCT","bili_jct"),("DEDE_USER_ID","DedeUserID"),("REFRESH_TOKEN","REFRESH_TOKEN")]:
                if cookies.get(ck):
                    app.config[k]=cookies[ck]
            app.config.save_config()
            app.login_qrcode_key=None
            valid,info=await app.check_cookie()
            yield event.plain_result(f"✅ 登录成功！\n{info}")
            if not app.is_running():
                await app.start()
                yield event.plain_result("🚀 后台任务已自动启动")
            return
        elif code==86090:
            yield event.plain_result(f"📱 {msg}")
            await asyncio.sleep(2)
        elif code==86101:
            yield event.plain_result(f"⏳ {msg}")
            await asyncio.sleep(3)
        elif code==86038:
            app.login_qrcode_key=None
            yield event.plain_result(f"❌ {msg}，请重新 /bili登录")
            return
        else:
            yield event.plain_result(f"❌ {msg}")
            return
    yield event.plain_result("⏳ 还没确认成功，请在手机上确认后再发 /bili确认")


async def start(app, event):
    if app.is_running():
        yield event.plain_result("⚠️ 已在运行")
        return
    if not app.has_cookie():
        yield event.plain_result("❌ 请先 /bili登录")
        return
    await app.start()
    yield event.plain_result("🚀 已启动！")


async def stop(app, event):
    if not app.is_running():
        yield event.plain_result("⚠️ 没在运行")
        return
    await app.stop()
    yield event.plain_result("⏹️ 已停止")


async def refresh_cookie(app, event):
    yield event.plain_result("🔄 刷新中...")
    _,msg=await app.refresh_cookie()
    yield event.plain_result(msg)


async def umo(app, event):
    umo = event.unified_msg_origin
    saved = []
    if not (app.settings.get("ABUSE_ALERT_QQ_UMO") or "").strip():
        app.config["ABUSE_ALERT_QQ_UMO"] = umo
        saved.append("恶意告警")
    if not (app.settings.get("WEEKLY_SUMMARY_QQ_UMO") or "").strip():
        app.config["WEEKLY_SUMMARY_QQ_UMO"] = umo
        saved.append("周总结")
    if not (app.settings.get("OWNER_QQ_UMO") or "").strip():
        app.config["OWNER_QQ_UMO"] = umo
        saved.append("主人跨端活动/分享")
    if saved:
        app.config.save_config()
        yield event.plain_result(f"当前 UMO：{umo}\n✅ 已自动填入：{'、'.join(saved)}的 QQ UMO 配置")
    else:
        yield event.plain_result(f"当前 UMO：{umo}\n（恶意告警和周总结的 UMO 都已有值，未覆盖）")


async def bind(app, event):
    parts = event.message_str.strip().split()
    if len(parts) < 2:
        yield event.plain_result("用法：/bili绑定 <B站UID>")
        return
    bili_uid = parts[1].strip()
    if not bili_uid.isdigit():
        yield event.plain_result("⚠️ B站UID应为数字")
        return
    owner_mid = str(app.settings.get("OWNER_MID") or "").strip()
    if owner_mid and bili_uid == owner_mid:
        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False
        if not is_admin:
            yield event.plain_result("⚠️ 绑定主人UID需要管理员权限，请联系管理员操作")
            return
    qq_id = str(event.get_sender_id())
    bindings = app.qq_bindings()
    bindings[qq_id] = bili_uid
    app.save_qq_bindings(bindings)
    yield event.plain_result(f"✅ 已绑定 QQ:{qq_id} ↔ B站UID:{bili_uid}")


async def unbind(app, event):
    qq_id = str(event.get_sender_id())
    bindings = app.qq_bindings()
    if qq_id not in bindings:
        yield event.plain_result("⚠️ 你还没有绑定B站UID")
        return
    del bindings[qq_id]
    app.save_qq_bindings(bindings)
    yield event.plain_result("✅ 已解除绑定")
