"""记忆：搜索、清算、周总结、老化清理、直播联动、旧记忆迁移、永久记忆、追番记忆。"""
from __future__ import annotations

from astrbot.api.message_components import Image, Plain
from ..chat_text import split_for_chat
from ..domain.self_memory import MAX_ENTRIES, SelfMemoryRejected


async def memory(app, event):
    parts = event.message_str.strip().split(maxsplit=2)
    type_alias = {
        "交流": {"chat"},
        "聊天": {"chat"},
        "评论": {"chat"},
        "视频": {"video"},
        "观影": {"video"},
        "动态": {"dynamic"},
        "直播": {"live"},
        "总结": {"user_summary"},
        "压缩": {"user_summary"},
    }
    if len(parts)<2:
        mc = await app.count_memories()
        chat_count = await app.count_memories(types={"chat"})
        video_count = await app.count_memories(types={"video"})
        dynamic_count = await app.count_memories(types={"dynamic"})
        live_count = await app.count_memories(types={"live"})
        user_summary_count = await app.count_memories(types={"user_summary"})
        lvl = app.memory_consolidation().get_stats()
        level_line = f"📊 级别: 今日:{lvl['today']} | 近期:{lvl['recent']} | 长期:{lvl['long_term']} | 老化:{lvl['aged']}"
        if lvl.get('no_level', 0) > 0:
            level_line += f" | ⚠无级别:{lvl['no_level']}"
        yield event.plain_result(
            "🧠 记忆统计\n"
            f"总计:{mc}\n"
            f"交流:{chat_count} | 视频:{video_count} | 动态:{dynamic_count} | 直播:{live_count} | 用户总结:{user_summary_count}\n"
            f"{level_line}\n\n"
            "用法:\n"
            "/bili记忆 <关键词>\n"
            "/bili记忆 <关键词> 视频 ← 只搜视频记忆\n"
            "/bili记忆 <关键词> 动态 ← 只搜动态记忆\n"
            "/bili记忆 <关键词> 直播 ← 只搜直播记忆\n"
            "/bili记忆 <关键词> 交流 ← 只搜交流记忆"
        )
        return
    query=parts[1]
    arg=parts[2] if len(parts)>2 else None
    source = None
    memory_types = None
    if arg:
        if arg == "all":
            source = None
        elif arg == "bilibili":
            source = arg
        elif arg in type_alias:
            memory_types = type_alias[arg]
        else:
            source = arg
    results = await app.search_memories(query, limit=5, source=source, memory_types=memory_types, reader_scope="admin")
    if not results:
        yield event.plain_result(f"🧠 没找到「{query}」的记忆")
        return
    suffix = f"（{arg}）" if arg else ""
    lines = [f"🧠 关于「{query}」的记忆{suffix}：",""]
    for i,r in enumerate(results,1): lines.append(f"{i}. {r}")
    for chunk in split_for_chat("\n".join(lines)):
        yield event.plain_result(chunk)


async def consolidation(app, event):
    yield event.plain_result("🌙 开始手动清算...")
    try:
        summary = await app.memory_consolidation().run_daily()
        yield event.plain_result(f"🌙 清算完成\n{summary}")
    except Exception as e:
        yield event.plain_result(f"❌ 清算失败: {e}")


async def weekly_summary(app, event):
    yield event.plain_result("📅 正在回顾这一周...")
    try:
        summary, delivered, image_path = await app.run_weekly_summary()
        if not summary:
            yield event.plain_result("📅 这周没什么活动记录，没有生成总结")
            return
        via = "、".join(delivered) if delivered else "仅存档"
        if image_path:
            yield event.chain_result([Plain(f"📅 周总结已生成（{via}）"), Image.fromFileSystem(image_path)])
        else:
            yield event.plain_result(f"{summary}\n\n——已投递：{via}")
    except Exception as e:
        yield event.plain_result(f"❌ 周总结失败: {e}")


async def cleanup_aged(app, event):
    removed = await app.memory_consolidation().cleanup_aged()
    if removed:
        yield event.plain_result(f"🗑️ 已清理 {removed} 条老化记忆")
    else:
        yield event.plain_result("✅ 没有需要清理的老化记忆")


async def integration(app, event):
    live_count = await app.count_memories(types={"live"})
    profile_count = len(app.user_profiles)
    yield event.plain_result(
        "🎙️ BiliBot 记忆联动\n"
        "━━━━━━━━━━━━\n"
        f"接口版本：v{app.memory_api.api_version}\n"
        f"用户画像：{profile_count} 个\n"
        f"直播记忆：{live_count} 条\n"
        "状态：✅ 等待直播插件按 B站 UID 读写"
    )


async def migrate(app, event):
    async with app.memory_write_lock:
        migrated = app.memory_consolidation()._migrate_legacy_entries()
        if migrated:
            await app.replace_memory_snapshot(assume_locked=True)
    if migrated:
        yield event.plain_result(f"📦 已迁移 {migrated} 条旧记忆（chat→recent/其他→long_term）")
    else:
        yield event.plain_result("✅ 所有记忆已有有效 level，无需迁移")


async def permanent(app, event):
    parts = event.message_str.strip().split(maxsplit=2)
    perm = app.permanent_memories()
    if len(parts) >= 3 and parts[1] == "删除":
        idx_str = parts[2].strip().lstrip("#")
        if not idx_str.isdigit():
            yield event.plain_result("❌ 序号必须是数字")
            return
        try:
            removed = app.delete_permanent_memory(int(idx_str))
        except SelfMemoryRejected:
            yield event.plain_result(f"❌ 序号超范围（1-{len(perm)}）")
            return
        yield event.plain_result(f"✅ 已删除永久记忆：{removed}")
        return
    if not perm:
        yield event.plain_result("💎 还没有永久记忆")
        return
    lines = [f"💎 永久记忆（{len(perm)}/{MAX_ENTRIES}）", "━━━━━━━━━━━━"]
    for i, p in enumerate(perm, 1):
        lines.append(f"  {i}. [{p.get('time','?')}] {p.get('text','')}")
    lines.append("\n删除用: /bili永久记忆 删除 <序号>")
    for chunk in split_for_chat("\n".join(lines)):
        yield event.plain_result(chunk)


_MEMO_KINDS = {"video": "视频", "bangumi": "番剧", "up": "UP主", "other": "其他"}


async def memos(app, event):
    parts = event.message_str.strip().split(maxsplit=2)
    if len(parts) >= 3 and parts[1] == "删除":
        memo_id = parts[2].strip().lstrip("#")
        if not memo_id.isdigit():
            yield event.plain_result("❌ 编号必须是数字")
            return
        if app.drop_memo(int(memo_id)):
            yield event.plain_result(f"✅ 已删除备忘 #{memo_id}")
        else:
            yield event.plain_result(f"❌ 没有还开着的备忘 #{memo_id}")
        return
    items = app.open_memos()
    if not items:
        yield event.plain_result("📝 备忘录是空的")
        return
    lines = [f"📝 备忘录（{len(items)} 条）", "━━━━━━━━━━━━"]
    for item in items:
        who = "、".join(person.get("name") or person.get("uid", "") for person in item["recommenders"]) or "自己"
        when = item["due_date"] or "以后再说"
        state = "（已排）" if item["state"] == "scheduled" else ""
        lines.append(f"#{item['id']} [{_MEMO_KINDS.get(item['kind'], item['kind'])}] {item['target']}{state}")
        lines.append(f"   {when} · {who}" + (f" · {item['note']}" if item["note"] else ""))
    lines.append("\n删除用: /bili备忘录 删除 <编号>")
    for chunk in split_for_chat("\n".join(lines)):
        yield event.plain_result(chunk)


async def bangumi(app, event):
    mem = app.bangumi_memory()
    if not mem:
        yield event.plain_result("🎬 还没有看过任何番剧")
        return
    lines = [f"🎬 追番记忆（共 {len(mem)} 部）", "━━━━━━━━━━━━"]
    for sid, record in sorted(mem.items(), key=lambda x: x[1].get("episodes", [{}])[-1].get("watched_at", ""), reverse=True)[:15]:
        title = record.get("title", "?")
        total = record.get("total_watched", 0)
        watched_eps = record.get("watched_eps", [])
        last_score = record.get("last_score", "?")
        ep_str = ",".join(watched_eps[:20]) if watched_eps else "?"
        lines.append(f"  《{title}》已看{total}集 [{ep_str}] 最近评分:{last_score}")
    yield event.plain_result("\n".join(lines))
