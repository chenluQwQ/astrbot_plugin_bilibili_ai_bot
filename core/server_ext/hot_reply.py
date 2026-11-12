"""热评楼中楼回复（服务器版 proactive.py 的独立开关，默认关闭）。

2.0.2 的评论生成链路已改用 ``content_protocol``，而服务器版是在主评论发完后
额外抓热评、随机挑几条再回复。这里按 2.0.2 的宿主接口重写：

- 抓热评：``bili_http._http_get`` 直接请求 reply/main（2.0.2 的
  ``bili_comments._get_hot_comments`` 只返回文本，缺 rpid / mid）
- 生成：``models._generation_llm_call`` + ``content_protocol.parse_proactive_comment``
- 发送：``outward_actions._execute_proactive_action`` + ``bili_comments._send_reply``
- 记录：``activity_logs.reply.append``，``source="hot_reply"``

不做任何自动重试：发送结果未知时交给统一动作运行时记录。
"""
from __future__ import annotations

import random
from datetime import datetime

from astrbot.api import logger

from ..content_protocol import ContentProtocolError, parse_proactive_comment
from . import compat

HOT_COMMENT_API = "https://api.bilibili.com/x/v2/reply/main"


def hot_reply_count(bot) -> int:
    """管理员配置的热评回复条数（0 表示关闭，上限 10）。"""
    try:
        return max(0, min(10, int(compat.config_value(bot, "PROACTIVE_HOT_REPLY_COUNT", 0) or 0)))
    except (TypeError, ValueError):
        return 0


async def _fetch_hot_comments(bot, oid, limit: int = 10) -> list[dict]:
    """返回 ``[{"rpid", "message", "username", "mid"}]``；失败返回空列表。"""
    try:
        data, _ = await bot.bili_http._http_get(
            HOT_COMMENT_API,
            params={"oid": oid, "type": 1, "mode": 3, "ps": max(1, min(20, limit))},
        )
        if data.get("code") != 0:
            return []
        replies = (data.get("data") or {}).get("replies") or []
        result = []
        for item in replies:
            content = item.get("content") or {}
            message = str(content.get("message") or "").strip()
            rpid = str(item.get("rpid") or "").strip()
            if not message or not rpid.isdigit():
                continue
            member = item.get("member") or {}
            result.append({
                "rpid": rpid,
                "message": message[:300],
                "username": str(member.get("uname") or "")[:32],
                "mid": str(member.get("mid") or ""),
            })
        return result
    except Exception as exc:
        logger.debug(f"[BiliBot] 抓热评失败（{oid}）: {exc!r}")
        return []


async def _generate_reply(bot, video_title: str, hot: dict) -> str:
    prompt = (
        "你正在 B站视频评论区里看别人的热评，准备回一条自然的楼中楼。\n"
        f"视频标题：{video_title or '未知'}\n"
        f"对方（{hot.get('username') or '网友'}）的热评：{hot.get('message')}\n"
        "要求：口语化、像真人接话；不重复对方原句；不提问索要隐私；"
        "不输出任何解释。只输出 JSON："
        '{"decision":"comment","text":"要发送的回复"}；'
        '不想回时输出 {"decision":"skip","text":""}。'
    )
    try:
        raw = await bot.models._generation_llm_call(prompt, max_tokens=300)
    except Exception as exc:
        logger.debug(f"[BiliBot] 热评回复生成失败: {exc!r}")
        return ""
    if not raw:
        return ""
    try:
        parsed = parse_proactive_comment(raw)
    except ContentProtocolError:
        # 模型偶尔只给纯文本；剥掉引号后仍可用。
        return str(raw).strip().strip('"“”')[:80]
    if parsed.get("decision") != "comment":
        return ""
    return str(parsed.get("text") or "").strip()


async def reply_hot_comments(bot, *, bvid: str, oid, title: str = "") -> int:
    """主评论成功后调用；返回实际发出的楼中楼条数。"""
    want = hot_reply_count(bot)
    if want <= 0 or not oid:
        return 0
    comments = await _fetch_hot_comments(bot, oid, limit=10)
    if not comments:
        return 0
    # 跳过自己发的：2.0.2 没有直接暴露 uid，用 OWNER_MID 兜底。
    owner_mid = str(compat.config_value(bot, "OWNER_MID", "") or "")
    candidates = [item for item in comments if not owner_mid or item.get("mid") != owner_mid]
    if not candidates:
        return 0
    sent = 0
    for hot in random.sample(candidates, min(want, len(candidates))):
        try:
            text = await _generate_reply(bot, title, hot)
            if not text:
                continue
            outcome = await bot.outward_actions._execute_proactive_action(
                f"proactive_hot_reply:{bvid}:{hot['rpid']}",
                "proactive_hot_reply",
                bvid,
                lambda: bot.bili_comments._send_reply(oid, int(hot["rpid"]), 1, text),
                metadata={"text": text[:200], "source": "hot_reply"},
            )
            if not outcome.success:
                continue
            bot.activity_logs.reply.append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "bvid": bvid,
                "title": title,
                "username": hot.get("username", ""),
                "content": hot["message"][:100],
                "reply": text,
                "source": "hot_reply",
                "rpid": hot["rpid"],
            })
            sent += 1
            logger.info(f"[BiliBot] 💬 热评楼中楼成功：{hot.get('username')} → {text[:40]}")
        except Exception as exc:
            logger.warning(f"[BiliBot] 热评楼中楼失败（{hot.get('rpid')}）: {exc}")
    return sent
