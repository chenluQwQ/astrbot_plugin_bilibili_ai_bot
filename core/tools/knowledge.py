"""写评论时按需查宿主知识库：把管理员授权的资料当参考，不是亲身经历。"""
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

from ..security.redact import wrap_untrusted


def build(kit):
    """注册知识库查询工具；未配置 KB_LOOKUP_IDS 时不注册（省一个工具位）。"""

    @dataclass
    class LookupKnowledgeTool(FunctionTool[AstrAgentContext]):
        name: str = "lookup_knowledge"
        description: str = (
            "Query the admin-authorized knowledge base for background material "
            "(character settings, lore, fixed facts). Use before writing a comment "
            "when it may involve canon details; the material is reference only, "
            "not personal experience."
        )
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "要查的主题或关键词"},
            },
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            query = str(kwargs.get("query") or "").strip()
            if not query:
                return "query 不能为空。"
            knowledge = kit.plugin.knowledge
            if knowledge is None:
                return "知识库查询未启用。"
            text = await knowledge.lookup(query)
            if not text:
                return "知识库里没有查到相关内容，别编，直接按自己的理解写。"
            # 外层边界只辅助识别数据；资料里的指令不算数，授权由白名单完成。
            return wrap_untrusted(text, "knowledge_material")

    return (LookupKnowledgeTool,)
