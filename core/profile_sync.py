"""跨平台画像同步：QQ 侧主人互动 → 回灌 bilibot 主人画像。

默认关闭（``ENABLE_CROSS_PLATFORM_PROFILE_SYNC``）。开启后，主人在 QQ 群/私信
中的发言会被累积、脱敏，达到阈值后由 LLM 提炼为画像事实与标签，写入
``user_profiles.json``（以 ``OWNER_MID`` 为键，``source_scope="qq_cross_platform"``
标注来源，与 B站沟通来源分开）。

安全约束：
- 只有 ``OWNER_QQ_UMO`` 里配置的 QQ 号（主人本人）发言才参与，其他人一律忽略。
- 提炼前的原文过 ``redact_pii`` 脱敏，密码/手机号/QQ号/邮箱/证件号替换为抽象说辞。
- 提炼结果只取 JSON 的 ``tags`` 与 ``user_facts``，不写原始对话正文。
- 任何异常都静默忽略，绝不影响主流程。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime

from astrbot.api import logger

from .security.redact import redact_pii

# 累积多少条主人发言才触发一次提炼（防频繁调 LLM）
BATCH_THRESHOLD = 8
# 累积窗口：超过这个时间没新消息就重置计数
WINDOW_SECONDS = 3600
# 单条发言参与提炼的最大长度
MAX_MESSAGE_CHARS = 200


def _owner_qq(bot) -> str:
    """从 OWNER_QQ_UMO 解析主人 QQ 号。"""
    umo = str(bot.config.get("OWNER_QQ_UMO", "") or "").strip()
    match = re.search(r"(?:FriendMessage|PrivateMessage):(\d+)$", umo, re.I)
    return match.group(1) if match else ""


def _is_enabled(bot) -> bool:
    return bool(bot.config.get("ENABLE_CROSS_PLATFORM_PROFILE_SYNC", False))


def _owner_mid(bot) -> str:
    return str(bot.config.get("OWNER_MID", "") or "").strip()


async def handle_qq_message(bot, event) -> None:
    """在 QQ 群/私信消息钩子里调用；非主人发言或未开启时直接返回。"""
    if not _is_enabled(bot):
        return
    owner_qq = _owner_qq(bot)
    owner_mid = _owner_mid(bot)
    if not owner_qq or not owner_mid:
        return
    try:
        sender = str(event.get_sender_id() or "")
    except Exception:
        return
    if sender != owner_qq:
        return
    # 过滤纯命令 / 纯表情 / 过短消息
    try:
        text = str(event.message_str or "").strip()
    except Exception:
        return
    if not text or text.startswith("/") or len(text) < 2:
        return
    text = redact_pii(text)[:MAX_MESSAGE_CHARS]

    now = time.time()
    queue = getattr(bot, "_cross_profile_queue", None)
    if not isinstance(queue, dict):
        queue = {"texts": [], "last_at": 0.0}
        bot._cross_profile_queue = queue
    # 超过窗口未发消息则清空重计，避免把隔很久的话混在一起提炼
    if now - float(queue.get("last_at") or 0) > WINDOW_SECONDS:
        queue["texts"] = []
    queue["texts"].append(text)
    queue["last_at"] = now

    if len(queue["texts"]) < BATCH_THRESHOLD:
        return

    sample = "\n".join(queue["texts"][-BATCH_THRESHOLD:])
    queue["texts"] = []
    await _distill(bot, owner_mid, sample)


async def _distill(bot, owner_mid, sample) -> None:
    """用 LLM 从主人发言样本里提炼画像事实与标签，脱敏后回灌。"""
    prompt = (
        f"以下是主人（QQ 侧）最近的一些发言，请提炼成画像标签和个人事实，"
        f"只输出 JSON，不要解释。\n\n"
        f"发言样本（已脱敏）：\n{sample}\n\n"
        f'输出格式：{{"tags":["标签1","标签2"],"user_facts":["事实1","事实2"]}}\n'
        f"要求：只提炼明确、稳定、可长期参考的信息（如身份、职业、兴趣、习惯、偏好）；"
        f"不要把一次性闲聊、临时情绪、具体事件细节当成长久事实；"
        f"不要编造发言里没有的信息；tags 最多 5 个，user_facts 最多 5 条。"
    )
    try:
        raw = await getattr(bot, "_summary_llm_call", bot._llm_call)(prompt, max_tokens=300)
    except Exception as exc:
        logger.warning(f"[BiliBot] 跨平台画像提炼调用失败: {exc}")
        return
    if not raw:
        return
    try:
        text = bot._repair_llm_json(raw) if hasattr(bot, "_repair_llm_json") else raw
        result = json.loads(text)
    except Exception:
        # 摘取首个 JSON 对象
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return
        try:
            result = json.loads(match.group(0))
        except Exception:
            return
    if not isinstance(result, dict):
        return
    tags = [redact_pii(str(t).strip()) for t in (result.get("tags") or []) if str(t).strip()]
    facts = [redact_pii(str(f).strip()) for f in (result.get("user_facts") or []) if str(f).strip()]
    if not tags and not facts:
        return
    try:
        bot._update_user_profile(
            owner_mid,
            new_facts=facts or None,
            new_tags=tags or None,
            source_scope="qq_cross_platform",
        )
        logger.info(f"[BiliBot] 🧠 跨平台画像同步：写入 {len(tags)} 标签 / {len(facts)} 事实")
    except Exception as exc:
        logger.warning(f"[BiliBot] 跨平台画像回灌失败: {exc}")
