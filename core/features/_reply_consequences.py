"""回复之后：先算好感、里程碑、恶意次数和要不要拉黑，再拉黑或发送，送达后统一提交。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime

from astrbot.api import logger
from ..config import LEVEL_NAMES
from ..runtime import ActionRequest
from ..infra.host_view import HostView
from ..domain.emotes import tidy_emotes


@dataclass
class ReplyConsequences:
    """一条评论回复会带来的后果。先算好，再决定拉黑还是发送，发送成功后统一提交。"""

    text: str                       # 真正要发的文字（命中里程碑时换成里程碑那句）
    old_score: int
    new_score: int
    delta: int
    affection_enabled: bool
    milestone: tuple | None = None
    strikes: int | None = None      # 要写回的恶意次数；None 表示不改
    block: bool = False
    bvid: str = ""
    video_title: str = ""

    @property
    def delta_text(self):
        return f"+{self.delta}" if self.delta >= 0 else str(self.delta)


class ReplyAftermath(HostView):
    HOST_API = frozenset({"memory_docs", 
        "affection", "bili_comments", "bili_users", "bili_videos", "blocklist", "event_runtime",
        "json_store", "layered_runtime", "memory_compress", "memory_writer", "milestones",
        "profile_candidates", "reply_log", "roles", "security_log", "settings", "user_profiles",
    })

    async def _commit_reply_signals(
        self, *, event_key, actor_id, actor_name, scope, result
    ):
        """Persist validated feedback only after the public reply was confirmed sent."""
        if not result.get("_protocol_validated"):
            return False
        signals = result.get("signals")
        if not isinstance(signals, dict):
            return False
        feedback_type = str(signals.get("feedback_type") or "none")
        if feedback_type == "none":
            return False
        layered = getattr(self, "layered_runtime", None)
        if layered is None or not layered.is_open:
            return False
        actor_id = str(actor_id or "")
        owner = self.roles.is_owner(actor_id)
        if owner:
            relation_weight = 3.0
        else:
            score = self.affection.score(actor_id)
            level = self.affection.level(score, actor_id)
            relation_weight = {
                "special": 1.8, "close": 1.6, "friend": 1.25,
            }.get(level, 1.0)
        reflection = signals.get("reflection_candidate") or {}
        try:
            created = await layered.feedback.record_candidate(
                event_key=f"{scope}:{event_key}",
                actor_id=actor_id,
                actor_name=actor_name,
                scope=scope,
                feedback_type=feedback_type,
                topic=str(signals.get("feedback_topic") or ""),
                event_summary=str(reflection.get("event") or ""),
                possible_mistake=str(reflection.get("possible_mistake") or ""),
                next_time=str(reflection.get("next_time") or ""),
                confidence=float(signals.get("confidence", 0.0) or 0.0),
                relation_weight=relation_weight,
                is_owner=owner,
            )
            if created:
                logger.info(
                    f"[BiliBot] 🪞 记录反馈候选: {feedback_type} "
                    f"topic={str(signals.get('feedback_topic') or '')[:40]} "
                    f"weight={relation_weight}"
                )
            return created
        except Exception as exc:
            # 回复已成功发送，反馈落库失败不能反过来触发平台重发。
            logger.warning(f"[BiliBot] 反馈候选写入失败，未影响已发送回复: {exc}")
            return False

    async def _apply_reply_result(self, *, mid, username, content, oid, rpid, comment_type, thread_id, result):
        """阶段 13–15：先算出这次回复的后果，再拉黑或者发送，发送成功后在一处提交全部后果。"""
        if not result.get("_protocol_validated") or result.get("decision") != "reply":
            return False
        if self.settings.get("ENABLE_BILI_EMOTES"):
            result = {**result, "reply": tidy_emotes(result["reply"])}
            if not result["reply"]:
                return False
        outcome = await self._decide_reply_consequences(
            mid=mid, username=username, oid=oid, comment_type=comment_type, result=result,
        )
        if outcome.block:
            await self._carry_out_auto_block(outcome, mid=mid, username=username, content=content,
                                             oid=oid, rpid=rpid, comment_type=comment_type)
            return False
        logger.info(f"[BiliBot] 💬 {username}: {outcome.text[:50]}")
        sent = await self.event_runtime.execute(
            ActionRequest(
                key=f"comment_reply:{rpid}",
                kind="comment_reply",
                event_key=f"bilibili:comment:{rpid}",
                target_id=str(rpid),
                priority=0 if self.roles.is_owner(mid) else 20 if outcome.old_score >= 40 else 40,
                metadata={"oid": str(oid), "comment_type": comment_type, "text": outcome.text[:200]},
            ),
            lambda: self.bili_comments._send_reply(oid, rpid, comment_type, outcome.text),
        )
        if sent.success:
            await self._commit_reply_consequences(
                outcome, mid=mid, username=username, content=content,
                oid=oid, rpid=rpid, thread_id=thread_id, result=result,
                own_rpid=getattr(getattr(sent, "value", None), "rpid", 0),
            )
        return sent.success

    async def _decide_reply_consequences(self, *, mid, username, oid, comment_type, result):
        """阶段 13：只读不写。新好感分、里程碑（命中就换掉回复文字）、恶意次数、要不要自动拉黑。"""
        cs = self.affection.score(str(mid))
        sd = result.get("score_delta", 1)
        outcome = ReplyConsequences(
            text=result["reply"], old_score=cs, new_score=cs, delta=sd,
            affection_enabled=bool(self.settings.get("ENABLE_AFFECTION")),
        )

        # ── 解析当前视频来源（comment_type=1 是视频评论区） ──
        if comment_type == 1 and oid:
            try:
                outcome.bvid = await self.bili_videos._oid_to_bvid(oid) or ""
                if outcome.bvid:
                    vc = self.memory_docs.video_memory.read({})
                    outcome.video_title = vc.get(outcome.bvid, {}).get("title", "")
            except Exception:
                pass

        if not outcome.affection_enabled:
            return outcome
        if self.roles.is_owner(mid):
            outcome.new_score = 100
            return outcome
        # cold 等级和自动拉黑阈值依赖负分，因此不能把下限截到 0。
        outcome.new_score = max(-99, min(99, cs + sd))
        outcome.milestone = self.milestones.peek(mid, cs, outcome.new_score, username)
        if outcome.milestone:
            outcome.text = outcome.milestone[1]
        # 自动拉黑：白名单/主人 永不拉黑，阈值与次数可配，开关可关
        auto_block = self.settings.get("ENABLE_AUTO_BLOCK") and not self.roles.never_block(mid)
        block_score = int(self.settings.get("AUTO_BLOCK_SCORE"))
        block_times = int(self.settings.get("AUTO_BLOCK_NEGATIVE_TIMES"))
        if auto_block and outcome.new_score <= block_score:
            outcome.block = True
        if sd <= -3:
            outcome.strikes = self.blocklist.strikes(mid) + 1
            if auto_block and block_times > 0 and outcome.strikes >= block_times:
                outcome.block = True
        elif self.blocklist.has_strikes_record(mid):
            outcome.strikes = 0
        return outcome

    async def _carry_out_auto_block(self, outcome, *, mid, username, content, oid, rpid, comment_type):
        """拉黑路：先回一句「我不想和你说话了。」，送达后再记分、记次数、调拉黑接口。"""
        block_notice = await self.event_runtime.execute(
            ActionRequest(
                key=f"comment_block_notice:{rpid}",
                kind="comment_reply",
                event_key=f"bilibili:comment:{rpid}",
                target_id=str(rpid),
                priority=0,
                metadata={"budget_exempt": True, "safety_action": True},
            ),
            lambda: self.bili_comments._send_reply(
                oid, rpid, comment_type, "我不想和你说话了。"
            ),
        )
        if not block_notice.success:
            return
        self.affection.set(str(mid), outcome.new_score)
        if outcome.strikes is not None:
            self.blocklist.set_strikes(mid, outcome.strikes)
        self.security_log.record(
            "negative", mid, username, content, f"{outcome.old_score}→{outcome.new_score}({outcome.delta_text})"
        )
        block_outcome = await self.event_runtime.execute(
            ActionRequest(
                key=f"comment_block:{mid}:{rpid}",
                kind="block_user",
                event_key=f"bilibili:comment:{rpid}",
                target_id=str(mid),
                priority=0,
                metadata={
                    "budget_exempt": True,
                    "safety_action": True,
                },
            ),
            lambda: self.bili_users._block_user(int(mid)),
        )
        if block_outcome.success:
            logger.info(f"[BiliBot] 🚫 拉黑 {username}")
        elif block_outcome.state == "unknown":
            logger.warning(
                f"[BiliBot] {username}({mid}) 的B站拉黑结果未知，"
                "已停止回复并保留本地隔离，禁止自动重试"
            )
        else:
            logger.warning(
                f"[BiliBot] 拉黑动作未确认成功：{username}({mid})"
            )

    async def _commit_reply_consequences(self, outcome, *, mid, username, content, oid, rpid, thread_id, result, own_rpid=0):
        """阶段 15：回复确认送达后才提交的全部后果，都在这里。"""
        sd = outcome.delta
        if outcome.affection_enabled:
            self.affection.set(str(mid), outcome.new_score)
            # fork 功能：关系互动计数器只在动作确认成功后累加，避免重发时重复计数
            self.user_profiles.record_interaction(mid, username, sd, "comment")
            if outcome.milestone:
                self.milestones.commit(mid, outcome.milestone[0], username)
            logger.info(
                f"[BiliBot] 💛 {outcome.old_score}→{outcome.new_score}（{outcome.delta_text}）| "
                f"{LEVEL_NAMES[self.affection.level(outcome.new_score, mid)]}"
            )
            if outcome.strikes is not None:
                self.blocklist.set_strikes(mid, outcome.strikes)
                if sd <= -3:
                    self.security_log.record(
                        "negative", mid, username, content,
                        f"{outcome.old_score}→{outcome.new_score}({outcome.delta_text})",
                    )
        await self._commit_reply_signals(
            event_key=str(rpid), actor_id=mid, actor_name=username,
            scope="bili_comment", result=result,
        )
        # 回复 JSON 里的 impression / user_facts 先存成画像候选，每晚清算挑着写进画像（core/features/profile_digest.py）
        self.profile_candidates.note(mid, "bili_comment", result.get("impression", ""), result.get("user_facts", []), username)
        if outcome.bvid:
            self.user_profiles.update(
                mid,
                username=username,
                video_encounter={
                    "bvid": outcome.bvid,
                    "title": outcome.video_title,
                    "time": datetime.now().strftime("%Y-%m-%d"),
                },
                source_scope="bili_comment",
            )
        # 写入独立的回复日志（不受记忆压缩影响）
        log_entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "mid": str(mid), "username": username,
            "content": content[:100], "reply": outcome.text[:100],
            "oid": str(oid), "rpid": str(rpid),
            "score_delta": sd,
        }
        if result.get("_context_trace"):
            log_entry["context_calls"] = result["_context_trace"]
        if outcome.bvid:
            log_entry["bvid"] = outcome.bvid
        if outcome.video_title:
            log_entry["video_title"] = outcome.video_title
        if own_rpid:
            log_entry["own_rpid"] = str(own_rpid)  # 自己这条回复的 rpid，WebUI 删评论用
        self.reply_log.record(log_entry)
        # 记忆写入（带视频来源）
        await self.memory_writer._save_memory_record(
            rpid, thread_id, mid, username, content, outcome.text,
            oid=oid, bvid=outcome.bvid, video_title=outcome.video_title,
        )
        await self.memory_compress.compress_thread(thread_id)
        await self.memory_compress.compress_oid(oid)
        await self.memory_compress.compress_user(mid, username, "bili_comment")
