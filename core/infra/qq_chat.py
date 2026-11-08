"""和 QQ 打交道：当前会话用的模型和人格、发消息和视频、往聊天请求里塞背景。"""
from __future__ import annotations

import asyncio

from astrbot.api import logger
from .host_view import HostView


class QQChat(HostView):
    """只按 QQ 会话写的；B站消息接进工具循环时要换成各通道自己的。"""

    HOST_API = frozenset({
        "context",
    })

    @staticmethod
    def _event_message_id(event):
        message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        return str(message_id) if message_id not in (None, "") else None

    async def _send_onebot_chain(self, event, component):
        """Send one media component through OneBot so its message id can be quoted.

        Returns ``(handled, message_id)``. ``handled`` is False when the adapter is
        not OneBot, so the caller falls back to the normal AstrBot send path. Once a
        send was attempted it is never retried, because it may already have arrived;
        a failed send reports ``None`` and a send without a returned id reports "".
        """
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        try:
            platform = str(event.get_platform_name() or "").lower()
        except Exception:
            platform = ""
        if platform != "aiocqhttp" or not callable(call_action):
            return False, None
        try:
            group_id = str(event.get_group_id() or "").strip()
            target = {"group_id": int(group_id)} if group_id else {"user_id": int(event.get_sender_id())}
            if getattr(component, "type", "") in ("nodes", "node") or type(component).__name__ in ("Nodes", "Node"):
                payload = component.to_dict()
                if asyncio.iscoroutine(payload):
                    payload = await payload
                action = "send_group_forward_msg" if group_id else "send_private_forward_msg"
                params = {**payload, **target}
            else:
                convert = getattr(type(event), "_from_segment_to_dict", None)
                segment = convert(component) if callable(convert) else component.to_dict()
                if asyncio.iscoroutine(segment):
                    segment = await segment
                action = "send_group_msg" if group_id else "send_private_msg"
                params = {**target, "message": [segment]}
        except Exception as exc:
            logger.debug(f"[BiliBot] 无法直接构造 OneBot 视频消息，改用普通发送: {exc}")
            return False, None
        try:
            ret = await call_action(action, **params)
        except Exception as exc:
            logger.warning(f"[BiliBot] 分享视频发送失败: {exc}")
            return True, None
        message_id = ret.get("message_id") if isinstance(ret, dict) else None
        return True, str(message_id) if message_id not in (None, "") else ""

    def _share_video_component(self, video_path):
        try:
            import astrbot.api.message_components as Comp
            for cls_name in ("Video", "File"):
                cls = getattr(Comp, cls_name, None)
                if not cls:
                    continue
                for meth in ("fromFileSystem", "from_file", "fromPath"):
                    fn = getattr(cls, meth, None)
                    if fn:
                        try:
                            return fn(video_path)
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"[BiliBot] 当前 AstrBot 视频组件不可用: {e}")
        return None

    def _share_forward_component(self, event, components):
        """One forward bundle, with exactly one video/file per node."""
        try:
            from astrbot.api.message_components import Node, Nodes

            try:
                self_id = str(event.get_self_id() or "0")
            except (AttributeError, TypeError):
                self_id = str(getattr(getattr(event, "message_obj", None), "self_id", "0") or "0")
            return Nodes([
                Node(uin=self_id, name="BiliBot", content=[component])
                for component in components
            ])
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            logger.warning(f"[BiliBot] 当前适配器无法构建视频合集，回退逐段发送: {type(exc).__name__}")
            return None

    async def _send_share_reaction(self, umo, text, reply_to):
        import astrbot.api.message_components as Comp
        from astrbot.api.event import MessageChain

        chain = [Comp.Plain(text)]
        if reply_to:
            chain.insert(0, Comp.Reply(id=str(reply_to)))
        await self.context.send_message(umo, MessageChain(chain))

    async def _qq_session_provider_id(self, umo):
        """The chat model this QQ conversation is currently using."""
        getter = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(getter):
            try:
                provider_id = getter(umo=umo)
                if asyncio.iscoroutine(provider_id):
                    provider_id = await provider_id
                if provider_id:
                    return str(provider_id)
            except Exception as exc:
                logger.debug(f"[BiliBot] 读取当前会话模型失败: {exc}")
        getter = getattr(self.context, "get_using_provider", None)
        if callable(getter):
            for kwargs in ({"umo": umo}, {}):
                try:
                    provider = getter(**kwargs)
                    meta = provider.meta() if provider is not None else None
                    if getattr(meta, "id", None):
                        return str(meta.id)
                except TypeError:
                    continue
                except Exception as exc:
                    logger.debug(f"[BiliBot] 读取当前会话模型失败: {exc}")
                    break
        return ""

    async def _qq_session_persona_prompt(self, umo):
        """The persona of this QQ conversation: its selected persona, else the default."""
        personas = getattr(self.context, "persona_manager", None)
        conversations = getattr(self.context, "conversation_manager", None)
        persona_id = None
        try:
            cid = await conversations.get_curr_conversation_id(umo)
            conversation = await conversations.get_conversation(umo, cid) if cid else None
            persona_id = getattr(conversation, "persona_id", None)
        except Exception as exc:
            logger.debug(f"[BiliBot] 读取当前会话人格失败: {exc}")
        if persona_id == "[%None]":
            return ""
        if persona_id:
            try:
                persona = await personas.get_persona(persona_id)
                prompt = getattr(persona, "system_prompt", None)
                if prompt is None and isinstance(persona, dict):
                    prompt = persona.get("prompt")
                if prompt:
                    return str(prompt).strip()
            except Exception as exc:
                logger.debug(f"[BiliBot] 读取会话人格 {persona_id} 失败: {exc}")
        try:
            persona = await personas.get_default_persona_v3(umo=umo)
        except TypeError:
            persona = await personas.get_default_persona_v3()
        except Exception as exc:
            logger.debug(f"[BiliBot] 读取默认人格失败: {exc}")
            return ""
        prompt = persona.get("prompt") if isinstance(persona, dict) else getattr(persona, "prompt", "")
        return str(prompt or "").strip()

    def _inject_context_block_before_user(self, req, block):
        """把背景作为本轮用户消息的临时附加段，不碰 system_prompt 和历史，免得打掉缓存。"""
        parts = getattr(req, "extra_user_content_parts", None)
        if isinstance(parts, list):
            try:
                from astrbot.core.agent.message import TextPart
                part = TextPart(text=block)
                mark = getattr(part, "mark_as_temp", None)
                parts.append(mark() if callable(mark) else part)
                return True
            except Exception as exc:
                logger.debug(f"[BiliBot] 无法追加临时上下文段，改拼进用户消息: {exc}")

        value = getattr(req, "prompt", None)
        if isinstance(value, str):
            req.prompt = f"{block}\n\n{value}" if value.strip() else block
            return True
        return False
