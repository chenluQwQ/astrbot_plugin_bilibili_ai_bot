"""性格演化和兴趣：查看、手动编辑、删除、回滚。"""
from __future__ import annotations

from datetime import datetime

from astrbot.api import logger
from ..domain.personality import meme_is_active


async def personality(app, event):
    evo = app.personality.state()
    readiness = await app.personality_evolution.readiness()
    lines = ["🌱 性格演化", "━━━━━━━━━━━━"]
    lines.append(
        f"模式：每周一次｜自动演化：{'开启' if app.settings.get('ENABLE_PERSONALITY_EVOLUTION') else '关闭'}"
    )
    lines.append(
        f"数据准备：{readiness['days']}/{readiness['minimum_days']} 天"
        f"（{'已就绪' if readiness['ready'] else '继续积累'}）"
    )
    block = evo.get("dynamic_block", {})
    block_lines = []
    if block.get("recent_state"):
        block_lines.append(f"  状态：{block['recent_state']}")
    for label, key in (
        ("喜好", "recent_preferences"),
        ("感想", "recent_thoughts"),
        ("反思", "recent_reflections"),
    ):
        values = block.get(key, [])
        if values:
            block_lines.append(f"  {label}：{'；'.join(values)}")
    if block_lines:
        lines.append("【近期动态区块】")
        lines.extend(block_lines)
    traits = evo.get("evolved_traits", [])
    if traits:
        lines.append("【手动/旧版成长变化】")
        for i, t in enumerate(traits, 1):
            lines.append(f"  {i}. [{t.get('time','')}] {t.get('change','')}")
            if t.get("trigger"): lines.append(f"     ↳ 触发：{t['trigger']}")
    habits = evo.get("speech_habits", [])
    if habits:
        lines.append("【说话习惯】")
        for i, h in enumerate(habits, 1): lines.append(f"  {i}. {h}")
    opinions = evo.get("opinions", [])
    if opinions:
        lines.append("【对事物的看法】")
        for i, o in enumerate(opinions, 1): lines.append(f"  {i}. {o}")
    ref = evo.get("last_reflection", "")
    if ref: lines.append(f"\n💭 最近反思：{ref}")
    memes = [
        item for item in evo.get("memes", [])
        if meme_is_active(item, datetime.now().strftime("%Y-%m-%d"))
    ]
    if memes:
        lines.append("【近期低频表达】")
        for item in memes:
            lines.append(
                f"  · {item.get('phrase', '')}（至 {item.get('expires_at', '')}）"
            )
    lines.append(
        f"\n📅 上次演化：{evo.get('last_evolve') or '从未'} | "
        f"版本：v{evo.get('version',0)} | 可回滚快照：{len(evo.get('history', []))}"
    )
    yield event.plain_result("\n".join(lines))


async def interest(app, event):
    preferences = app.preferences()
    lifecycle_items = []
    try:
        lifecycle_items = await preferences.current(limit=20)
    except Exception as exc:
        logger.debug(f"[BiliBot] 兴趣状态读取失败，使用本地副本: {exc}")
    if not lifecycle_items:
        lifecycle_items = preferences.cached()
    yield event.plain_result(
        app.interest_report(lifecycle_items=lifecycle_items)
    )


async def personality_edit(app, event):
    parts = event.message_str.strip().split(maxsplit=2)
    if len(parts) < 3:
        yield event.plain_result("用法：\n/bili性格编辑 习惯 <内容>\n/bili性格编辑 看法 <内容>\n/bili性格编辑 变化 <内容>")
        return
    category, content = parts[1].strip(), parts[2].strip()
    yield event.plain_result(app.personality.add_manual(category, content))


async def personality_delete(app, event):
    parts = event.message_str.strip().split(maxsplit=2)
    if len(parts) < 3:
        yield event.plain_result("用法：/bili性格删除 <习惯|看法|变化> <序号>")
        return
    category, idx_str = parts[1].strip(), parts[2].strip()
    if not idx_str.isdigit():
        yield event.plain_result("❌ 序号必须是数字")
        return
    yield event.plain_result(app.personality.remove_manual(category, int(idx_str) - 1))


async def personality_rollback(app, event):
    success, message = app.personality.rollback()
    yield event.plain_result(("✅ " if success else "⚠️ ") + message)
