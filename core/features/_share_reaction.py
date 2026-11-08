"""分享发出后 Bot 自己决定看不看，看完引用视频说一句；工具转后台看完的，也在原会话说一句。
两次模型输出都走严格 JSON，失败就沉默（工具看片结果改发固定提示）。"""
from __future__ import annotations

import asyncio
import json

from astrbot.api import logger
from ..content_protocol import (
    ContentProtocolError, SHARE_REACTION_SCHEMA_PROMPT, SHARE_WATCH_SCHEMA_PROMPT,
    parse_share_reaction, parse_share_watch_decision,
    TOOL_WATCH_FOLLOWUP_SCHEMA_PROMPT,
)
from ..infra.host_view import HostView
from ..security.redact import wrap_untrusted


class ShareReaction(HostView):
    """后台任务挂在 ``_share_reaction_tasks``，停用时取消。"""

    HOST_API = frozenset({
        "models", "qq_chat", "seen_videos", "settings", "share", "share_session", "video_watch",
        "watch_on_request",
    })

    def __init__(self, host):
        super().__init__(host)
        self._share_reaction_tasks = set()

    def _start_share_reaction(self, event, info, share_record, reply_target):
        if not self.settings.get("BILI_SHARE_PARSE_AUTO_WATCH"):
            return
        task = asyncio.create_task(self._react_to_shared_video(
            str(getattr(event, "unified_msg_origin", "") or ""),
            info,
            share_record,
            reply_target,
            scene=self.share_session._share_scene(event)[1],
            user_text=str(getattr(event, "message_str", "") or ""),
        ))
        self._share_reaction_tasks.add(task)
        task.add_done_callback(self._share_reaction_tasks.discard)

    async def _decide_share_watch(self, info, *, scene, user_text, persona, provider_id):
        video = {
            "title": info.get("title", ""),
            "up": info.get("owner_name", ""),
            "partition": info.get("tname", ""),
            "duration": self.share._format_duration(info.get("duration", 0)),
            "desc": self.share._share_video_intro(info),
        }
        prompt = (
            f"有人在QQ{scene}里分享了一个B站视频，解析卡和原视频已经发出来了。"
            "现在由你决定自己要不要点开看。\n"
            f"{wrap_untrusted(json.dumps(video, ensure_ascii=False), 'shared_video')}\n"
        )
        if user_text.strip():
            prompt += f"分享时的消息：\n{wrap_untrusted(user_text[:300], 'share_message')}\n"
        prompt += "看视频要花些时间；只是没兴趣也完全可以不看。\n" + SHARE_WATCH_SCHEMA_PROMPT
        raw = await self.models._llm_call(prompt, system_prompt=persona, max_tokens=60, provider_id=provider_id)
        if not raw:
            return False
        try:
            return parse_share_watch_decision(raw)["decision"] == "watch"
        except ContentProtocolError as exc:
            logger.warning(f"[BiliBot] 分享视频观看决定结构校验失败，不看: {exc}")
            return False

    async def _share_reaction_text(self, watch_message, *, scene, persona, provider_id):
        prompt = (
            f"你看完了对方在QQ{scene}里分享的这个B站视频，要引用这条视频随口评价一句。\n"
            f"{wrap_untrusted(watch_message[:1600], 'watch_result')}\n"
            "只说一句，基于真正看到的内容；不复述标题，不带链接，不提分析、评分这些内部过程。\n"
            + SHARE_REACTION_SCHEMA_PROMPT
        )
        raw = await self.models._llm_call(prompt, system_prompt=persona, max_tokens=120, provider_id=provider_id)
        if not raw:
            return ""
        try:
            parsed = parse_share_reaction(raw)
        except ContentProtocolError as exc:
            logger.warning(f"[BiliBot] 分享视频评价结构校验失败，保持沉默: {exc}")
            return ""
        return parsed["text"] if parsed["decision"] == "comment" else ""

    async def _react_to_shared_video(self, umo, info, share_record, reply_target, *, scene, user_text):
        """Decide to watch a parsed share; if watched, quote the sent video with one remark."""
        bvid = str(info.get("bvid", "") or "")
        try:
            provider_id = await self.qq_chat._qq_session_provider_id(umo)
            if not provider_id:
                logger.info("[BiliBot] 找不到当前 QQ 会话的聊天模型，跳过分享视频观看")
                return
            persona = await self.qq_chat._qq_session_persona_prompt(umo)
            # 看过且记得内容就不用再决定；只剩“看过但忘了”的痕迹时不重看也不评价。
            if not self.video_watch._cached_video_memory(bvid):
                if await self.seen_videos.has_seen(bvid):
                    return
                if not await self._decide_share_watch(
                    info, scene=scene, user_text=user_text, persona=persona, provider_id=provider_id
                ):
                    logger.info(f"[BiliBot] 分享视频决定不看：{bvid}")
                    return
            result = await self.watch_on_request._watch_video_and_save_memory(bvid, memory_source="qq_share")
            if not result.get("ok") or result.get("seen_only"):
                return
            cached = self.video_watch._cached_video_memory(bvid) or {}
            if isinstance(share_record, dict):
                share_record["watched_summary"] = str(
                    cached.get("summary") or cached.get("analysis") or ""
                )[:260]
            text = await self._share_reaction_text(
                result.get("message", ""), scene=scene, persona=persona, provider_id=provider_id
            )
            if not text:
                return
            try:
                reply_to = await asyncio.wait_for(asyncio.shield(reply_target), timeout=1800)
            except asyncio.TimeoutError:
                reply_to = None
            await self.qq_chat._send_share_reaction(umo, text, reply_to)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[BiliBot] 分享视频观看/评价失败: {exc}")

    async def _tool_watch_followup_text(self, watch_message, *, scene, user_text, persona, provider_id):
        prompt = (
            f"刚才在QQ{scene}里，对方让你看的B站视频你转到后台去看了，现在有结果了，要主动接着说一句。\n"
            f"{wrap_untrusted(watch_message[:1600], 'watch_result')}\n"
        )
        if user_text.strip():
            prompt += f"对方当时的消息：\n{wrap_untrusted(user_text[:300], 'share_message')}\n"
        prompt += (
            "看完了就基于真正看到的内容说一句；结果显示没看成就如实说没看成，不要编内容。"
            "不复述标题，不带链接，不提分析、评分这些内部过程。\n"
            + TOOL_WATCH_FOLLOWUP_SCHEMA_PROMPT
        )
        raw = await self.models._llm_call(prompt, system_prompt=persona, max_tokens=120, provider_id=provider_id)
        if not raw:
            return ""
        try:
            parsed = parse_share_reaction(raw)
        except ContentProtocolError as exc:
            logger.warning(f"[BiliBot] 工具看片结果消息结构校验失败，改发固定提示: {exc}")
            return ""
        return parsed["text"] if parsed["decision"] == "comment" else ""

    async def _send_tool_watch_followup(self, umo, watch_message, *, scene, user_text, failed=False):
        """Tell the conversation that asked how a backgrounded watch ended."""
        text = "刚才那个视频没看成，晚点再试试吧。" if failed else ""
        if not failed:
            try:
                provider_id = await self.qq_chat._qq_session_provider_id(umo)
                if provider_id:
                    persona = await self.qq_chat._qq_session_persona_prompt(umo)
                    text = await self._tool_watch_followup_text(
                        watch_message, scene=scene, user_text=user_text,
                        persona=persona, provider_id=provider_id,
                    )
            except Exception as exc:
                logger.warning(f"[BiliBot] 工具看片结果消息生成失败，改发固定提示: {exc}")
        # 用户还在等结果，模型说不出话也要告诉一声；固定文字不含模型输出。
        await self.qq_chat._send_share_reaction(umo, text or "刚才那个视频有结果了，可以再问问我。", None)
