"""各组工具共用的东西：插件和 ``BotApp``、Bot 和主人的身份、写操作排队、结果不确定时的说法。"""
import hashlib

from ..app import app_of
from ..runtime import ActionRequest, EventPriority


class ToolKit:
    def __init__(self, plugin):
        self.plugin = plugin
        self.app = app_of(plugin)
        # Bot 自身身份信息，供工具描述和调用使用
        self.bot_uid = plugin.settings.str("DEDE_USER_ID") or "未知"
        self.owner_uid = plugin.settings.str("OWNER_MID") or "未知"
        self.owner_name = plugin.settings.str("OWNER_NAME") or "未知"

    async def execute_write_action(self, key, kind, target_id, handler, *, metadata=None):
        """Put every LLM-triggered Bilibili write through the shared queue."""

        return await self.app.event_runtime.execute(
            ActionRequest(
                key=str(key),
                kind=str(kind),
                event_key="bilibili:tool:write",
                target_id=str(target_id),
                priority=EventPriority.DIRECT_MENTION,
                metadata={"tool_action": True, **dict(metadata or {})},
            ),
            handler,
        )

    @staticmethod
    def short_digest(value):
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def write_outcome_text(outcome, success_text, failure_text, action_name):
        """Never turn an ambiguous remote write into a retryable 'failed' result."""
        if outcome.success:
            return success_text
        if outcome.state == "unknown":
            return (
                f"⚠️ {action_name}结果未知：请求可能已经到达B站。"
                "请勿自动重试，先人工核查远端状态。"
            )
        reason = str(outcome.reason or "")
        if reason.startswith("budget_exhausted:"):
            return f"{action_name}没有执行：今天这类操作的次数用完了。"
        if outcome.duplicate:
            return f"{action_name}已经在排队或处理过了，不用再发一次。"
        return failure_text
