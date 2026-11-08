"""手动让 Bot 做一件事：解析分享的视频、看一次视频、发一条动态、看番。"""
from __future__ import annotations

import asyncio
from datetime import datetime


async def parse(app, event):
    parts = (event.message_str or "").strip().split(maxsplit=1)
    target_text = parts[1].strip() if len(parts) >= 2 else app.collect_share_text(
        event,
        include_reply=True,
    )
    async for result in app.handle_share(
        event,
        text_override=target_text,
        trigger_mode="manual",
        show_errors=True,
    ):
        yield result


async def proactive(app, event):
    if not app.has_cookie():
        yield event.plain_result("❌ 请先 /bili登录")
        return
    if not app.settings.get("ENABLE_PROACTIVE"):
        yield event.plain_result("⚠️ 当前未开启主动看视频功能，请先用 /bili开关 主动")
        return
    if app.run_activity_now("proactive", max_watch=1) is None:
        yield event.plain_result("⏳ 已有主动看视频任务在运行")
        return
    app.record_proactive_trigger({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": "manual_command",
        "scheduled": "bili主动",
        "status": "triggered",
    })
    yield event.plain_result("🎯 已手动触发一次主动看视频")


async def dynamic(app, event):
    if not app.has_cookie():
        yield event.plain_result("❌ 请先 /bili登录")
        return
    yield event.plain_result("📢 正在发布动态...")
    task = app.run_activity_now("dynamic", human_initiated=True)
    if task is None:
        started = False
    else:
        # 停用插件会取消这个任务；等它结束，但别让取消冒到命令处理这边。
        await asyncio.wait({task})
        started = None if task.cancelled() else task.result()
    if started is False:
        yield event.plain_result("⏳ 已有一条动态在发布，本次跳过")
    else:
        yield event.plain_result("📢 动态发布流程已完成，请查看日志")


async def watch_bangumi(app, event):
    if not app.settings.get("ENABLE_BANGUMI"):
        yield event.plain_result("⚠️ 番剧功能未开启（ENABLE_BANGUMI）")
        return
    if not app.has_cookie():
        yield event.plain_result("⚠️ 未登录B站")
        return
    parts = event.message_str.strip().split(maxsplit=1)
    season_id = None
    if len(parts) >= 2 and parts[1].strip().isdigit():
        season_id = int(parts[1].strip())
    if app.run_activity_now("bangumi", season_id=season_id) is None:
        yield event.plain_result("⚠️ 已经在看番了，等这轮看完吧")
        return
    yield event.plain_result(f"🎬 开始看番{'（sid=' + str(season_id) + '）' if season_id else '（随机选番）'}...")
