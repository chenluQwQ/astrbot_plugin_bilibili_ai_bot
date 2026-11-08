"""``/bili日志``：按天看视频、番剧、动态、回复、自主查询的记录；以及只读的测试查询。"""
from __future__ import annotations

from datetime import datetime

from ..chat_text import split_for_chat


async def daily_log(app, event):
    parts = event.message_str.strip().split(maxsplit=2)
    kind = parts[1].strip() if len(parts) >= 2 else ""
    date_given = len(parts) >= 3
    target_date = parts[2].strip() if date_given else datetime.now().strftime("%Y-%m-%d")
    # 兼容旧用法：/bili日志 2026-07-01 → 视频日志
    if kind and kind[0].isdigit():
        kind, target_date, date_given = "视频", kind, True
    renderers = {
        "视频": _render_video_log,
        "番剧": _render_bangumi_log,
        "动态": _render_dynamic_log,
        "回复": _render_reply_log,
        "查询": lambda app, target_date, date_given: app.comment_context_log(target_date, date_given),
    }
    if kind not in renderers:
        yield event.plain_result(
            "📋 日志查询\n"
            "━━━━━━━━━━━━\n"
            "请打全命令选择要看的日志：\n"
            "/bili日志 视频 — 主动看视频&评论记录\n"
            "/bili日志 番剧 — 看番记录\n"
            "/bili日志 动态 — 动态发布记录\n"
            "/bili日志 回复 — 评论回复记录\n"
            "/bili日志 查询 — 自主查询与只读实测记录\n"
            "\n可加日期查往天，如：/bili日志 视频 2026-07-01（默认今天）"
        )
        return
    for chunk in split_for_chat(renderers[kind](app, target_date, date_given)):
        yield event.plain_result(chunk)


def _render_video_log(app, target_date, date_given):
    wl = app.watch_log()
    today_watch = [l for l in wl if l.get("time", "").startswith(target_date)]
    pl = app.proactive_log()
    today_comment = [l for l in pl if l.get("time", "").startswith(target_date) and l.get("type") != "bangumi"]
    if not today_watch and not today_comment:
        return f"📋 {target_date} 没有主动行为记录"
    lines = [f"📋 {target_date} 主动行为日志", "━━━━━━━━━━━━"]
    if today_watch:
        lines.append(f"\n🎬 视频处理记录 {len(today_watch)} 条：")
        for i, w in enumerate(today_watch, 1):
            score = w.get("score", "?")
            actions = " ".join(w.get("actions", [])) or "无互动"
            lines.append(f"  {i}. 「{w.get('title','?')}」")
            lines.append(f"     🔗 bilibili.com/video/{w.get('bvid','')}")
            failed = w.get("evaluation_status") == "failed" or w.get("comment") == "评价失败"
            if failed:
                detail = w.get("diagnostic") or {}
                stage = {"content": "内容不足", "model_request": "模型请求", "evaluation_schema": "评价格式校验", "evaluation_exception": "评价异常"}.get(detail.get("stage"), "评价")
                lines.append(f"     UP:{w.get('up_name','?')} | 未评分 | {stage}")
                lines.append(f"     ⚠️ {detail.get('reason') or '旧记录未保存原因，更新后的新记录会显示详情'}")
                for error in detail.get("media_errors", [])[-3:]:
                    lines.append(f"     ↳ {error.get('stage', '')}: {error.get('reason', '')}")
                if detail.get("missing_fields"):
                    lines.append("     缺少字段: " + ", ".join(detail["missing_fields"]))
                call = detail.get("model_call") or {}
                if call.get("provider") or call.get("model"):
                    lines.append(f"     模型: {call.get('provider') or call.get('model')}")
                if "elapsed_seconds" in detail:
                    lines.append(f"     时间: {detail.get('time', w.get('time', ''))} | 评价耗时: {detail['elapsed_seconds']}秒")
            else:
                lines.append(f"     UP:{w.get('up_name','?')} | {score}分 | {w.get('mood','?')}")
            if w.get("review"): lines.append(f"     📝 {w['review']}")
            lines.append(f"     {actions}")
    if today_comment:
        lines.append(f"\n💬 发了 {len(today_comment)} 条评论：")
        for i, c in enumerate(today_comment, 1):
            lines.append(f"  {i}. 「{c.get('title','?')}」")
            lines.append(f"     💬 {c.get('comment','?')}")
    return "\n".join(lines)


def _render_bangumi_log(app, target_date, date_given):
    log = app.bangumi_watch_log()
    today_log = [l for l in log if l.get("time", "").startswith(target_date)]
    if not today_log:
        return f"🎬 {target_date} 没有看番记录"
    lines = [f"🎬 {target_date} 看番日志（共 {len(today_log)} 集）", "━━━━━━━━━━━━"]
    for i, l in enumerate(today_log, 1):
        t = l.get("time", "?")
        time_part = t.split(" ", 1)[1] if " " in t else t
        lines.append(f"{i}. [{time_part}] 《{l.get('title', '?')}》第{l.get('ep_index', '?')}话")
        if l.get("evaluation_status") == "failed" or l.get("comment") in ("评价失败", "看番出错"):
            lines.append(f"   未评分 | {l.get('comment') or '没看成'}，下次从这集接着看")
            lines.append(f"   ⚠️ {l.get('fail_reason') or '旧记录没存原因，更新后的新记录会写'}")
            continue
        lines.append(f"   ⭐{l.get('score', '?')}/10 {l.get('mood', '')} {l.get('review', '')}")
        if l.get("comment"):
            lines.append(f"   💬 {l['comment']}")
    return "\n".join(lines)


def _render_dynamic_log(app, target_date, date_given):
    log = app.dynamic_log()
    if date_given:
        log = [l for l in log if l.get("time", "").startswith(target_date)]
        if not log:
            return f"📝 {target_date} 没有动态记录"
        header = f"📝 {target_date} 动态记录"
    else:
        if not log:
            return "📝 还没有动态记录"
        header = "📝 最近动态记录"
    lines = [header, "━━━━━━━━━━━━"]
    for i, l in enumerate(log[-10:], 1):
        img = "🖼️" if l.get("has_image") else "📄"
        lines.append(f"{i}. [{l.get('time','')}] {img}")
        lines.append(f"   {l.get('text','')}")
    return "\n".join(lines)


def _render_reply_log(app, target_date, date_given):
    # 优先从独立回复日志读取
    reply_log = app.reply_log()
    today_replies = [r for r in reply_log if r.get("time", "").startswith(target_date)]
    # 兼容旧数据：也从 memory.json 补充
    if not today_replies:
        memory = app.memory_file_entries()
        chats = [m for m in memory if m.get("memory_type") == "chat" and m.get("time", "").startswith(target_date)]
        if not chats:
            return f"💬 {target_date} 没有回复记录"
        lines = [f"💬 {target_date} 回复日志（共 {len(chats)} 条，来自记忆）", "━━━━━━━━━━━━"]
        for i, c in enumerate(chats, 1):
            username = c.get("username", "?")
            t = c.get("time", "?")
            time_part = t.split(" ", 1)[1] if " " in t else t
            text = c.get("text", "")
            user_msg = ""
            bot_reply = ""
            if "说：" in text and "| Bot回复：" in text:
                after_said = text.split("说：", 1)[1]
                parts2 = after_said.split(" | Bot回复：", 1)
                user_msg = parts2[0].strip()
                bot_reply = parts2[1].strip() if len(parts2) > 1 else ""
            else:
                user_msg = text
            lines.append(f"{i}. [{time_part}] {username}")
            lines.append(f"   📨 {user_msg}")
            if bot_reply:
                lines.append(f"   💬 {bot_reply}")
        return "\n".join(lines)
    lines = [f"💬 {target_date} 回复日志（共 {len(today_replies)} 条）", "━━━━━━━━━━━━"]
    for i, r in enumerate(today_replies, 1):
        t = r.get("time", "?")
        time_part = t.split(" ", 1)[1] if " " in t else t
        lines.append(f"{i}. [{time_part}] {r.get('username', '?')}")
        lines.append(f"   📨 {r.get('content', '?')}")
        lines.append(f"   💬 {r.get('reply', '?')}")
        lines.append(f"   💛 好感{'+' if r.get('score_delta', 0) >= 0 else ''}{r.get('score_delta', 0)}")
    return "\n".join(lines)


async def test_query(app, event):
    parts = event.message_str.strip().split(maxsplit=2)
    if len(parts) < 2:
        yield event.plain_result("用法：/bili测试查询 BV号 [要核实的问题]\n使用实际配置的模型和资料接口，会产生正常调用费用；不发送B站评论。")
        return
    for chunk in split_for_chat(await app.test_comment_context(parts[1], parts[2] if len(parts) > 2 else "")):
        yield event.plain_result(chunk)
