"""QQ 分享管线：认链接 → 查视频 → 冷却 → 发解析卡 →
并行地发原视频、让 Bot 决定看不看。"""
from __future__ import annotations

import asyncio
import re

from ..infra.host_view import HostView


class ShareHandler(HostView):
    """群聊、私聊的每条消息都会进来；自动、手动命令、LLM 工具三种触发。"""

    HOST_API = frozenset({
        "bili_videos", "qq_chat", "settings", "share_delivery", "share_links", "share_reaction",
        "share_session",
    })

    async def _handle_bili_share(
        self,
        event,
        text_override=None,
        trigger_mode="auto",
        show_errors=False,
    ):
        if not self.settings.get("ENABLE_BILI_SHARE_PARSE"):
            if show_errors:
                yield event.plain_result("⚠️ B站视频解析总开关已关闭，请先用 /bili开关 解析 开启。")
            return
        trigger_keys = {
            "auto": ("BILI_SHARE_PARSE_AUTO_TRIGGER_ENABLED", "自动解析"),
            "manual": ("BILI_SHARE_PARSE_MANUAL_TRIGGER_ENABLED", "手动解析"),
            "llm": ("BILI_SHARE_PARSE_LLM_TRIGGER_ENABLED", "LLM解析"),
        }
        trigger_key, trigger_label = trigger_keys.get(trigger_mode, trigger_keys["auto"])
        msg = (event.message_str or "").strip()
        if text_override is None and (
            msg.startswith("/") or re.match(r"^bili解析(?:\s|$)", msg, re.IGNORECASE)
        ):
            return
        text = str(text_override) if text_override is not None else self.share_links._collect_share_text(event)
        if not text.strip() and trigger_mode in ("manual", "llm"):
            text = self.share_session._get_pending_bili_share_text(event)
        if not text.strip():
            if show_errors:
                yield event.plain_result("⚠️ 没有找到需要解析的B站链接或BV号。")
            return
        target = await self.share_links._extract_bili_share_target(text)
        if trigger_mode == "auto" and target:
            # 自动解析关闭时也记录目标，之后可用 /bili解析 或 LLM 解析“上面那个”。
            self.share_session._remember_pending_bili_share(event, text)
        if not self.settings.get(trigger_key):
            if show_errors:
                yield event.plain_result(
                    f"⚠️ {trigger_label}触发已关闭，请先用 /bili开关 {trigger_label} 开启。"
                )
            return
        if not target and trigger_mode in ("manual", "llm"):
            pending_text = self.share_session._get_pending_bili_share_text(event)
            if pending_text:
                text = pending_text
                target = await self.share_links._extract_bili_share_target(text)
        if not target:
            if show_errors:
                yield event.plain_result("⚠️ 没有识别到有效的B站视频链接、短链或BV号。")
            return
        info = await self._get_video_info_by_share_target(target)
        if not info or not info.get("bvid"):
            if show_errors:
                yield event.plain_result("⚠️ 获取B站视频信息失败，请检查链接或稍后重试。")
            return
        bvid = info.get("bvid", "")
        if self.share_session._share_recent_hit(event, bvid):
            if show_errors:
                yield event.plain_result("⚠️ 这个视频刚刚解析过，冷却结束后可以再次解析。")
            return

        intro = self._share_video_intro(info)
        share_record = self.share_session._remember_recent_group_share(event, info, intro)
        yield event.plain_result(self._build_share_card_text(info, intro))

        # 解析只是把视频下载发出去，不算看过，也不写长期记忆；“上面那个视频”靠 share_record 的会话上下文。
        # 看不看由 Bot 自己决定，和下载发送并行；评价要等视频发出后才能引用它。
        reply_target = asyncio.get_running_loop().create_future()
        self.share_reaction._start_share_reaction(event, info, share_record, reply_target)
        sent_message_id = None
        try:
            async for result in self.share_delivery._send_shared_video(event, bvid, info.get("duration")):
                if isinstance(result, dict):
                    sent_message_id = result.get("message_id") or sent_message_id
                else:
                    yield result
        finally:
            if not reply_target.done():
                reply_target.set_result(sent_message_id or self.qq_chat._event_message_id(event))

    async def _get_video_info_by_share_target(self, target):
        if target.get("bvid"):
            oid = await self.bili_videos._get_video_oid(target["bvid"])
            if not oid:
                return None
            info = await self.bili_videos._get_video_info(oid)
            if info:
                info["oid"] = oid
            return info
        if target.get("aid"):
            info = await self.bili_videos._get_video_info(target["aid"])
            if info:
                info["oid"] = target["aid"]
            return info
        return None

    @staticmethod
    def _format_duration(seconds):
        try:
            seconds = int(seconds or 0)
        except Exception:
            seconds = 0
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _share_video_intro(self, info):
        intro = (info.get("desc") or "这个视频没有简介。").strip()
        intro = re.sub(r"\s+", " ", intro)
        return intro[:260]

    def _build_share_card_text(self, info, intro):
        bvid = info.get("bvid", "")
        link = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
        title = info.get("title") or "未知标题"
        owner = info.get("owner_name") or "未知UP"
        owner_mid = info.get("owner_mid") or "?"
        tname = info.get("tname") or "未知分区"
        intro = (intro or self._share_video_intro(info)).strip()
        lines = [
            "🎞️ B站视频解析",
            "━━━━━━━━━━━━",
            f"标题：{title}",
            f"UP主：{owner}（UID:{owner_mid}）",
            f"分区：{tname} | 时长：{self._format_duration(info.get('duration', 0))}",
        ]
        if link:
            lines.append(f"原链接：{link}")
        lines.extend(["", f"简介：{intro}"])
        if self.settings.get("BILI_SHARE_PARSE_SEND_VIDEO"):
            lines.append("\n请稍等...视频一会发出")
        return "\n".join(lines)
