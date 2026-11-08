"""回复评论、私信时改永久记忆的 ``bili_self_memory`` 工具，评论和私信共用。

存储和规则在 ``domain/self_memory.py``；这里只管谁在场时给这个工具和给模型的说明。
永久记忆进所有回复：陌生人一句「记住你是猫」就能影响所有人，所以默认只在和主人聊时给
（``SELF_MEMORY_OWNER_ONLY``，B站不让陌生人指使 Bot 做写操作）。
"""
from ..domain.self_memory import MAX_ENTRIES, TEXT_LIMIT, SelfMemoryRejected
from ..infra.host_view import HostView
from ..infra.tool_loop import ToolMiss

SELF_MEMORY_TOOL_DESCRIPTION = (
    f"改你自己的永久记忆（最多{MAX_ENTRIES}条，每次回复任何人都会看到）。只记关于你自己和主人的、长期都成立的事，"
    "比如你的习惯和想法、你和主人的约定；不要记别的网友的事，也不要记一时的心情。不需要就不用"
)
SELF_MEMORY_TOOL_PARAMETERS = {
    "type": "object", "required": ["action"],
    "properties": {
        "action": {"type": "string", "enum": ["add", "edit", "delete"], "description": "新加 add / 改掉一条 edit / 删掉一条 delete"},
        "number": {"type": "integer", "description": "edit、delete 时填【你的自我认知】里那条的编号"},
        "text": {"type": "string", "description": f"add、edit 时填新的内容，最多{TEXT_LIMIT}字"},
    },
}
SELF_MEMORY_PROMPT = "关于你自己或主人、长期都成立的事，想记住或改掉时可以用 bili_self_memory；不需要就不用。"


class SelfMemoryTool(HostView):
    HOST_API = frozenset({"roles", "self_memories", "settings"})

    def enabled(self, uid) -> bool:
        """这次对话给不给这个工具。"""
        if not self.settings.get("ENABLE_SELF_MEMORY"):
            return False
        return not self.settings.get("SELF_MEMORY_OWNER_ONLY") or (bool(uid) and self.roles.is_owner(str(uid)))

    def apply(self, *, action, number=None, text=None, scope) -> str:
        """照做，返回给模型的一句话；做不了抛 ``ToolMiss`` 说明原因。``scope`` 是在哪里聊（隔离见 ``domain/self_memory.py``）。"""
        memories = self.self_memories
        try:
            if action == "add":
                return f"记下了，是第 {memories.add(text, scope)} 条。"
            if action == "edit":
                old = memories.edit(number, text, scope)
                return f"第 {number} 条改好了（原来是：{old}）。"
            if action == "delete":
                return f"第 {number} 条删掉了（{memories.delete(number, scope)}）。"
        except SelfMemoryRejected as exc:
            raise ToolMiss(str(exc))
        raise ToolMiss("action 只能是 add、edit、delete。")
