"""BiliBot LLM 工具定义（FunctionTool 模式）。
工具返回的字符串会回到 LLM，由 LLM 用人设风格重新生成回复。

按组分在 ``recall``、``share``、``lookup``、``watch``、``actions``、``bangumi``、``feedback``；共用的在 ``_kit``。
工具只经 ``BotApp``（``core/app.py``）的公开方法用插件。
"""
from . import actions, bangumi, feedback, lookup, recall, share, watch
from ._kit import ToolKit
from .watch import WATCH_INLINE_WAIT, WATCH_MAX_JOBS, WATCH_PENDING_MESSAGE  # noqa: F401


def create_tools(plugin):
    """创建所有工具实例，通过闭包捕获 plugin 引用。"""
    kit = ToolKit(plugin)
    app = kit.app
    RecallBiliTool, = recall.build(kit)
    ParseBiliShareTool, = share.build(kit)
    SearchBilibiliTool, = lookup.build(kit)
    WatchVideoTool, WatchAndShareVideoPrivateTool, WatchVideosTool = watch.build(kit)
    BiliActionTool, BlockBiliUserTool = actions.build(kit)
    BiliBangumiTool, = bangumi.build(kit, SearchBilibiliTool)
    OwnerFeedbackTool, = feedback.build(kit)

    # ── 实际执行守卫：开关、可信身份、分层授权与一次性写确认 ──

    def _guard_tool(tool):
        original_call = tool.call
        if tool.name in {
            "bili_action",
            "bili_block_user",
            "bili_watch_videos",
            "watch_and_share_video_private",
            "bili_bangumi",
        }:
            tool.description = (
                f"{tool.description} Write subactions require admin confirmation; "
                "after the user's exact '确认' reply, repeat the call with identical arguments."
            )

        async def guarded_call(context, **kwargs):
            if not plugin.settings.get("ENABLE_LLM_TOOLS"):
                return "⚠️ LLM工具已关闭，管理员可用 /bili开关 工具 开启。"
            runtime = app.layered_runtime
            if runtime is None:
                return "⚠️ 安全授权层未启动，工具调用已拒绝。"
            try:
                decision = await runtime.authorize_tool_call(
                    tool.name, context, dict(kwargs)
                )
            except Exception:
                return "⚠️ 授权校验失败，工具调用已拒绝。"
            if not decision.allowed:
                if decision.reason.startswith("请确认以下写操作"):
                    return decision.reason
                return f"⚠️ {decision.reason}"
            return await original_call(context, **kwargs)

        tool.call = guarded_call
        return tool

    # 默认只向模型暴露少量、职责清晰的入口。
    tools = [
        RecallBiliTool(),
        ParseBiliShareTool(),
        SearchBilibiliTool(),
        WatchVideoTool(),
        BiliBangumiTool(),
        BiliActionTool(),
        WatchVideosTool(),
        # 拉黑具有更高风险，保留独立工具名和独立确认语义。
        BlockBiliUserTool(),
    ]
    # 分享后追问开着才给：主人回答喜不喜欢时记进口味
    if plugin.settings.float("ASK_OWNER_FEEDBACK_CHANCE", minimum=0.0, maximum=1.0) > 0:
        tools.append(OwnerFeedbackTool())
    # 私信分享工具默认隐藏，只有管理员显式开启后才注册给 LLM。
    if plugin.settings.get("BILI_PRIVATE_SHARE_TOOL_ENABLED"):
        tools.append(WatchAndShareVideoPrivateTool())
    return [_guard_tool(t) for t in tools]
