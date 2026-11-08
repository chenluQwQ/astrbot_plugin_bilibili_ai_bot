"""人：好感度、拉黑、解黑、黑名单。"""
from __future__ import annotations

from datetime import datetime

from ..config import LEVEL_NAMES
from ..runtime import WriteResultUnknownError
from ..chat_text import split_for_chat


async def affection(app, event):
    parts = event.message_str.strip().split(maxsplit=1)
    if len(parts)>=2:
        uid=parts[1].strip()
        sc=app.affection.score(uid)
        lv=app.relationship_level(sc,uid)
        p=app.user_profiles.raw(uid)
        lines=[f"👤 用户 {uid}",f"💛 {sc}分 | {LEVEL_NAMES[lv]}",f"📝 {p.get('impression','暂无')}"]
        if p.get("facts"): lines.append(f"📋 {'；'.join(p['facts'][-5:])}")
        yield event.plain_result("\n".join(lines))
        return
    if not len(app.affection):
        yield event.plain_result("💛 无记录")
        return
    sa=app.affection.ranking(10)
    lines=["💛 好感度 Top 10","━━━━━━━━━━━━"]
    ps=app.user_profiles.all()
    for i,(uid,sc) in enumerate(sa,1):
        lv=app.relationship_level(sc,uid)
        imp=ps.get(uid,{}).get("impression","")
        lines.append(f"{i}. UID:{uid} | {sc}分 {LEVEL_NAMES[lv]}{' — '+imp if imp else ''}")
    for chunk in split_for_chat("\n".join(lines)):
        yield event.plain_result(chunk)


async def block(app, event):
    parts = event.message_str.strip().split(maxsplit=1)
    if len(parts)<2:
        yield event.plain_result("用法: /bili拉黑 <UID>")
        return
    uid=parts[1].strip()
    if not uid.isdigit():
        yield event.plain_result("❌ UID必须是数字")
        return
    if app.is_owner(uid):
        yield event.plain_result("❌ 不能拉黑主人！")
        return
    block_unknown = False
    try:
        success = await app.block(int(uid))
    except WriteResultUnknownError:
        success = False
        block_unknown = True
    app.blocklist.add(uid, {"username":"手动拉黑","reason":"手动拉黑","time":datetime.now().strftime("%Y-%m-%d %H:%M")})
    if block_unknown:
        yield event.plain_result(
            f"⚠️ UID:{uid} 已加入本地黑名单，但B站拉黑结果未知；"
            "请求可能已经生效，请勿重复操作，先人工核查远端状态。"
        )
        return
    yield event.plain_result(f"{'✅' if success else '⚠️'} 已拉黑 UID:{uid}{'（B站API调用成功）' if success else '（B站API失败，但已加入本地黑名单）'}")


async def unblock(app, event):
    parts = event.message_str.strip().split(maxsplit=1)
    if len(parts)<2:
        yield event.plain_result("用法: /bili解黑 <UID>")
        return
    uid=parts[1].strip()
    if not app.blocklist.contains(uid):
        yield event.plain_result(f"⚠️ UID:{uid} 不在黑名单中")
        return
    # B站解除拉黑: act=6
    try:
        api_ok = await app.unblock(uid)
    except WriteResultUnknownError:
        yield event.plain_result(
            f"⚠️ UID:{uid} 的B站解黑结果未知，请求可能已经生效；"
            "本地黑名单和好感度均未修改，请先人工核查远端状态，勿重复操作。"
        )
        return
    except Exception: api_ok=False
    app.blocklist.remove(uid)
    # 重置好感度为0
    app.affection.set(uid, 0)
    yield event.plain_result(f"✅ 已解除拉黑 UID:{uid}，好感度重置为0{'' if api_ok else '（B站API失败，但已从本地黑名单移除）'}")


async def blocklist(app, event):
    bl = app.blocklist.entries()
    if not bl:
        yield event.plain_result("🚫 黑名单为空")
        return
    lines = ["🚫 黑名单","━━━━━━━━━━━━"]
    for uid,info in bl.items():
        lines.append(f"UID:{uid} | {info.get('reason','未知')} | {info.get('time','')}")
    yield event.plain_result("\n".join(lines))
