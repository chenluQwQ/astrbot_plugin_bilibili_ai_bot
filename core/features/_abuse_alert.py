"""恶意告警：被人攻击时用人设口吻通过 QQ 告诉主人。"""
from __future__ import annotations

from astrbot.api import logger
from ..infra.host_view import HostView


class AbuseAlert(HostView):
    HOST_API = frozenset({"context", "models", "settings"})

    async def _check_abuse_alert(self, *, username: str, mid: str,
                                  content: str, bot_reply: str, score_delta: int):
        """检测恶意评论并通过 QQ 私信通知主人。"""
        mode = self.settings.get("ABUSE_ALERT_MODE").lower().strip()
        if mode == "off":
            return

        # 留空发到主人 QQ 私聊
        umo = (self.settings.get("ABUSE_ALERT_QQ_UMO") or self.settings.get("OWNER_QQ_UMO") or "").strip()
        if not umo:
            return

        threshold = int(self.settings.get("ABUSE_ALERT_SCORE_THRESHOLD"))

        # score 模式：直接看 score_delta
        if mode == "score":
            if score_delta <= threshold:
                await self._send_abuse_alert(
                    umo=umo, username=username, mid=mid,
                    content=content, bot_reply=bot_reply,
                    score_delta=score_delta, detail="",
                )
            return

        # model 模式：score_delta 触发阈值后再调模型二次确认
        if mode == "model":
            if score_delta > threshold:
                return  # 分数没到阈值，跳过

            detail = await self._model_judge_abuse(username, content, bot_reply)
            if detail:  # 模型确认有恶意
                await self._send_abuse_alert(
                    umo=umo, username=username, mid=mid,
                    content=content, bot_reply=bot_reply,
                    score_delta=score_delta, detail=detail,
                )
            else:
                logger.debug(f"[BiliBot] 模型二次判断：{username} 非恶意，跳过告警")

    async def _model_judge_abuse(self, username: str, content: str, bot_reply: str) -> str:
        """调模型二次确认是否为恶意攻击，返回判断说明（空字符串=非恶意）。"""
        try:
            prompt = (
                f"请判断以下B站评论是否属于对Bot的恶意攻击（辱骂、人身攻击、持续骚扰、恶意引战等）。\n\n"
                f"用户「{username}」的评论：{content[:300]}\n"
                f"Bot的回复：{bot_reply[:200]}\n\n"
                f"如果是恶意攻击，用一句话概括恶意类型和严重程度。\n"
                f"如果只是普通的不友善、开玩笑、吐槽、或正常批评，回复「无」。\n"
                f"只回复概括或「无」，不要其他内容。"
            )
            result = await self.models._llm_call(prompt, max_tokens=100)
            if not result:
                return ""
            result = result.strip()
            if result == "无" or len(result) <= 1:
                return ""
            return result
        except Exception as e:
            logger.debug(f"[BiliBot] 恶意二次判断失败: {e}")
            return ""

    async def _send_abuse_alert(self, *, umo: str, username: str, mid: str,
                                 content: str, bot_reply: str,
                                 score_delta: int, detail: str):
        """用人设口吻通过 QQ 私信告诉主人有人攻击，询问是否拉黑。"""
        try:
            sp = await self.models._get_system_prompt()
            severity = "不太友善" if score_delta >= -4 else "很过分地辱骂"
            detail_note = f"（{detail}）" if detail else ""

            gen_prompt = (
                f"【情境】你在B站被人恶意攻击了，现在要向主人倾诉这件事并询问是否要拉黑对方。\n\n"
                f"事件详情：\n"
                f"- 用户「{username}」（UID: {mid}）对你说了{severity}的话{detail_note}\n"
                f"- 他的评论原文：{content[:200]}\n"
                f"- 你的回复：{bot_reply[:150]}\n\n"
                f"请用你自己的语气和性格向主人描述这件事，要包含对方的UID（{mid}），"
                f"最后问主人要不要拉黑这个人。\n"
                f"语气自然，像在跟亲近的人撒娇/倾诉，不要用模板化格式，2~4句话。"
            )
            msg = await self.models._llm_call(gen_prompt, system_prompt=sp, max_tokens=200)
            if not msg or len(msg) > 500:
                # 兜底：直接发事实
                msg = (
                    f"呜…B站有个人骂我，UID是{mid}，叫{username}。\n"
                    f"他说：{content[:100]}\n"
                    f"要拉黑他吗？"
                )

            from astrbot.api.event import MessageChain
            chain = MessageChain().message(msg)
            await self.context.send_message(umo, chain)
            logger.info(f"[BiliBot] 🔔 恶意告警已发送 → QQ | {username}({mid}): {content[:30]}")
        except Exception as e:
            logger.warning(f"[BiliBot] 恶意告警发送失败: {e}")
