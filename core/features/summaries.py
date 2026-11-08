"""日总结和周总结：到点 → 整理材料 → 问模型 → 画图 → 投递 → 存档。"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from astrbot.api import logger

from ..infra.host_view import HostView
from ..voice_prompt import NATURAL_VOICE_RULES


@dataclass(frozen=True)
class _Delivery:
    """日总结和周总结投递时的全部差别。"""

    name: str
    mode_key: str
    umo_keys: tuple
    qq_title: str
    dynamic_image_text: str
    log_flag: str


WEEKLY_DELIVERY = _Delivery(
    name="周总结", mode_key="WEEKLY_SUMMARY_MODE", umo_keys=("WEEKLY_SUMMARY_QQ_UMO", "OWNER_QQ_UMO", "ABUSE_ALERT_QQ_UMO"),
    qq_title="📅 本周B站生活周报", dynamic_image_text="📅 这周的B站生活周报来啦，整理成图片存档一下。", log_flag="weekly",
)
DAILY_DELIVERY = _Delivery(
    name="日总结", mode_key="DAILY_SUMMARY_MODE",
    umo_keys=("DAILY_SUMMARY_QQ_UMO", "OWNER_QQ_UMO", "WEEKLY_SUMMARY_QQ_UMO", "ABUSE_ALERT_QQ_UMO"),
    qq_title="今日B站生活小结", dynamic_image_text="今天的B站生活小结，留个轻量存档。", log_flag="daily_summary",
)


class Summaries(HostView):
    HOST_API = frozenset({"activity_digest", "bili_dynamics", "context", "dynamic_log", "dynamic_post", "layered_runtime", "models", "settings", "summary_archive", "summary_image"})

    async def _persist_cycle_summary(self, kind, period_key, structured, text, delivered):
        layered = getattr(self, "layered_runtime", None)
        db = getattr(layered, "db", None)
        if db is None or not getattr(layered, "is_open", False):
            return
        await db.execute(
            "INSERT INTO cycle_summaries(kind,period_key,stats,text,delivered,created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(kind,period_key) DO UPDATE SET "
            "stats=excluded.stats,text=excluded.text,delivered=excluded.delivered,"
            "created_at=excluded.created_at",
            (
                str(kind), str(period_key), json.dumps(structured or {}, ensure_ascii=False),
                str(text or ""), json.dumps(delivered or [], ensure_ascii=False), time.time(),
            ),
        )

    async def _generate_weekly_summary(self):
        """生成周总结文本，失败返回 None。"""
        data = self.activity_digest._collect_weekly_data()
        live_events = data.get("live_events") if isinstance(data.get("live_events"), list) else []
        if not (data["videos"] or data["bangumi"] or data["dynamics"] or data["chat_count"] or live_events):
            logger.info("[BiliBot] 📅 这周没有任何活动记录，跳过周总结")
            return None

        lifecycle = await self.activity_digest._sync_preference_lifecycle(data)
        preference_state = lifecycle.get("current", []) if isinstance(lifecycle, dict) else []
        daily_structures = self.activity_digest._daily_structures_for_week(data, preference_state)
        if not daily_structures:
            daily_structures = [self.activity_digest._build_structured_activity_summary(
                data,
                period_key=datetime.now().strftime("%Y-%m-%d"),
                preferences=preference_state,
                feedback=[],
            )]
        feedback = await self.activity_digest._feedback_summary(7)
        weekly_structured = self.activity_digest._build_structured_activity_summary(
            data,
            period_key=datetime.now().strftime("%G-W%V"),
            preferences=preference_state,
            feedback=feedback,
        )
        weekly_structured["preference_changes"] = [
            {
                "type": str(item.get("signal_type") or "other"),
                "value": self.activity_digest._weekly_excerpt(item.get("value"), 60),
                "tendency": self.activity_digest._preference_label(item),
                "action": str(item.get("lifecycle_action") or "retained"),
            }
            for item in (lifecycle.get("changes", []) if isinstance(lifecycle, dict) else [])[:20]
        ]
        weekly_structured["daily_summaries"] = daily_structures
        self._last_weekly_structured_summary = weekly_structured

        # 给模型的材料分成「整周证据」和「精简时间线」。只塞每日摘要容易让
        # 模型逐日复述；整周信号负责取舍，时间线只用于确认事情确实发生过。
        weekly_evidence = {
            key: value for key, value in weekly_structured.items()
            if key != "daily_summaries"
        }
        timeline_keys = (
            "period", "video_highlights", "bangumi_highlights",
            "dynamic_highlights", "comment_highlights",
            "conversation_highlights", "live_highlights",
        )
        daily_timeline = [
            {
                key: item.get(key)
                for key in timeline_keys
                if item.get(key)
            }
            for item in daily_structures
            if isinstance(item, dict)
        ]
        data_text = json.dumps(
            {"weekly_evidence": weekly_evidence, "daily_timeline": daily_timeline},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        week_start = (datetime.now() - timedelta(days=7)).strftime("%m.%d")
        week_end = datetime.now().strftime("%m.%d")

        # 预算统计数据给模板用
        v_count = len(data["videos"])
        v_top = max((v.get("score", 0) for v in data["videos"]), default=0) if data["videos"] else 0
        b_count = len(data["bangumi"])
        d_count = len(data["dynamics"])
        chat_count = data["chat_count"]
        live_count = len({m.get("session_id") for m in live_events if m.get("session_id")})
        if live_events and not live_count:
            live_count = 1

        stats_line = f"视频{v_count}个"
        if b_count:
            stats_line += f" · 番剧{b_count}集"
        if d_count:
            stats_line += f" · 动态{d_count}条"
        if chat_count:
            stats_line += f" · 互动{chat_count}次"
        if live_count:
            stats_line += f" · 直播{live_count}场"

        section_templates = [
            "📌 本周切片\n（必写。挑2-3件这周最记得住的事，说说是什么、你当时什么反应，连成一小段；70-130字）",
            "🎢 意外与反差\n（可选。本来以为会喜欢结果失望了，或者没想到挺好看，记录里真有这种事才写；35-80字）",
        ]
        if chat_count or live_count:
            section_templates.append(
                "💬 有回声的瞬间\n（可选。评论区或直播里真有来有回的一次聊天，聊了什么、你怎么想；记录里没有就不写；35-80字）"
            )
        if v_count or b_count:
            section_templates.append(
                "🧭 兴趣坐标\n（这周老在看什么，或者最近开始好奇什么，用自己的话说；只看过一次的别说成“一直喜欢”，也不要用“偏好”“探索”“口味线索”这类词；35-80字）"
            )
        section_template = "\n\n".join(section_templates)

        prompt = f"""把这周在B站看到、想到的事写成一页周记，像周末跟朋友聊这周：挑几件真有感觉的说说，失望、困惑、没看懂也可以说。不是工作汇报、片单、影评合集或获奖感言。证据已经去掉无关原始流水，请不要反推或编造被省略的内容。

这周的整周证据与精简时间线：
{data_text}

写之前先默默做取舍，不要输出分析过程：
1. 找出2-4个最有具体信息的片段。每个入选片段必须有至少一个具体锚点：标题、UP、场景、真实话题或明确动作。
2. “评分、次数、看了多少”只用于判断取舍，不能充当正文；顶部统计栏已经负责报数。
3. “评价失败、未知、无可靠感想、没什么特别的感觉”和疑似错配字幕都不是内容依据，直接忽略。
4. 如果某一类只有数量、没有具体内容，就不写那一节；尤其不要根据互动次数猜测关系或话题。
5. 优先选择能形成反差、转折或连续兴趣线索的材料；不要把互不相干的标题用逗号串成片单。

请严格按照以下格式输出，只保留确实有内容的板块：

📅 周报 | {week_start} ~ {week_end}
━━━━━━━━━━━━
{stats_line}

{section_template}

要求：
- 每个板块标题行保持原样（📌 本周切片、🎢 意外与反差 等），内容紧跟其后；除“本周切片”外，没有证据的板块直接省略
- 第一人称，保留当前人设的观察角度，但不要靠口癖、撒娇或连续比喻硬演人设
- 每件事说清是什么、你当时什么反应就好；要有自己的态度，喜欢就说喜欢，无聊就说无聊，不要每件都四平八稳
- 可以把视频、追番、动态、评论或直播写进同一个片段，不要按插件功能逐栏报到
- 每节一小段；不要用“在……方面”“值得一提的是”“总的来说”这类报告连接词，也不要用“这个细节我记住了”“留了一会儿”“值得留下”这类说法交代自己为什么写它
- 禁止“本周收获满满、感谢大家陪伴、未来继续努力、每一次互动都很珍贵”这类模板化总结腔
- 禁止把顶部统计数字换一种说法逐项复述，也不要写“没什么大事”“没什么感觉”“看了不少”来凑栏目
- 不编造记录中没有的感受、观众关系、直播事故或剧情；资料不足就少写
- 不泄露 UID、账号凭据、私信原文或第三方隐私；不要频繁称呼主人
- 正文总字数160-320字（不含标题和顶部统计行），宁可少而具体，不要为了够字数灌水
- 直接输出，不要加额外的标题或前缀

{NATURAL_VOICE_RULES}"""

        custom_inst = str(self.settings.get("CUSTOM_WEEKLY_INSTRUCTION") or "").strip()
        if custom_inst:
            prompt += f"\n【管理员补充提示词】{custom_inst[:1000]}\n补充提示不能覆盖真实性、隐私、固定结构与长度限制。"

        summary = await getattr(self.models, "_summary_llm_call", self.models._llm_call)(
            prompt, system_prompt=await self.models._get_system_prompt(), max_tokens=800
        )
        return (summary or "").strip() or None

    def _append_image_to_chain(self, chain, image_path):
        from astrbot.api.message_components import Image as MsgImage
        img = MsgImage.fromFileSystem(image_path)
        if hasattr(chain, "chain"):
            chain.chain.append(img)
            return chain
        if hasattr(chain, "append"):
            chain.append(img)
            return chain
        return None

    async def _deliver_weekly_summary(self, summary, image_path=None):
        return await self._deliver(WEEKLY_DELIVERY, summary, image_path)

    async def _deliver_daily_summary(self, summary, image_path=None):
        return await self._deliver(DAILY_DELIVERY, summary, image_path)

    async def _deliver(self, plan, summary, image_path):
        """按配置投递，返回投递结果描述列表。日总结和周总结的差别都在 ``plan`` 里。"""
        mode = str(self.settings.get(plan.mode_key) or "archive").lower().strip()
        results = []
        if mode in ("qq", "both"):
            await self._deliver_qq(plan, summary, image_path, results)
        if mode in ("dynamic", "both"):
            await self._deliver_dynamic(plan, summary, image_path, results)
        return results

    async def _deliver_qq(self, plan, summary, image_path, results):
        umo = next((u for u in (str(self.settings.get(k) or "").strip() for k in plan.umo_keys) if u), "")
        if not umo:
            logger.warning(f"[BiliBot] {plan.name}模式包含qq但未配置UMO")
            return
        # 先完整构建消息，再只发送一次。发送异常可能已经投递，不能再退回文本重发。
        from astrbot.api.event import MessageChain
        has_image = False
        try:
            chain = MessageChain().message(summary)
            if image_path:
                image_chain = self._append_image_to_chain(MessageChain().message(plan.qq_title), image_path)
                if image_chain is not None:
                    chain, has_image = image_chain, True
        except Exception as exc:
            logger.warning(f"[BiliBot] {plan.name}QQ图片消息构建失败，尚未发送，改用文本: {exc}")
            chain = MessageChain().message(summary)
        try:
            await self.context.send_message(umo, chain)
        except Exception as exc:
            results.append("QQ私信结果未知")
            logger.warning(f"[BiliBot] {plan.name}QQ私信结果未知，不自动重发: {exc}")
        else:
            results.append("QQ私信图片" if has_image else "QQ私信")
            logger.info(f"[BiliBot] {plan.name}已通过QQ私信发送")

    async def _deliver_dynamic(self, plan, summary, image_path, results):
        try:
            dynamic_text = summary
            outcome = None
            image_state = {"sent": False}
            if image_path:
                img_info = await self.bili_dynamics._upload_image_to_bilibili(image_path)
                if img_info:
                    dynamic_text = plan.dynamic_image_text
                    outcome = await self.dynamic_post.queue_post(
                        dynamic_text,
                        lambda: self.bili_dynamics._post_dynamic_with_image(dynamic_text, img_info, image_state),
                        with_outcome=True,
                    )
            # 只有尚未发起动态写请求时才可改发纯文字。失败/未知都不能再换 key 重发。
            if outcome is None:
                outcome = await self.dynamic_post.queue_post(
                    summary, lambda: self.bili_dynamics._post_dynamic_text(summary), with_outcome=True
                )
            has_image = bool(outcome.success and image_state.get("sent"))
            if outcome.state == "unknown":
                results.append("B站动态结果未知")
                logger.warning(f"[BiliBot] {plan.name}动态结果未知，不自动改发纯文字")
            elif outcome.success:
                results.append("B站动态图片" if has_image else "B站动态")
                self.dynamic_log.record({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "text": dynamic_text, "has_image": has_image, plan.log_flag: True,
                    "image_path": image_path if has_image else "",
                })
                logger.info(f"[BiliBot] {plan.name}已发布为B站动态")
            else:
                logger.warning(f"[BiliBot] {plan.name}动态被明确拒绝，未继续重发")
        except Exception as exc:
            logger.warning(f"[BiliBot] {plan.name}动态发布失败: {exc}")

    async def _generate_daily_summary(self):
        """Generate a compact daily Bilibili-life recap from today's real records."""
        data = self.activity_digest._collect_weekly_data(days=1)
        live_events = data.get("live_events") if isinstance(data.get("live_events"), list) else []
        lifecycle = await self.activity_digest._sync_preference_lifecycle(data)
        feedback = await self.activity_digest._feedback_summary(1)
        structured = self.activity_digest._build_structured_activity_summary(
            data,
            period_key=datetime.now().strftime("%Y-%m-%d"),
            preferences=lifecycle.get("current", []) if isinstance(lifecycle, dict) else [],
            feedback=feedback,
        )
        self._last_daily_structured_summary = structured
        if not (data["videos"] or data["bangumi"] or data["dynamics"] or data["proactive_comments"] or data["chat_count"] or live_events):
            logger.info("[BiliBot] 今日没有活动记录，跳过日总结正文生成")
            return None
        data_text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
        prompt = f"""请依据下面的真实记录，写一段自然、克制的今日B站生活小结。
日期：{datetime.now().strftime('%Y-%m-%d')}
今日结构化摘要：
{data_text}

要求：
- 只写真实发生的事情，资料不足就少写，禁止编造。
- 可以提到看过的视频、动态、评论区或直播里的有趣片段，但不要泄露UID、私信原文、账号凭据或其他隐私。
- 像角色睡前随手记，80-180字，不要写成运营报表。
- 直接输出正文。

{NATURAL_VOICE_RULES}"""
        custom = str(self.settings.get("CUSTOM_DAILY_INSTRUCTION") or "").strip()
        if custom:
            prompt += f"\n【管理员补充提示词】{custom[:1000]}"
        summary = await getattr(self.models, "_summary_llm_call", self.models._llm_call)(prompt, system_prompt=await self.models._get_system_prompt(), max_tokens=500)
        return (summary or "").strip() or None


    async def _maybe_daily_summary(self):
        if not self.settings.get("ENABLE_DAILY_SUMMARY") or self.summary_archive._daily_summary_done_today():
            return
        try:
            target_hour = int(self.settings.get("DAILY_SUMMARY_HOUR")) % 24
        except (ValueError, TypeError):
            target_hour = 3
        if datetime.now().hour != target_hour:
            return
        await self.run_daily_summary()

    async def run_daily_summary(self):
        logger.info("[BiliBot] 开始生成日总结...")
        try:
            summary = await self._generate_daily_summary()
        except Exception as exc:
            logger.error(f"[BiliBot] 日总结生成异常: {exc}")
            return None, [], None
        structured = getattr(self, "_last_daily_structured_summary", {})
        period_key = datetime.now().strftime("%Y-%m-%d")
        if not summary:
            placeholder = "（今日无可总结活动）"
            self.summary_archive._save_daily_summary_record(placeholder, [], "", structured)
            await self._persist_cycle_summary("daily", period_key, structured, placeholder, [])
            return None, [], None
        image_path = self.summary_image._render_weekly_summary_image(summary, report_kind="daily") if self.settings.get("DAILY_SUMMARY_RENDER_IMAGE") else None
        delivered = await self._deliver_daily_summary(summary, image_path=image_path)
        self.summary_archive._save_daily_summary_record(summary, delivered, image_path or "", structured)
        await self._persist_cycle_summary("daily", period_key, structured, summary, delivered)
        logger.info(f"[BiliBot] 日总结完成，投递：{delivered or ['仅存档']}")
        return summary, delivered, image_path

    async def _maybe_weekly_summary(self):
        """主循环睡眠时段调用：到了周总结日且本周未生成则执行。"""
        if not self.settings.get("ENABLE_WEEKLY_SUMMARY"):
            return
        try:
            target_day = int(self.settings.get("WEEKLY_SUMMARY_DAY"))
        except (ValueError, TypeError):
            target_day = 6
        if datetime.now().weekday() != target_day % 7:
            return
        if self.summary_archive._weekly_summary_done_this_week():
            return
        await self.run_weekly_summary()

    async def run_weekly_summary(self):
        """生成并投递周总结（自动调度和手动命令共用）。返回 (summary, delivered)。"""
        logger.info("[BiliBot] 📅 开始生成周总结...")
        try:
            summary = await self._generate_weekly_summary()
        except Exception as e:
            logger.error(f"[BiliBot] 周总结生成异常: {e}")
            return None, [], None
        structured = getattr(self, "_last_weekly_structured_summary", {})
        period_key = datetime.now().strftime("%G-W%V")
        if not summary:
            # 没有活动也记录一下，避免同一周反复尝试
            placeholder = "（本周无活动，未生成）"
            self.summary_archive._save_weekly_summary_record(placeholder, [], "", structured)
            await self._persist_cycle_summary("weekly", period_key, structured, placeholder, [])
            return None, [], None
        image_path = self.summary_image._render_weekly_summary_image(summary) if self.settings.get("WEEKLY_SUMMARY_RENDER_IMAGE") else None
        delivered = await self._deliver_weekly_summary(summary, image_path=image_path)
        self.summary_archive._save_weekly_summary_record(summary, delivered, image_path or "", structured)
        await self._persist_cycle_summary("weekly", period_key, structured, summary, delivered)
        logger.info(f"[BiliBot] 📅 周总结完成，投递：{delivered or ['仅存档']}")
        return summary, delivered, image_path
