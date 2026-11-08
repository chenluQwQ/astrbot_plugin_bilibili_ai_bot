"""看到好视频推荐给主人：今天推荐过什么、还能不能推、用什么方式发。"""
from __future__ import annotations

import re
from datetime import datetime

from astrbot.api import logger

from ..content_protocol import ContentProtocolError, RECOMMENDATION_SCHEMA_PROMPT, parse_recommendation
from ..infra.host_view import HostView


class OwnerRecommend(HostView):
    HOST_API = frozenset({"states", "_owner_last_recommended_at", "_owner_recommended_bvids", "bili_comments", "bili_messages", "config", "json_store", "models", "outward_actions", "owner_feedback", "settings", "video_evaluate"})

    def _owner_recommend_delivery(self):
        # The boolean switch is the page-level capability toggle.  Keep the
        # legacy string value for the delivery-mode selector and old configs.
        if "ENABLE_OWNER_RECOMMEND" in self.config and not bool(self.settings.get("ENABLE_OWNER_RECOMMEND")):
            return "off"
        value = str(
            self.settings.get("RECOMMEND_OWNER_DELIVERY") or ""
        ).strip().lower()
        aliases = {
            "private": "private_message",
            "dm": "private_message",
            "私信": "private_message",
            "comment": "comment",
            "评论": "comment",
            "both": "both",
            "两者": "both",
            "qq": "qq_private",
            "qq_private": "qq_private",
            "bili_private_and_qq": "bili_private_and_qq",
            "all": "all",
            "off": "off",
            "关闭": "off",
        }
        value = aliases.get(value, value)
        return value if value in {"private_message", "comment", "both", "qq_private", "bili_private_and_qq", "all", "off"} else "private_message"

    @staticmethod
    def _is_owner_recommend_action(action):
        return "推荐给主人" in str(action or "")

    def _load_owner_recommendation_state(self, watch_log):
        """Import successful historical shares once, independent of capped watch logs."""
        ledger = self.states.owner_recommend.read({})
        ledger = ledger if isinstance(ledger, dict) else {}
        changed = False
        for item in watch_log if isinstance(watch_log, list) else []:
            if not isinstance(item, dict) or not item.get("bvid"):
                continue
            if not any(self._is_owner_recommend_action(action) for action in item.get("actions", []) or []):
                continue
            bvid = str(item["bvid"])
            if bvid not in ledger:
                ledger[bvid] = {"time": item.get("recommended_at") or item.get("time", ""), "title": item.get("title", "")}
                changed = True
        if changed:
            self.states.owner_recommend.write(ledger)
        records = [item for item in ledger.values() if isinstance(item, dict)]
        self._host._owner_recommended_bvids = set(ledger)
        self._host._owner_last_recommended_at = max((str(item.get("time", "")) for item in records), default="")
        today = datetime.now().strftime("%Y-%m-%d")
        count = sum(1 for item in records if str(item.get("time", "")).startswith(today))
        return ledger, count

    def _can_recommend_owner(self, evaluation, score, recommended_today):
        if self._owner_recommend_delivery() == "off":
            return False
        if not evaluation.get("recommend_owner", False):
            return False
        try:
            gap = max(0, int(self.settings.get("RECOMMEND_OWNER_MIN_GAP_MINUTES")))
            last = getattr(self, "_owner_last_recommended_at", "")
            if gap and last and (datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M")).total_seconds() < gap * 60:
                return False
        except (TypeError, ValueError):
            pass
        try:
            min_score = int(self.settings.get("RECOMMEND_OWNER_MIN_SCORE"))
        except (TypeError, ValueError):
            min_score = 7
        min_score = max(1, min(10, min_score))
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return False
        if not min_score <= score <= 10 or not str(evaluation.get("recommend_reason") or "").strip():
            return False
        try:
            daily_limit = int(self.settings.get("RECOMMEND_OWNER_DAILY_LIMIT"))
        except (TypeError, ValueError):
            daily_limit = 3
        daily_limit = max(0, daily_limit)
        return not daily_limit or recommended_today < daily_limit

    async def _recommend_video_to_owner(self, watch_round, video, oid, video_description, evaluation, review, actions):
        """写一句随手分享，按配置的渠道发给主人；模型选择不分享时什么都不发。"""
        bvid = video["bvid"]
        on = self.settings.get("OWNER_NAME") or "主人"
        owner_mid = str(self.settings.get("OWNER_MID") or "").strip()
        owner_bili = self.settings.get("OWNER_BILI_NAME")
        delivery = self._owner_recommend_delivery()
        try:
            rec_reason = evaluation.get("recommend_reason", "")
            owner_interest = await self.video_evaluate._owner_recommendation_context(
                f"{video.get('title', '')} {(video_description or '')[:500]}"
            )
            delivery_scene = {
                "private_message": "通过B站私信把链接分享给对方",
                "comment": "在视频评论区@对方",
                "both": "通过B站私信分享，并在视频评论区@对方",
                "qq_private": "通过QQ私信把链接分享给对方",
                "bili_private_and_qq": "通过B站私信和QQ私信分享",
                "all": "通过B站私信、QQ私信分享，并在视频评论区@对方",
            }.get(delivery, "通过B站私信把链接分享给对方")
            rec_prompt = f"""你刚看完一个B站视频，自己发现了值得分享的具体点，想告诉{on}。现在要{delivery_scene}，请写一句自然的随手分享，不必假定对方也有相同爱好。

视频信息：
- 标题：「{video.get('title', '')}」
- 视频内容：{(video_description or '')[:320]}
- 你看完的感想：{review or '挺有意思的'}
- 你想推荐给ta的原因：{rec_reason or '单纯想分享'}
{('- 对方画像与相关记忆（只使用其中明确的信息）：' + owner_interest) if owner_interest else ''}

只写推荐时附带的那句话。要求：
- 优先提一个自己真的看完后在意的具体点；有可靠兴趣线索时再轻轻带出为什么想到对方，不必每次直说“想到你”
- 没有可靠兴趣线索就只说自己的真实感受，像熟人顺手丢来一个东西，不假装了解对方
- 自己的爱好不是对方的爱好。没有对方明确反馈，不能因自己喜欢或以前分享过就写“你肯定喜欢”
- 不照搬“推荐理由”字段，不总结整部视频，不写“我为你找到了”或“给你推荐一个”
- 私信语气可以更松一点；评论区语气要让路人看到也能理解，不暴露私下记忆
- 禁止“快来看”“超好看”“强烈推荐”“不看后悔”“墙裂安利”等催促和营销腔
- 不复述完整标题，不要堆感叹号、连续撒娇或以问题句催对方回应
- 12-42字，通常一句，说完自然收住
- 不要带@符号、不要带人名或称呼（系统会按发送方式处理）
- 写不自然、没有具体看点或这一刻并不想分享时，可以放弃；缺少对方的兴趣记录不影响自然分享
{RECOMMENDATION_SCHEMA_PROMPT}"""
            custom_rec_inst = self.settings.get("CUSTOM_RECOMMEND_INSTRUCTION")
            if custom_rec_inst:
                rec_prompt += f"\n【补充提示词】{custom_rec_inst}"
            rec_output = await getattr(self.models, "_generation_llm_call", self.models._llm_call)(rec_prompt, system_prompt=await self.models._get_system_prompt(), max_tokens=180)
            try:
                rec_result = parse_recommendation(rec_output)
            except ContentProtocolError as exc:
                logger.warning(f"[BiliBot] 主人推荐结构校验失败，放弃分享: {exc}")
                rec_result = {"decision": "skip", "text": ""}
            if rec_result["decision"] != "share":
                raise ContentProtocolError("recommendation_skipped")
            rec_text = re.sub(r'@\S+\s*', '', rec_result["text"])
            rec_text = re.sub(r'[\r\n]+', ' ', rec_text).strip(' "“”\'')[:48]
            owner_name = (self.settings.get("OWNER_NAME") or "").strip()
            _name_patterns = ["主人", "亲爱的"] + ([re.escape(owner_name)] if owner_name else [])
            rec_text = re.sub(rf'^({"|".join(_name_patterns)})[，,\s]*', '', rec_text)
            sent_owner_recommend = False
            if delivery in {"private_message", "both", "bili_private_and_qq", "all"}:
                if owner_mid:
                    private_msg = (
                        f"{rec_text}\n"
                        f"https://www.bilibili.com/video/{bvid}"
                    )
                    if (
                        await self.outward_actions._execute_proactive_action(
                            f"proactive_owner_private:{bvid}",
                            "proactive_owner_recommend",
                            owner_mid,
                            lambda: self.bili_messages._send_bili_private_message(
                                owner_mid, private_msg
                            ),
                        )
                    ).success:
                        actions.append("✉️私信推荐给主人")
                        sent_owner_recommend = True
                        logger.info(
                            f"[BiliBot] ✉️ 已通过B站私信给主人分享：{bvid}"
                        )
                else:
                    logger.warning(
                        "[BiliBot] 跳过B站私信推荐：未配置 OWNER_MID"
                    )
            if delivery in {"comment", "both", "all"}:
                if owner_bili:
                    rec_msg = f"@{owner_bili} {rec_text}"
                    if (
                        await self.outward_actions._execute_proactive_action(
                            f"proactive_owner_comment:{bvid}",
                            "proactive_owner_recommend",
                            bvid,
                            lambda: self.bili_comments._send_comment(oid, rec_msg),
                        )
                    ).success:
                        actions.append("📢评论区推荐给主人")
                        sent_owner_recommend = True
                        logger.info(f"[BiliBot] 📢 已在评论区@主人：{rec_msg}")
                else:
                    logger.warning(
                        "[BiliBot] 跳过评论区推荐：未配置 OWNER_BILI_NAME"
                    )
            if delivery in {"qq_private", "bili_private_and_qq", "all"}:
                if (
                    await self.outward_actions._execute_proactive_action(
                        f"proactive_owner_qq:{bvid}",
                        "proactive_owner_recommend",
                        bvid,
                        lambda: self.outward_actions._send_owner_recommend_qq(rec_text, bvid),
                        metadata={"channel": "qq_private"},
                    )
                ).success:
                    actions.append("💬QQ推荐给主人")
                    sent_owner_recommend = True
                    await self.owner_feedback.maybe_ask(bvid, video.get("title", ""), rec_text)
            if sent_owner_recommend:
                watch_round.owner_recommend_count += 1
                self._host._owner_last_recommended_at = datetime.now().strftime("%Y-%m-%d %H:%M")
                self._owner_recommended_bvids.add(bvid)
                watch_round.recommend_ledger[bvid] = {"time": self._owner_last_recommended_at, "title": video.get("title", "")}
                self.states.owner_recommend.write(watch_round.recommend_ledger)
        except ContentProtocolError as e:
            if str(e) == "recommendation_skipped":
                logger.info("[BiliBot] 本次主人推荐选择不发送")
            else:
                logger.warning(f"[BiliBot] 主人推荐内容无效: {e}")
        except Exception as e:
            logger.warning(f"[BiliBot] 生成或发送主人推荐失败: {e}")
