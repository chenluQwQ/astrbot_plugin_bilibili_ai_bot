"""QQ 分享管线：认链接 → 查视频 → 冷却 → 发解析卡 →
并行地发原视频、让 Bot 决定看不看。"""
from __future__ import annotations

import asyncio
import re

from ..infra.host_view import HostView

_COMMAND_WORD = re.compile(r"^bili(?:umo|[\u4e00-\u9fff]{2,6})(?:\s|$)", re.IGNORECASE)


REGISTERED_BILI_COMMANDS = (
    "bili解析", "bili登录", "bili确认", "bili状态", "bili计划", "bili分区",
    "bili启动", "bili停止", "bili主动", "bili开关", "bili刷新", "bili记忆",
    "bili清算", "bili周总结", "bili清理老化", "bili联动", "bili迁移记忆",
    "bili好感", "bili拉黑", "bili解黑", "bili黑名单", "bili清理", "bili清理旧",
    "bili导出", "bili性格", "bili兴趣", "bili性格编辑", "bili性格删除",
    "bili性格回滚", "bili测试查询", "bili日志", "bili永久记忆", "bili备忘录",
    "bili动态", "bili看番", "bili番剧记忆", "biliUMO", "bili帮助", "bili绑定",
    "bili解绑",
)
_BILI_COMMAND_RE = re.compile(
    r"^(?:"
    + "|".join(re.escape(cmd) for cmd in sorted(REGISTERED_BILI_COMMANDS, key=len, reverse=True))
    + r")(?:\s|$)",
    re.IGNORECASE,
)
_COMMAND_FILTER_NAMES = frozenset({"CommandFilter", "CommandGroupFilter"})


class ShareHandler(HostView):
    """群聊、私聊的每条消息都会进来；自动、手动命令、LLM 工具三种触发。"""

    HOST_API = frozenset({
        "bili_videos", "qq_chat", "settings", "share_delivery", "share_links", "share_reaction",
        "share_session",
    })

    @staticmethod
    def _component_type_name(comp) -> str:
        if isinstance(comp, dict):
            return str(comp.get("type", "")).lower()
        raw_type = getattr(comp, "type", None)
        if raw_type is not None:
            val = getattr(raw_type, "value", raw_type)
            if isinstance(val, str) and val.strip():
                return val.strip().lower()
        return type(comp).__name__.lower()

    @classmethod
    def _plain_component_text(cls, comp):
        cname = cls._component_type_name(comp)
        if isinstance(comp, dict):
            if cname in {"plain", "text"}:
                data = comp.get("data")
                if isinstance(data, dict) and isinstance(data.get("text"), str):
                    return data["text"]
                if isinstance(comp.get("text"), str):
                    return comp["text"]
            return None
        if cname in {"plain", "text"} or isinstance(getattr(comp, "text", None), str):
            return str(getattr(comp, "text", ""))
        return None

    @staticmethod
    def _is_command_text(text) -> bool:
        stripped = str(text or "").strip()
        if not stripped:
            return False
        return stripped.startswith("/") or bool(_BILI_COMMAND_RE.match(stripped))

    @classmethod
    def _is_command_event(cls, event) -> bool:
        """判断消息是否为命令；WakingCheckStage 会剥离 wake_prefix（如 /），需同时查原始前缀和已激活命令。"""
        if cls._is_command_text(getattr(event, "message_str", "")):
            return True

        msg_obj = getattr(event, "message_obj", None)
        raw_msg = getattr(msg_obj, "message_str", None)
        if isinstance(raw_msg, str) and cls._is_command_text(raw_msg):
            return True

        chain = getattr(msg_obj, "message", None)
        if isinstance(chain, (list, tuple)):
            for comp in chain:
                cname = cls._component_type_name(comp)
                if cname in {"at", "atall", "reply"}:
                    continue
                text = cls._plain_component_text(comp)
                if text is None:
                    break
                if text.strip():
                    if cls._is_command_text(text):
                        return True
                    break

        get_extra = getattr(event, "get_extra", None)
        if callable(get_extra):
            parsed_params = get_extra("handlers_parsed_params")
            if isinstance(parsed_params, dict) and parsed_params:
                return True
            activated = get_extra("activated_handlers")
            if isinstance(activated, (list, tuple)):
                for handler in activated:
                    for flt in getattr(handler, "event_filters", None) or ():
                        if type(flt).__name__ in _COMMAND_FILTER_NAMES:
                            return True
        return False

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
        # AstrBot 先把唤醒前缀「/」去掉再交给插件，「/bili测试查询 BV…」到这里是「bili测试查询 BV…」。
        # 结合已注册命令名、原始消息前缀及已激活命令过滤器，避免自动分享解析抢先截断命令。
        if text_override is None and trigger_mode == "auto" and self._is_command_event(event):
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
