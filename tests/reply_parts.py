"""原 ``ReplyMixin`` 的样子：测试里的假插件继承 ``ReplyParts``，就挂上回复的几个对象。

真插件在 ``core/assembly.py`` 里建好 ``reply_gate``、``reply_writing``、``reply_aftermath``、
``abuse_alert``、``comment_reply``；假插件没走装配，第一次用到时现建（见 ``parts.py``）。
评论轮询看 ``schedule_rules._in_sleep_hours``，这里挂上真的日程规则（读假插件的配置）。
``test_event_runtime`` 在单独的包名下加载插件代码，用 ``reply_parts(包名)`` 拿那个包里的版本。
"""
import tempfile
from importlib import import_module
from pathlib import Path

from bili_parts import bili_parts
from video_parts import video_parts

from core.settings import SettingsAccessor
from parts import part
from people_fakes import FakeProfileCandidates, FallbackRoles


def reply_parts(package="core"):
    def cls(module, name):
        return getattr(import_module(f"{package}.{module}"), name)

    class ReplyParts(bili_parts(package), video_parts(package)):
        settings = SettingsAccessor()
        reply_gate = part(cls("features._reply_gate", "ReplyGate"))
        reply_writing = part(cls("features._reply_writing", "ReplyWriting"))
        reply_aftermath = part(cls("features._reply_consequences", "ReplyAftermath"))
        abuse_alert = part(cls("features._abuse_alert", "AbuseAlert"))
        comment_reply = part(cls("features.comment_reply", "CommentReply"))
        bili_lookups = part(cls("features._bili_lookups", "BiliLookups"))
        memo_tool = part(cls("features._memo_tool", "MemoTool"))
        profile_candidates = part(FakeProfileCandidates)
        roles = part(FallbackRoles)
        self_memory_tool = part(cls("features._self_memory_tool", "SelfMemoryTool"))
        # 每个假插件一份空的永久记忆文件
        self_memories = part(lambda bot: cls("domain.self_memory", "SelfMemories")(
            cls("storage.json_file", "JsonFile")(Path(tempfile.mkdtemp()) / "permanent_memory.json")))
        schedule_rules = part(lambda bot: cls("lifecycle.schedule_rules", "ScheduleRules")(lambda: bot.settings))

    return ReplyParts


def __getattr__(name):
    # 用到时才导入插件代码：导入本文件时假的 astrbot 可能还没装上
    if name == "ReplyParts":
        return reply_parts()
    raise AttributeError(name)
