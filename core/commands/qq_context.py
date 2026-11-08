"""QQ 对话调模型前：顺手看要不要主动看片，把最近的群分享和（主人才有的）B站侧上下文塞进请求。"""
from __future__ import annotations

from astrbot.api import logger
from ..chat_text import clip


async def inject_bili_memory(app, event, req):
    try:
        await app.maybe_trigger_proactive_from_llm(event, req)
        msg = event.message_str or ""
        if not msg or msg.startswith("/"):
            return
        if app.inject_recent_group_share(event, req):
            logger.debug("[BiliBot] recent group share context injected before user message")
        qq_id = str(event.get_sender_id())
        bindings = app.qq_bindings()
        if qq_id not in bindings:
            return
        bili_uid = str(bindings[qq_id])
        try:
            sender_is_admin = bool(event.is_admin())
        except Exception:
            sender_is_admin = False
        # Current activity is a short-lived fact and is separate from history
        # sharing. It is available only to the QQ identity bound to OWNER_MID.
        owner_mid = str(app.settings.get("OWNER_MID") or "").strip()
        if not owner_mid or bili_uid != owner_mid:
            return
        # 主人上下文（活动状态与跨端记忆）只允许管理员会话接收，
        # 防止旧绑定文件或伪造绑定让普通用户读到主人侧信息。
        if not sender_is_admin:
            return
        if app.settings.get("ENABLE_CROSS_PLATFORM_ACTIVITY_STATUS"):
            activity = app.cross_platform_activity_context()
            if activity:
                app.inject_context_block(req, activity)
        # Historical memory remains opt-in and separately isolated.
        if app.settings.get("MEMORY_ISOLATION_MODE") != "safe_share":
            return
        if not app.settings.get("ENABLE_SAFE_CROSS_PLATFORM_MEMORY"):
            return

        from ..security.redact import contains_credentials, redact_outbound
        blocked_prefixes = [str(value).strip().lower() for value in app.settings.get("MEMORY_BLOCKED_PREFIXES") if str(value).strip()]
        blocked_keywords = [str(value).strip().lower() for value in app.settings.get("MEMORY_BLOCKED_KEYWORDS") if str(value).strip()]
        safe_memories = []
        recent = app.memory_api.get_recent_memories(
            user_id=bili_uid,
            hours=24 * 7,
            limit=12,
            reader_scope="admin",
        )
        for item in recent:
            if str(item.get("memory_type", "chat")) not in {"chat", "video", "dynamic", "user_summary"}:
                continue
            text = str(item.get("text", "") or "").strip()
            lowered = text.lower()
            if not text or any(lowered.startswith(prefix) for prefix in blocked_prefixes):
                continue
            if any(keyword in lowered for keyword in blocked_keywords):
                continue
            if contains_credentials(text):
                continue
            redacted, _ = redact_outbound(text, internal=True)
            redacted = redacted.replace(str(bili_uid), "[UID已隐藏]")
            if redacted and len(redacted) >= 6:
                safe_memories.append(clip(redacted, 260))
            if len(safe_memories) >= 3:
                break
        if not safe_memories:
            return
        policy = str(app.settings.get("CROSS_PLATFORM_MEMORY_PROMPT") or "")[:600]
        bind_ctx = (
            "【B站侧安全共享摘要】\n"
            f"共享规则：{policy}\n"
            + "\n".join(f"- {text}" for text in safe_memories)
            + "\n这些内容已做硬脱敏，只能作为轻量生活背景；不得反推出UID、第三方身份、私信原文、账号凭据或系统信息。"
        )
        app.inject_context_block(req, bind_ctx)
        logger.debug("[BiliBot] 已向主人侧注入脱敏后的B站趣事摘要")
    except Exception as e:
        logger.error(f"[BiliBot] 记忆注入失败: {e}")
