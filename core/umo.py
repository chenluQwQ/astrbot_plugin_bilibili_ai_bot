"""主动推送用的 UMO（unified_msg_origin）归一化与发送。

AstrBot 的 UMO 固定是「平台实例ID:消息类型:会话ID」三段式，例如
``demo_onebot:FriendMessage:10001``。历史上配置里经常只填两段
（``FriendMessage:10001``），``context.send_message`` 找不到平台实例时
只返回 False 而不抛异常，于是推送「看起来发了、实际没发」。
这里统一补齐平台实例 ID，并把发送结果显式返回。
"""

from __future__ import annotations

import re

from astrbot.api import logger

_FULL_UMO = re.compile(
    r"^(?P<platform>[^:]+):(?P<type>[A-Za-z]+Message):(?P<session>.+)$"
)
_MESSAGE_TYPES = {
    "friendmessage": "FriendMessage",
    "privatemessage": "FriendMessage",
    "groupmessage": "GroupMessage",
}


def platform_ids(context) -> list[str]:
    """当前已加载的平台实例 ID，aiocqhttp/onebot 类排在最前。"""
    ids: list[str] = []
    try:
        insts = list(context.platform_manager.platform_insts)
    except Exception:
        return ids
    for inst in insts:
        try:
            meta = inst.meta()
        except Exception:
            continue
        platform_id = str(getattr(meta, "id", "") or "").strip()
        if not platform_id:
            continue
        kind = f"{getattr(meta, 'type', '') or ''} {getattr(meta, 'name', '') or ''}".lower()
        if "aiocqhttp" in kind or "onebot" in kind:
            ids.insert(0, platform_id)
        else:
            ids.append(platform_id)
    return ids


def is_full_umo(value: str) -> bool:
    return bool(_FULL_UMO.match(str(value or "").strip()))


def normalize_umo(context, value: str) -> str:
    """把两段式/纯数字的配置补成三段式 UMO；无法识别时原样返回。"""
    raw = str(value or "").strip()
    if not raw or is_full_umo(raw):
        return raw

    parts = raw.split(":")
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        msg_type, session_id = parts[0].strip(), parts[1].strip()
    elif len(parts) == 1 and parts[0].strip().isdigit():
        msg_type, session_id = "FriendMessage", parts[0].strip()
    else:
        return raw

    mapped = _MESSAGE_TYPES.get(msg_type.lower())
    if mapped is None:
        return raw
    ids = platform_ids(context)
    if not ids:
        return raw
    normalized = f"{ids[0]}:{mapped}:{session_id}"
    logger.info(f"[BiliBot] UMO 补全平台实例：{raw} → {normalized}")
    return normalized


async def send_chain_to_umo(context, value: str, chain) -> tuple[bool, str]:
    """按 UMO 主动发送消息链，返回 (是否真的送达, 说明)。

    ``context.send_message`` 找不到平台实例时只返回 False、不抛异常，
    所以必须检查返回值，否则「发送成功」只是日志里的错觉。
    """
    umo = normalize_umo(context, value)
    if not umo:
        return False, "UMO 为空"
    if not is_full_umo(umo):
        return False, f"UMO 缺少平台实例 ID：{umo}"
    try:
        delivered = await context.send_message(umo, chain)
    except Exception as exc:  # 平台层异常
        return False, f"{umo} 发送异常：{exc}"
    if delivered is False:
        return False, f"{umo} 未找到可用平台实例（平台未启用或被禁用）"
    return True, umo


async def send_to_umo(context, value: str, text: str) -> tuple[bool, str]:
    """按 UMO 主动发送纯文本，返回 (是否真的送达, 说明)。"""
    from astrbot.api.event import MessageChain

    return await send_chain_to_umo(context, value, MessageChain().message(str(text)))
