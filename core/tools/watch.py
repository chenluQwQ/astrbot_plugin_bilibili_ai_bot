"""看视频的工具。AstrBot 给每次工具调用限时，所以先等一小会儿，没看完就转后台，看完在原会话说一声。"""
import asyncio
import re
import time

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass


WATCH_INLINE_WAIT = 30
WATCH_MAX_JOBS = 3
WATCH_PENDING_MESSAGE = (
    "[还在看] 这个视频比较长，已转到后台继续，看完会写入视频记忆。"
    "现在还不知道视频内容，不要编造；告诉对方你正在看、看完会说一声就好，不要重复调用这个工具。"
)


def build(kit):
    app = kit.app
    execute_write_action = kit.execute_write_action
    plugin = kit.plugin

    private_share_last_sent = {}
    private_share_lock = asyncio.Lock()
    async def _watch_video_for_tool(bvid, *, force_rewatch=False):
        result = await app.watch_video(
            bvid, memory_source="tool_watch", force_rewatch=force_rewatch
        )
        if result.get("ok"):
            result["message"] += (
                "\n（如需互动可调用 bili_action：点赞/投币/收藏/关注UP主/评论）"
            )
        return result
    # AstrBot 给每次工具调用限时（用户日志里是 300 秒），长视频下载加分析常常超过，
    # 超时会把这次看片整个取消。所以先等一小会儿，没看完就留在后台继续。
    app.tool_watch_jobs = {}
    app.tool_watch_followup_tasks = set()
    watch_followups = {}
    async def _send_watch_followup(umo, message, *, scene, user_text, failed):
        try:
            await app.send_watch_followup(
                umo, message, scene=scene, user_text=user_text, failed=failed
            )
        except Exception as exc:
            logger.warning(f"[BiliBot] 工具看片结果消息发送失败: {exc}")
    def _finish_watch_job(key, task):
        app.tool_watch_jobs.pop(key, None)
        waiting = watch_followups.pop(key, {})
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning(f"[BiliBot] 工具后台看片失败({key}): {error}")
        message = "" if error is not None else str(task.result() or "")
        for umo, (scene, user_text) in waiting.items():
            followup = asyncio.create_task(_send_watch_followup(
                umo, message, scene=scene, user_text=user_text, failed=error is not None,
            ))
            app.tool_watch_followup_tasks.add(followup)
            followup.add_done_callback(app.tool_watch_followup_tasks.discard)
    async def _run_watch_job(key, start_job, event):
        """同一个视频只跑一份：模型重复调用时等已有的任务，不重新下载。"""
        task = app.tool_watch_jobs.get(key)
        if task is None:
            if len(app.tool_watch_jobs) >= WATCH_MAX_JOBS:
                return "⚠️ 手头还有几个视频没看完，先不接新的，晚点再试。"
            task = asyncio.create_task(start_job())
            app.tool_watch_jobs[key] = task
            task.add_done_callback(lambda done: _finish_watch_job(key, done))
        # asyncio.wait 被取消或超时都不会取消 task，工具调用结束后它照常跑完。
        await asyncio.wait({task}, timeout=WATCH_INLINE_WAIT)
        if not task.done():
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if umo:
                watch_followups.setdefault(key, {})[umo] = (
                    app.share_scene(event)[1],
                    str(getattr(event, "message_str", "") or ""),
                )
            return WATCH_PENDING_MESSAGE
        if task.cancelled():
            return "⚠️ 看片被中断了。"
        return task.result()
    async def _watch_video_message(bvid, *, force_rewatch=False):
        return (await _watch_video_for_tool(bvid, force_rewatch=force_rewatch))["message"]
    @dataclass
    class WatchVideoTool(FunctionTool[AstrAgentContext]):
        name: str = "watch_video"
        description: str = "Read watched video memories first; analyze and save a new Bilibili video once. Rewatch only on an explicit administrator request. Can then use interaction tools."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "BV ID, e.g. BV1xx411x7xx"},
                "force_rewatch": {"type": "boolean", "default": False, "description": "Only true when administrator explicitly says to rewatch. Repeated links/questions use memories."},
            },
            "required": ["bvid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            event = getattr(getattr(context, "context", None), "event", None)
            force = False
            if kwargs.get("force_rewatch") is True and event is not None:
                import re
                try:
                    text = str(getattr(event, "message_str", "") or "")
                    force = bool(event.is_admin()) and bool(re.search(r"重新\s*看|重看", text))
                except Exception:
                    force = False
            bvid = str(kwargs.get("bvid", "") or "").strip()
            return await _run_watch_job(
                f"watch:{bvid.lower()}",
                lambda: _watch_video_message(bvid, force_rewatch=force),
                event,
            )
    async def _watch_and_share_private(receiver_mid, requested_bvid, cooldown, key):
        async with private_share_lock:
            now = time.monotonic()
            remaining = cooldown - (now - private_share_last_sent.get(key, 0))
            if remaining > 0:
                return f"⚠️ 该视频刚刚已分享，约 {int(remaining) + 1} 秒后才能再次测试。"

            result = await _watch_video_for_tool(requested_bvid)
            if not result["ok"]:
                return result["message"]
            share_bucket = int(time.time() // max(1, cooldown or 1))
            share_outcome = await execute_write_action(
                f"tool_private_share:{receiver_mid}:{result['bvid']}:{share_bucket}",
                "private_video_share",
                receiver_mid,
                lambda: app.send_private_video_share(
                    receiver_mid, result["video_info"]
                ),
            )
            if not share_outcome.success:
                if share_outcome.state == "unknown":
                    return (
                        f"{result['message']}\n\n"
                        "⚠️ 视频已看完并写入记忆，但 B站原生视频卡片发送结果未知；"
                        "请求可能已经送达，请勿自动重试，先人工核查私信。"
                    )
                return (
                    f"{result['message']}\n\n"
                    "⚠️ 视频已看完并写入记忆，但 B站原生视频卡片发送失败；请检查 Cookie、CSRF、接收 UID 和风控日志。"
                )
            actual_key = f"{receiver_mid}:{result['bvid'].lower()}"
            sent_at = time.monotonic()
            private_share_last_sent[key] = sent_at
            private_share_last_sent[actual_key] = sent_at
            return (
                f"[已看完并私信分享给主人]\n"
                f"视频：{result['video_info'].get('title', result['bvid'])}\n"
                f"BV号：{result['bvid']}\n"
                f"接收 UID：{receiver_mid}\n"
                "已先完成视频分析和记忆写入，再发送 B站原生视频卡片。"
            )
    @dataclass
    class WatchAndShareVideoPrivateTool(FunctionTool[AstrAgentContext]):
        name: str = "watch_and_share_video_private"
        description: str = (
            "Watch a Bilibili video completely, save the video memory, then send its native "
            "Bilibili share card to the owner's Bilibili UID. Only call when the user "
            "explicitly asks to watch and privately share a video with the owner."
        )
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "bvid": {"type": "string", "description": "BV ID, e.g. BV1xx411x7xx"},
            },
            "required": ["bvid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            if not plugin.settings.get("BILI_PRIVATE_SHARE_TOOL_ENABLED"):
                return "⚠️ 看完后私信给主人功能未开启，请管理员先在插件配置中开启。"
            receiver_mid = str(
                plugin.settings.get("OWNER_MID") or ""
            ).strip()
            if not receiver_mid.isdigit():
                return "⚠️ 主人的B站 UID（OWNER_MID）未配置或格式错误。"
            if receiver_mid == str(plugin.settings.get("DEDE_USER_ID") or "").strip():
                return "⚠️ 主人的B站 UID 不能与当前 Bot 的B站 UID 相同，否则无法给自己发私信。"

            requested_bvid = str(kwargs.get("bvid", "") or "").strip()
            if not requested_bvid:
                return "请提供要观看并私信分享的视频 BV 号。"
            try:
                cooldown = max(
                    0,
                    int(plugin.settings.get("BILI_PRIVATE_SHARE_COOLDOWN") or 0),
                )
            except (TypeError, ValueError):
                cooldown = 60
            key = f"{receiver_mid}:{requested_bvid.lower()}"
            return await _run_watch_job(
                f"share:{key}",
                lambda: _watch_and_share_private(receiver_mid, requested_bvid, cooldown, key),
                getattr(getattr(context, "context", None), "event", None),
            )
    @dataclass
    class WatchVideosTool(FunctionTool[AstrAgentContext]):
        name: str = "bili_watch_videos"
        description: str = "Trigger proactive video browsing on Bilibili. Use when user asks to watch/browse videos."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {},
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            return await app.watch_videos_report()

    return WatchVideoTool, WatchAndShareVideoPrivateTool, WatchVideosTool
