"""AstrBot 工具循环的假零件：``FunctionTool``/``ToolSet``、B站事件用到的平台类，和一个照剧本走的工具循环。

``astrbot_tool_modules()`` 给 ``patch.dict(sys.modules, ...)`` 用。``ScriptedToolLoop`` 顶替 ``models.tool_loop``：
剧本里的字符串是模型最后的回复，``call(名字, 参数)`` 是模型先调一次工具（真的跑工具的 handler）。
"""
import sys
import types
from unittest.mock import patch


class FakeFunctionTool:
    def __init__(self, *, name, description, parameters, handler=None):
        self.name, self.description, self.parameters, self.handler = name, description, parameters, handler


class FakeToolSet:
    def __init__(self, tools):
        self.tools = tools


def fake_tool_module():
    module = types.ModuleType("astrbot.core.agent.tool")
    module.FunctionTool = FakeFunctionTool
    module.ToolSet = FakeToolSet
    return module


def fake_platform_modules():
    class MessageType:
        FRIEND_MESSAGE = "FriendMessage"

    class AstrBotMessage:
        pass

    class MessageMember:
        def __init__(self, user_id, nickname=None):
            self.user_id, self.nickname = user_id, nickname

    class PlatformMetadata:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AstrMessageEvent:
        def __init__(self, message_str, message_obj, platform_meta, session_id):
            self.message_str, self.message_obj = message_str, message_obj
            self.platform_meta, self.session_id = platform_meta, session_id
            self.role = "admin"  # 真类默认 member；这里故意给 admin，看我们有没有改回来

        @property
        def unified_msg_origin(self):
            return f"{self.platform_meta.id}:{self.message_obj.type}:{self.session_id}"

        def is_admin(self):
            return self.role == "admin"

    modules = {
        "astrbot.core.platform": types.ModuleType("astrbot.core.platform"),
        "astrbot.core.platform.astr_message_event": types.ModuleType("astrbot.core.platform.astr_message_event"),
        "astrbot.core.platform.astrbot_message": types.ModuleType("astrbot.core.platform.astrbot_message"),
        "astrbot.core.platform.message_type": types.ModuleType("astrbot.core.platform.message_type"),
        "astrbot.core.platform.platform_metadata": types.ModuleType("astrbot.core.platform.platform_metadata"),
    }
    modules["astrbot.core.platform"].__path__ = []
    modules["astrbot.core.platform.astr_message_event"].AstrMessageEvent = AstrMessageEvent
    modules["astrbot.core.platform.astrbot_message"].AstrBotMessage = AstrBotMessage
    modules["astrbot.core.platform.astrbot_message"].MessageMember = MessageMember
    modules["astrbot.core.platform.message_type"].MessageType = MessageType
    modules["astrbot.core.platform.platform_metadata"].PlatformMetadata = PlatformMetadata
    return modules


def astrbot_tool_modules():
    return {"astrbot.core.agent.tool": fake_tool_module(), **fake_platform_modules()}


class WithToolFakes:
    """测试类混入它：每个测试期间装上假的工具类和平台类，测完换回去（QQ 工具的测试要用 conftest 里那份）。"""

    def setUp(self):
        super().setUp()
        patcher = patch.dict(sys.modules, astrbot_tool_modules())
        patcher.start()
        self.addCleanup(patcher.stop)


def call(name, **args):
    return {"call": name, "args": args}


class ScriptedToolLoop:
    """每次被叫到就按剧本往下走：先跑剧本里的工具调用，碰到字符串就当最后的回复返回。

    剧本用完返回 None（等于模型调用失败）；剧本里的异常照样抛出。``calls`` 记下每次交过来的东西，
    ``results`` 记下每次工具调用交回模型的文字。
    """

    def __init__(self, script=(), on_call=None):
        self.script = list(script)
        self.calls = []
        self.results = []
        self._on_call = on_call

    async def __call__(self, prompt, system_prompt="", *, event=None, tools=(), max_steps=None, **kwargs):
        names = [tool.name for tool in tools]
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt, "event": event, "tools": names,
                           "max_steps": max_steps, **kwargs})
        if self._on_call:
            self._on_call(prompt, names)
        by_name = {tool.name: tool for tool in tools}
        while self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            if not isinstance(step, dict):
                return step
            tool = by_name.get(step["call"])
            # AstrBot 碰到不在工具集里的名字，会告诉模型没有这个工具。
            text = await tool.handler(event, **step["args"]) if tool else f"未找到工具 {step['call']}"
            self.results.append((step["call"], text))
        return None
