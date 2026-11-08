"""主人对分享视频的反馈：分享后 Bot 问了一句喜不喜欢，主人回答后用 ``bili_owner_feedback`` 记进主人口味（只给管理员）。"""
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass


def build(kit):
    app = kit.app

    @dataclass
    class OwnerFeedbackTool(FunctionTool[AstrAgentContext]):
        name: str = "bili_owner_feedback"
        description: str = (
            "Record whether the owner liked a Bilibili video you shared, right after the owner answers your "
            "'do you like it' question. Only call when the owner clearly says they like or dislike it."
        )
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "liked": {"type": "boolean", "description": "true if the owner liked it"},
                "points": {"type": "array", "items": {"type": "string"},
                           "description": "up to 3 short words: what the owner liked or disliked about it"},
                "bvid": {"type": "string", "description": "optional; defaults to the video you just asked about"},
            },
            "required": ["liked"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            liked = kwargs.get("liked")
            if not isinstance(liked, bool):
                return "liked 要填 true 或 false。"
            points = kwargs.get("points") or []
            if not isinstance(points, list):
                points = [points]
            return app.record_owner_feedback(liked, points, str(kwargs.get("bvid") or ""))

    return OwnerFeedbackTool,
