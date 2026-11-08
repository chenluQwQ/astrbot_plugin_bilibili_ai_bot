"""评论 → 回复管线：拉通知、挑一条、准入、生成、发送、收尾。"""
from __future__ import annotations

import asyncio
import time
import random
import traceback
from dataclasses import dataclass, field

from astrbot.api import logger
from ..config import LEVEL_NAMES, BILI_AT_NOTIFY_URL, BILI_NOTIFY_URL
from ..runtime import EventState, InboundEvent
from ..infra.host_view import HostView
from ..security.inbound import has_block_keyword


@dataclass
class CommentTurn:
    """一条评论从被挑中到处理完的全过程，在各阶段之间传递。"""

    item: dict
    runtime_event: InboundEvent
    claim: object = None
    content_emb: object = None
    image_desc: str = ""
    path: list = field(default_factory=list)

    @property
    def rpid(self):
        return self.item["rpid"]

    @property
    def mid(self):
        return self.item["mid"]

    @property
    def username(self):
        return self.item["username"]

    @property
    def content(self):
        return self.item["content"]

    @property
    def oid(self):
        return self.item["oid"]

    @property
    def comment_type(self):
        return self.item["comment_type"]

    @property
    def thread_id(self):
        return self.item["thread_id"]


class CommentReply(HostView):
    HOST_API = frozenset({
        "_running", "abuse_alert", "affection", "bili_comments", "bili_http",
        "blocklist", "comment_context", "event_runtime", "replied_comments", "replied_content",
        "reply_aftermath", "reply_gate", "reply_writing", "roles", "schedule_rules", "security_log",
        "settings", "tell_owner",
        "vision",
    })

    def __init__(self, host):
        super().__init__(host)
        self._first_poll = True
        self._reply_cooldown_until = 0.0

    def _comment_runtime_event(self, item):
        """把评论/@通知转换为统一事件，并计算跨入口优先级。"""

        mid = str(item.get("mid") or "")
        score = self.affection.score(mid)
        level = self.affection.level(score, mid)
        return InboundEvent(
            source="comment",
            event_id=str(item.get("rpid") or item.get("at_id") or ""),
            actor_id=mid,
            actor_name=item.get("username", ""),
            content=item.get("content", ""),
            conversation_id=str(item.get("thread_id") or ""),
            target_id=str(item.get("oid") or ""),
            account_id=str(self.settings.get("DEDE_USER_ID") or ""),
            occurred_at=float(item.get("timestamp") or 0),
            metadata={
                "notification_source": item.get("source", "reply"),
                "comment_type": item.get("comment_type", 1),
                "at_id": item.get("at_id", ""),
                "is_admin": self.roles.is_owner(mid),
                "direct_mention": item.get("source") == "at",
                "conversation_active": level in ("friend", "close", "special"),
                "interesting": self.roles.always_reply(mid),
            },
        )

    async def _generate_comment_reply(self, ctx, oid, comment_type, thread_id, *, defer=None, resume=None):
        """评论：评论区场景提示词，交给查资料循环（模型可以先要资料再回）。"""
        scene_prompt = (
            f"【场景】你在B站评论区回复别人的评论。这是公开场合，其他人也能看到你的回复。{ctx.security_notice}\n"
            "评论区回复的基本原则：\n"
            "- 先抓住评论里最具体的一个点再回，宁可短一点，也不要复述整句或泛泛表示赞同\n"
            "- 像真人在评论区回一轮话：口语、自然、8-45字，通常一句，确有必要才两句\n"
            "- 玩梗就接梗，认真讨论就回应观点；没看懂的梗不要硬接，也不要装懂\n"
            "- 避免客服腔和万能句：少用“感谢支持”“确实如此”“很高兴你能”“每个人都有”\n"
            "- 回应完这个具体点就收住，不必习惯性反问、邀请继续交流或给出万能祝福\n"
            "- 不要为了显得亲密而乱叫昵称、连续撒娇、堆颜文字或感叹号\n"
            "- 记忆和关系只用于调整语气；没有明确依据时不要编造共同经历，不要提UID、好感度、记忆系统或内部判断\n"
            f"- 这是公开评论区。即使回复{ctx.on}也可以亲近，但不要暴露私聊内容，不要每次都喊“主人”\n"
        )
        target_name = "评论"
        prompt = self.reply_writing._assemble_reply_prompt(ctx, scene_prompt=scene_prompt, target_name=target_name)
        custom_reply_inst = self.settings.get("CUSTOM_REPLY_INSTRUCTION")
        if custom_reply_inst:
            prompt += f"\n\n【补充提示词】{custom_reply_inst}"
        return await self.comment_context._run_comment_reply_flow(
            prompt, ctx.sp, oid, comment_type, thread_id, uid=ctx.mid, nickname=ctx.username,
            content=str(ctx.clean_content or ""), suspicious=ctx.is_suspicious,
            defer=defer, resume=resume,
        )

    async def _poll_unified(self):
        if time.time() < self._reply_cooldown_until:
            return
        try:
            replied = self.replied_comments.load()
            pending = await self._fetch_comment_notifications(replied)
            if self._first_poll:
                self._mark_pending_read_on_first_poll(pending, replied)
                return
            turn = self._pick_next_comment(pending, replied)
            if turn is None:
                return
            try:
                await self._run_comment_turn(turn, replied)
            finally:
                if turn.path:
                    logger.info(f"[BiliBot] 🧭 评论 {turn.rpid}：{' → '.join(turn.path)}")
        except Exception as e:
            logger.error(f"[BiliBot] 轮询出错: {e}\n{traceback.format_exc()}")

    async def _run_comment_turn(self, turn, replied):
        if not await self._claim_comment(turn, replied):
            return
        if not await self._admit_comment(turn):
            return
        turn.image_desc = await self._describe_comment_images(turn)

        async def resume_after_inspection(resume):
            await self._resume_comment_turn(turn, resume)

        result = await self.reply_writing._generate_reply(
            turn.content, turn.mid, turn.username, turn.thread_id, turn.oid, turn.comment_type,
            image_desc=turn.image_desc, comment_defer=resume_after_inspection,
        )
        if (result or {}).get("decision") == "deferred":
            turn.path.append("转后台")
            logger.info(f"[BiliBot] 📹 视频检查转后台，看完再回复：{turn.username}")
            return
        await self._send_comment_turn(turn, result)

    async def _resume_comment_turn(self, turn, resume):
        """视频检查做完后才开始回复；发送仍受回复模型和发送动作各自的时限约束。"""
        try:
            while self.schedule_rules._in_sleep_hours() and getattr(self, "_running", True):
                await asyncio.sleep(60)
            if self.reply_gate._daily_reply_limit_reached("comment"):
                self.security_log.record(
                    "daily_reply_limit", turn.mid, turn.username, turn.content, "评论回复达到管理员硬上限"
                )
                await self._ignore_comment(turn, "daily_reply_limit")
                return
            result = await self.reply_writing._generate_reply(
                turn.content, turn.mid, turn.username, turn.thread_id, turn.oid, turn.comment_type,
                image_desc=turn.image_desc, comment_resume=resume,
            )
            await self._send_comment_turn(turn, result)
        except Exception as e:
            logger.error(f"[BiliBot] 评论后续回复出错: {e}\n{traceback.format_exc()}")
            await self._ignore_comment(turn, "deferred_reply_failed", EventState.FAILED)
        finally:
            logger.info(f"[BiliBot] 🧭 评论 {turn.rpid}：{' → '.join(turn.path)}")

    async def _send_comment_turn(self, turn, result):
        if not await self._accept_generated_reply(turn, result):
            return
        sent = await self.reply_aftermath._apply_reply_result(
            mid=turn.mid, username=turn.username, content=turn.content,
            oid=turn.oid, rpid=turn.rpid, comment_type=turn.comment_type,
            thread_id=turn.thread_id, result=result,
        )
        turn.path.append("已发送" if sent else "未发送")
        await self._finish_comment_turn(turn, result, sent)

    async def _fetch_comment_notifications(self, replied):
        """阶段 1：拉「回复我的」和「@我的」两种通知，去掉已经处理过的。"""
        pending = []
        try:
            d, _ = await self.bili_http._http_get(BILI_NOTIFY_URL, params={"ps": 10, "pn": 1})
            if d["code"] == 0:
                for item in d.get("data", {}).get("items", []):
                    r = item.get("item", {})
                    rpid = str(r.get("source_id", ""))
                    if not rpid or rpid in replied:
                        continue
                    pending.append({
                        "rpid": rpid,
                        "mid": str(item.get("user", {}).get("mid", "")),
                        "username": item.get("user", {}).get("nickname", ""),
                        "content": r.get("source_content", ""),
                        "oid": r.get("subject_id", 0),
                        "comment_type": r.get("business_id", 1),
                        "thread_id": str(r.get("root_id") or rpid),
                        "source": "reply",
                    })
        except Exception as e:
            logger.warning(f"[BiliBot] 回复通知拉取失败: {e}")
        try:
            d, _ = await self.bili_http._http_get(BILI_AT_NOTIFY_URL, params={"ps": 10, "pn": 1})
            if d["code"] == 0:
                for item in d.get("data", {}).get("items", []):
                    at_id = str(item.get("id", ""))
                    if not at_id or self.replied_comments.at_seen(at_id):
                        continue
                    source = item.get("item", {})
                    rpid = str(source.get("source_id", ""))
                    if rpid and rpid in replied:
                        self.replied_comments.remember_at(at_id)
                        continue
                    content = self.bili_comments._strip_at_prefix(source.get("source_content", ""))
                    user = item.get("user", {})
                    pending.append({
                        "rpid": rpid,
                        "mid": str(user.get("mid", "")),
                        "username": user.get("nickname", "") or str(user.get("mid", "")),
                        "content": content,
                        "oid": source.get("subject_id", 0),
                        "comment_type": source.get("business_id", 1),
                        "thread_id": str(source.get("root_id") or rpid or at_id),
                        "source": "at",
                        "at_id": at_id,
                    })
        except Exception as e:
            logger.warning(f"[BiliBot] @通知拉取失败: {e}")
        return pending

    def _mark_pending_read_on_first_poll(self, pending, replied):
        """阶段 2：插件启动后的第一轮，把拉到的全部标成已读，不回复。

        有意如此：停机期间积下的评论不补回，避免一开机就连着回一串旧评论。
        """
        for p in pending:
            if p["rpid"]:
                replied.add(p["rpid"])
            if p.get("at_id"):
                self.replied_comments.remember_at(p["at_id"])
        self.replied_comments.save(replied)
        self.replied_comments.save_at()
        self._first_poll = False
        if pending:
            logger.info(f"[BiliBot] 首次运行，标记 {len(pending)} 条已读")

    def _pick_next_comment(self, pending, replied):
        """阶段 3：去重后按事件优先级排序，这一轮只处理排第一的那条。"""
        # rpid 为唯一主键（没有 rpid 的评论回不了复，后面会被准入拦掉）
        seen_rpids = set()
        unique = []
        for p in pending:
            rpid = p["rpid"]
            if not rpid or rpid in seen_rpids or rpid in replied:
                continue
            seen_rpids.add(rpid)
            unique.append(p)
        if not unique:
            return None
        event_items = [
            (self._comment_runtime_event(candidate), candidate)
            for candidate in unique
        ]
        event_items.sort(
            key=lambda pair: self.event_runtime.event_sort_key(pair[0])
        )
        runtime_event, item = event_items[0]
        return CommentTurn(item=item, runtime_event=runtime_event)

    async def _claim_comment(self, turn, replied):
        """阶段 4、5：认领事件，然后在生成回复之前就标成已处理（之后失败也不会再回）。"""
        rpid = turn.rpid
        claim = await self.event_runtime.claim(turn.runtime_event)
        if not claim.accepted:
            if rpid:
                replied.add(rpid)
                self.replied_comments.save(replied)
            logger.debug(
                f"[BiliBot] 评论事件运行时去重：{rpid} ({claim.reason})"
            )
            return False
        turn.claim = claim
        turn.path.append("新评论")
        if rpid:
            replied.add(rpid)
            self.replied_comments.save(replied)
        if turn.item.get("at_id"):
            self.replied_comments.remember_at(turn.item["at_id"])
            self.replied_comments.save_at()
        return True

    async def _ignore_comment(self, turn, reason, state=EventState.IGNORED):
        turn.path.append(f"{'失败' if state == EventState.FAILED else '忽略'}:{reason}")
        await self.event_runtime.transition(turn.claim.event_key, state, reason)

    async def _admit_comment(self, turn):
        """阶段 6–8：准入。必回对象（主人、@、高好感、白名单）跳过筛选；其余要过低价值过滤、兴趣、概率、语义去重。"""
        mid, username, content = turn.mid, turn.username, turn.content
        if not content or not turn.rpid:
            await self._ignore_comment(turn, "empty_or_missing_reply_id")
            return False
        if self.blocklist.contains(mid):
            await self._ignore_comment(turn, "blocked_user")
            return False
        if has_block_keyword(content):
            self.security_log.record("keyword_blocked", mid, username, content, "关键词过滤")
            await self._ignore_comment(turn, "keyword_blocked")
            return False

        cs = self.affection.score(str(mid))
        lv = self.affection.level(cs, mid)
        logger.info(f"[BiliBot] 🔍 DEBUG comment_type={turn.comment_type} oid={turn.oid}")
        logger.info(f"[BiliBot] 📩 {username}（{LEVEL_NAMES[lv]}|{cs}分）：{content[:50]}")

        # ── 管理员硬上限始终生效；兴趣/低价值筛选只对非必回对象生效 ──
        high_aff = lv in ("friend", "close", "special")
        force_reply = (
            self.roles.is_owner(mid) or turn.item.get("source") == "at"
            or high_aff or self.roles.always_reply(mid)
        )
        if self.reply_gate._daily_reply_limit_reached("comment"):
            self.security_log.record("daily_reply_limit", mid, username, content, "评论回复达到管理员硬上限")
            await self._ignore_comment(turn, "daily_reply_limit")
            return False
        if force_reply:
            turn.path.append("必回")
            logger.info(f"[BiliBot] ✅ 必回（主人/@/高好感/白名单）：{username}")
            return True
        turn.path.append("普通")
        filter_reason = self.reply_gate._interaction_filter_reason(content, "comment")
        if filter_reason:
            self.security_log.record("interaction_filtered", mid, username, content, filter_reason)
            logger.info(f"[BiliBot] 🧹 评论过滤：{username} ({filter_reason})")
            await self._ignore_comment(turn, filter_reason)
            return False
        interested, interest_reason = await self.reply_gate._should_reply_by_interest(content, username, "comment")
        if not interested:
            self.security_log.record("interest_skip", mid, username, content, interest_reason)
            logger.info(f"[BiliBot] 兴趣筛选跳过：{username} ({interest_reason})")
            await self._ignore_comment(turn, "interest_skip")
            return False
        prob = max(0, min(100, int(self.settings.get("REPLY_PROBABILITY_PERCENT"))))
        roll = random.randint(1, 100)
        if roll > prob:
            logger.info(f"[BiliBot] 🎲 概率跳过（掷{roll} > {prob}%）：{username}")
            await self._ignore_comment(turn, "probability_skip")
            return False
        if self.settings.get("ENABLE_SIMILAR_SKIP"):
            repeated, turn.content_emb = await self._is_semantically_repeated(content)
            if repeated:
                self.security_log.record("similar_skip", mid, username, content, "语义相似去重")
                await self._ignore_comment(turn, "semantic_duplicate")
                return False
        return True

    async def _describe_comment_images(self, turn):
        """阶段 9：评论带图就识别，结果作为用户内容的一部分交给生成。"""
        image_desc = ""
        image_urls = await self.bili_comments._get_comment_images(turn.oid, turn.rpid, turn.comment_type)
        if image_urls:
            logger.info(f"[BiliBot] 🖼️ 发现 {len(image_urls)} 张图片，识别中...")
            image_desc = await self.vision._recognize_images(image_urls)
            if image_desc:
                turn.path.append("看图")
                logger.info(f"[BiliBot] 🖼️ 图片内容：{image_desc[:50]}...")
        return image_desc

    async def _accept_generated_reply(self, turn, result):
        """阶段 12 之后：模型选择不回、或者没生成出合格回复，就在这里结束。想找主人说的不管回不回都先递过去。"""
        if (result or {}).get("tell_owner"):
            await self.tell_owner.maybe_tell(result["tell_owner"], uid=turn.mid, username=turn.username,
                                             content=turn.content, channel="comment")
        decision = str((result or {}).get("decision") or "error")
        if decision in {"ignore", "observe"}:
            await self._ignore_comment(turn, f"model_{decision}")
            return False
        if decision != "reply" or not result.get("reply"):
            reason = str((result or {}).get("error") or "reply_generation_failed")
            logger.warning(f"[BiliBot] {turn.username} 回复未生成：{reason}")
            await self._ignore_comment(turn, reason, EventState.FAILED)
            return False
        turn.path.append("生成:reply")
        return True

    async def _finish_comment_turn(self, turn, result, sent):
        """阶段 16：语义去重库、回复冷却、恶意告警。"""
        if sent and self.settings.get("ENABLE_SIMILAR_SKIP"):
            await self._record_replied_content(turn.content, emb=turn.content_emb)

        # 回复冷却：防止短时间内重复回复
        cooldown = max(int(self.settings.get("REPLY_COOLDOWN")), 5)
        self._reply_cooldown_until = time.time() + cooldown

        # 恶意告警：回复完成后异步检查
        try:
            await self.abuse_alert._check_abuse_alert(
                username=turn.username, mid=turn.mid, content=turn.content,
                bot_reply=result.get("reply", ""),
                score_delta=result.get("score_delta", 0),
            )
        except Exception as e:
            logger.debug(f"[BiliBot] 恶意告警检查异常: {e}")

    async def _is_semantically_repeated(self, content):
        """与最近回复过的评论做语义比对，返回 (是否命中, embedding)。"""
        text = (content or "").strip()
        if not text:
            return False, None
        emb = await self.vision._get_embedding(text)
        if not emb:
            return False, None
        threshold = max(0, min(100, int(self.settings.get("REPLY_SIMILARITY_PERCENT")))) / 100.0
        return self.replied_content.has_similar(emb, threshold, self.vision._cosine_similarity), emb

    async def _record_replied_content(self, content, emb=None):
        """回复真正发出后才写入语义去重库。"""
        text = (content or "").strip()
        if not text:
            return
        emb = emb or await self.vision._get_embedding(text)
        if not emb:
            return
        self.replied_content.record(text, emb)
