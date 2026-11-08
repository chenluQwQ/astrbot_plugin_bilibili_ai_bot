"""QQ 里让 Bot 解析 B站分享：走和自动解析同一条管线（``features/share.py``），触发方式记作 llm。"""
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass


def build(kit):
    app = kit.app
    plugin = kit.plugin

    @dataclass
    class ParseBiliShareTool(FunctionTool[AstrAgentContext]):
        name: str = "bili_parse_video"
        description: str = (
            "Parse a Bilibili video link, short link, or BV ID and directly send the parse card "
            "and optional original video to the current chat. Use when the user explicitly asks "
            "to parse or send a Bilibili video. Do not call it merely because a quoted message "
            "contains a Bilibili link; the user's current message must show parse intent."
        )
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Bilibili URL/BV ID; use 'recent' when the user means the video above",
                },
            },
            "required": [],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            if not plugin.settings.get("ENABLE_BILI_SHARE_PARSE"):
                return "B站视频解析总开关已关闭，管理员可用 /bili开关 解析 开启。"
            if not plugin.settings.get("BILI_SHARE_PARSE_LLM_TRIGGER_ENABLED"):
                return "LLM解析触发已关闭，管理员可用 /bili开关 LLM解析 开启。"
            target = str(kwargs.get("target", "") or "").strip()
            event = getattr(getattr(context, "context", None), "event", None)
            if event is None:
                return "当前工具调用没有关联聊天事件，无法直接发送解析结果。"
            # 工具仅在 LLM 已判断用户有解析意图后调用；此时才读取引用正文。
            event_text = app.collect_share_text(event, include_reply=True)
            target = f"{target}\n{event_text}" if target else event_text

            sent_count = 0
            async for result in app.handle_share(
                event,
                text_override=target,
                trigger_mode="llm",
                show_errors=True,
            ):
                await event.send(result)
                sent_count += 1
            if not sent_count:
                return "没有识别到可解析的B站视频，请让用户提供完整链接或BV号。"
            return "解析结果已直接发送到当前聊天；不要重复输出解析卡或视频链接。"

    return ParseBiliShareTool,
