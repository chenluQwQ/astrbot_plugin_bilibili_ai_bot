"""看片时对外做的事：统一过事件运行时，发给主人的 QQ 消息也在这。"""
from __future__ import annotations


from astrbot.api import logger

from ..infra.host_view import HostView
from ..runtime import ActionRequest, EventPriority


class OutwardActions(HostView):
    HOST_API = frozenset({"context", "event_runtime", "settings"})

    async def _execute_proactive_action(
        self, key, kind, target_id, handler, *, metadata=None
    ):
        """Route proactive work and writes through the shared action queue."""

        outcome = await self.event_runtime.execute(
            ActionRequest(
                key=str(key),
                kind=str(kind),
                event_key=f"bilibili:proactive:{target_id}",
                target_id=str(target_id),
                priority=EventPriority.BACKGROUND,
                metadata={"proactive": True, **dict(metadata or {})},
            ),
            handler,
        )
        if not outcome.success and str(outcome.reason).startswith("budget_exhausted:"):
            logger.info(f"[BiliBot] 🧭 统一行为预算已满，跳过 {kind}: {outcome.reason}")
        elif not outcome.success and outcome.state == "unknown":
            logger.warning(
                f"[BiliBot] 主动动作发送结果未知，不自动重试 {kind}: {key}"
            )
        return outcome

    async def _send_owner_recommend_qq(self, text, bvid):
        umo = str(self.settings.get("OWNER_QQ_UMO") or "").strip()
        if not umo:
            logger.warning("[BiliBot] 跳过 QQ 推荐：未配置 OWNER_QQ_UMO")
            return False
        try:
            from astrbot.api.event import MessageChain
            message = f"{text}\nhttps://www.bilibili.com/video/{bvid}"
            await self.context.send_message(umo, MessageChain().message(message))
            logger.info(f"[BiliBot] ✉️ 已通过 QQ 私信给主人分享：{bvid}")
            return True
        except Exception as exc:
            logger.warning(f"[BiliBot] QQ 推荐发送失败: {exc}")
            return False
