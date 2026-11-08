"""功能开关和清理临时文件、清理旧日志。"""
from __future__ import annotations

import os


TOGGLE_ITEMS = {
    "回复": "ENABLE_REPLY",
    "查询": "COMMENT_CONTEXT_ENABLED",
    "私信": "ENABLE_PRIVATE_MESSAGES",
    "私信回复": "PRIVATE_MESSAGE_AUTO_REPLY",
    "私信拉黑": "PRIVATE_MESSAGE_AUTO_BLOCK",
    "主动": "ENABLE_PROACTIVE",
    "动态": "ENABLE_DYNAMIC",
    "好感": "ENABLE_AFFECTION",
    "心情": "ENABLE_MOOD",
    "演化": "ENABLE_PERSONALITY_EVOLUTION",
    "工具": "ENABLE_LLM_TOOLS",
    "解析": "ENABLE_BILI_SHARE_PARSE",
    "自动解析": "BILI_SHARE_PARSE_AUTO_TRIGGER_ENABLED",
    "手动解析": "BILI_SHARE_PARSE_MANUAL_TRIGGER_ENABLED",
    "LLM解析": "BILI_SHARE_PARSE_LLM_TRIGGER_ENABLED",
    "解析视频": "BILI_SHARE_PARSE_SEND_VIDEO",
    "筛选": "ENABLE_PROACTIVE_LLM_PREFILTER",
    "点赞": "PROACTIVE_LIKE",
    "投币": "PROACTIVE_COIN",
    "收藏": "PROACTIVE_FAV",
    "关注": "PROACTIVE_FOLLOW",
    "评论": "PROACTIVE_COMMENT",
}


async def toggle(app, event):
    parts = event.message_str.strip().split(maxsplit=1)
    tm = TOGGLE_ITEMS
    if len(parts)<2:
        lines = ["可切换功能："] + [f"  {n} ({'✅' if app.settings.get(k) else '❌'})" for n,k in tm.items()] + ["","用法: /bili开关 回复","      /bili开关 全部 ← 一键开/关所有"]
        yield event.plain_result("\n".join(lines))
        return
    name=parts[1].strip()

    # ── 一键全部开/关 ──
    if name == "全部":
        # 私信监听和真实拉黑不纳入“一键全部”，避免误开启外部写操作。
        bulk_keys = [
            key for label, key in tm.items()
            if label not in {"私信", "私信回复", "私信拉黑"}
        ]
        # 任一主功能开着 → 全关；全关了 → 全开
        any_on = any(app.settings.get(k) for k in bulk_keys)
        new_state = not any_on
        for k in bulk_keys:
            app.config[k] = new_state
        app.config.save_config()
        emoji = "✅ 全部开启" if new_state else "❌ 全部关闭"
        yield event.plain_result(
            f"{emoji}（{len(bulk_keys)}项；B站私信与私信拉黑需单独切换）"
        )
        return

    key=tm.get(name)
    if not key:
        yield event.plain_result(f"❌ 不认识：{name}")
        return
    cur=app.settings.get(key)
    app.config[key]=not cur
    app.config.save_config()
    yield event.plain_result(f"{name}: {'✅ 已开启' if not cur else '❌ 已关闭'}")


async def cleanup(app, event):
    parts = event.message_str.strip().split(maxsplit=1)
    full_clean = len(parts) >= 2 and parts[1].strip() == "all"
    # 清理临时文件
    app.cleanup_temp_files()
    msg_lines = ["🗑️ 清理完成：", "  ✅ 临时图片/视频/二维码已清理"]
    if full_clean:
        # 活动日志在数据库里：立刻按每晚的规则清一次（30 天前的、每种超过 3000 条的）
        removed = app.prune_activity_logs()
        if removed:
            msg_lines.append(f"  ✅ 活动日志：删了 {sum(removed.values())} 条旧记录")
        # 已回复的评论只留最近 2000 条（每晚清算也会做）
        trimmed = app.replied_comments.trim()
        if trimmed:
            msg_lines.append(f"  ✅ 已回复记录：{trimmed[0]}→{trimmed[1]}条")
        msg_lines.append("")
        msg_lines.append("💡 提示：如需完全重置，手动删除 plugin_data/astrbot_plugin_bilibili_ai_bot 目录")
    else:
        msg_lines.append("")
        msg_lines.append("💡 /bili清理 all ← 同时清理旧日志")
    yield event.plain_result("\n".join(msg_lines))


async def stow_old_json(app, event):
    """旧 json 挪进 ``old_json/<时间>/``：先列出来，带「确认」再挪。不删，要找回就拖回原处。"""
    confirmed = event.message_str.strip().split(maxsplit=1)[1:] == ["确认"]
    files = app.old_json_files()
    if files is None:
        yield event.plain_result("⚠️ 有部分数据没有接上数据库，还在使用旧 json，这次不移动。请查看启动日志里和数据库有关的报错")
        return
    if not files:
        yield event.plain_result("✨ 没有需要移动的旧 json")
        return
    total = sum(size for _, size in files)
    if not confirmed:
        names = "\n".join(f"  {name}" for name, _ in files)
        yield event.plain_result(
            f"📦 以下 {len(files)} 个旧 json 已经完整导入数据库，之后没有改动（共 {total / 1024:.0f} KB）：\n{names}\n"
            "它们会被移到数据目录下的 old_json 文件夹里，不会删除。\n"
            "移走后如果数据库出问题，插件就读不到这些旧文件了；需要时把它们移回数据目录即可。\n"
            "确认移动请发送 /bili清理旧 确认"
        )
        return
    target = app.stow_old_json([name for name, _ in files])
    yield event.plain_result(f"✅ 已移动 {len(files)} 个旧 json（{total / 1024:.0f} KB）到数据目录下的 old_json/{os.path.basename(target)}")


async def export(app, event):
    """数据库里的数据按旧格式写回 json：想退回旧版本、或自己备份看看时用。"""
    written = app.export_json()
    if not written:
        yield event.plain_result("⚠️ 数据库没接上，现在本来就在用 json，不用导出")
        return
    names = "\n".join(f"  {os.path.basename(path)}" for path in written)
    yield event.plain_result(f"📦 已按旧格式写回 {len(written)} 个 json：\n{names}\n💡 插件照常用数据库；下次启动会把这些文件再合并一次，不会重复")
