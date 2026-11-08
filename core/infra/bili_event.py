"""把一条 B站评论或私信包成 AstrBot 的消息事件，只为了走 AstrBot 的工具循环。

只开事件不开会话：对话历史照旧由插件自己的记忆管，
这里的事件不进 AstrBot 的会话列表，也不能用来发消息——回复由插件自己的发送流程发。
发消息的人在 B站永远是普通成员，拿不到管理员权限的工具。
"""
from __future__ import annotations

PLATFORM_NAME = "bilibili"
PLATFORM_ID = "bilibili_ai_bot"
CHANNELS = ("comment", "private")

_event_class = None


def _bili_event_class():
    """AstrBot 的类在用到时才导入：测试里换成假的 astrbot 也能导入本模块。"""
    global _event_class
    if _event_class is None:
        from astrbot.core.platform.astr_message_event import AstrMessageEvent

        class BiliMessageEvent(AstrMessageEvent):
            async def send(self, message):
                raise RuntimeError("B站事件只用于工具循环，不能直接发消息")

        _event_class = BiliMessageEvent
    return _event_class


def make_bili_event(*, channel, uid, text, nickname="", self_uid="", message_id=""):
    """``channel`` 是 ``comment`` 或 ``private``；会话 ID 是「通道:UID」，评论和私信分开。"""
    if channel not in CHANNELS:
        raise ValueError(f"未知的 B站通道：{channel}")
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    uid = str(uid or "").strip()
    session_id = f"{channel}:{uid}"
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = str(self_uid or "")
    message.session_id = session_id
    message.message_id = str(message_id or "")
    message.sender = MessageMember(user_id=uid, nickname=str(nickname or "") or None)
    message.message = []
    message.message_str = str(text or "")
    message.raw_message = None
    meta = PlatformMetadata(
        name=PLATFORM_NAME,
        description="B站评论和私信（只用于工具循环）",
        id=PLATFORM_ID,
        support_streaming_message=False,
        support_proactive_message=False,
    )
    event = _bili_event_class()(message.message_str, message, meta, session_id)
    event.role = "member"
    return event
