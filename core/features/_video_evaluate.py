"""看完一个视频后问模型：打几分、什么感受、想不想评论。严格 JSON，校验不过就不算。"""
from __future__ import annotations

import time
from datetime import datetime

from astrbot.api import logger

from ..content_protocol import ContentProtocolError, PROACTIVE_COMMENT_SCHEMA_PROMPT, parse_proactive_comment
from ..infra.host_view import HostView
from ..security.redact import wrap_untrusted
from ..video_diagnostics import evaluation_failure, get_call_diagnostic, set_call_diagnostic, exception_detail
from ..voice_prompt import NATURAL_VOICE_RULES
from ..video_evaluation import VIDEO_EVALUATION_SCHEMA_PROMPT, VideoEvaluationError, parse_video_evaluation


class VideoEvaluate(HostView):
    HOST_API = frozenset({"activity_logs", "config", "json_store", "memory_recall", "models", "mood", "settings", "taste", "user_profiles"})

    async def _owner_recommendation_context(self, query_text):
        """只使用已绑定主人的画像和其本人记忆，避免拿全直播间话题猜偏好。"""
        owner_mid = str(self.settings.get("OWNER_MID") or "").strip()
        if not owner_mid:
            return ""
        parts = []
        profile_context = self.user_profiles.context(owner_mid)
        if profile_context:
            parts.append(profile_context)
        recalled = await self.memory_recall._search_memories(
            query_text,
            limit=4,
            memory_types={"chat", "live", "user_summary"},
            user_id=owner_mid,
            score_threshold=0.4,
            # 评价会变成公开评论和动态：只读公开评论和 Bot 自己的经历，主人的私信、QQ 私聊不进来。
            reader_scope="proactive",
        )
        if recalled:
            parts.append("【与当前视频相关的主人记忆】\n" + "\n".join(recalled))
        return "\n".join(parts)[:1000]

    async def _evaluate_video(self, video_info, video_description):
        started = time.monotonic()
        set_call_diagnostic()
        try:
            return await self._evaluate_video_inner(video_info, video_description)
        except Exception as exc:
            reason = exception_detail(exc, self.config)
            evaluation_failure(video_info, "evaluation_exception", reason, self.config,
                               elapsed=time.monotonic() - started)
            logger.warning(f"[BiliBot] 评价准备失败: {reason}")
            return None

    async def _evaluate_video_inner(self, video_info, video_description):
        video_info.pop("_evaluation_diagnostic", None)
        if video_info.get("analysis_basis") in {"metadata", "seen_only"}:
            reason = "只有标题/简介，未取得可靠字幕或画面" if video_info.get("analysis_basis") == "metadata" else "仅剩旧观看痕迹，未重新观看，不生成新评分"
            evaluation_failure(video_info, "content", reason, self.config)
            logger.info(f"[BiliBot] 暂不评分: {reason}")
            return None
        sp = await self.models._get_system_prompt()
        on = self.settings.get("OWNER_NAME") or "主人"
        watch_log = self.activity_logs.watch.recent()
        recent_taste = self.taste._format_recent_taste_summary(watch_log)
        recent_preferences = self.taste._format_recent_preference_summary(watch_log)
        today_mood, today_mood_reason = self.mood.today()
        owner_memory_context = await self._owner_recommendation_context(
            f"{video_info.get('title', '')} {video_info.get('desc', '')} {video_description[:500]}"
        )
        owner_context_block = (
            f"\n已记录的{on}画像和相关记忆（仅作分享交流的可选参考，不用于决定你自己的兴趣；轻量视频引用、收到分享都不代表喜欢）：\n{wrap_untrusted(owner_memory_context, 'owner_evidence')}\n"
            if owner_memory_context else ""
        )
        video_material = wrap_untrusted(
            f"UP主：{video_info.get('up_name', '')}\n标题：{video_info.get('title', '')}\n"
            f"简介：{video_info.get('desc', '')[:100]}\n视频内容：{video_description}",
            "video_material",
        )
        prompt = f"""你刚看完一个B站视频：
{video_material}
{owner_context_block}

你当前的状态：{today_mood}（{today_mood_reason}）
你最近按真实评分形成的分区倾向：
{recent_taste}
你最近的具体兴趣、好奇、厌恶或疲劳信号：
{recent_preferences}

score、review 记录你自己的观后感和兴趣；是否分享取决于你是否真想把这次发现告诉对方。
人设影响表达与感受，但不要只因“我不玩/不吃/不熟悉”就压低评分，也不要只因题材像角色背景就加分。
即使题材陌生，也要评价其中具体的信息、创意、笑点或体验。反复观看同类题材不能自动变成高分依据。

评分说明：
- 1-3：看不下去、内容很差或无聊到想退出
- 4-5：一般般，没什么感觉，打发时间
- 6-7：还行，有点意思，正常水平的视频
- 8-9：很好看，会想点赞收藏的程度
- 10：封神，看完想二刷或者到处安利
不要预设大部分视频应该落在哪个分数，也不要为了拉开差距硬造低分或高分。可用一位小数，按本条证据独立评分：内容是否扎实/有趣、表达是否清楚/有创意、观看体验和自己的真实投入。
评分理由是写给自己的打分备注，一两句话：点出一个最能决定分数的具体亮点或问题，说清它为什么让你打这个分；不写分数本身，不用逐档比较，也不要罗列多个理由；同类视频的制作与内容差异必须反映到评分。人设用来表达感受，不能成为固定加减分表。
review是写给自己的观后感，像看完在备忘录里随手记两三句：一个让你有反应的画面、台词或念头，加上你当时的真实反应。不复读标题，不写成影评或测评，不只写“挺有意思”“一般般”。

        comment要求：像真人随手在评论区打的字，只回应一个具体细节；不要概括视频、客套、夸UP辛苦，也不要以“期待下一期”收尾。

recommend_owner判断：你真的喜欢、惊讶、被逗笑或发现有意思的具体内容，想分享给{on}时填true，不必先证明符合对方的爱好；不要为了完成次数而每条都分享。
- recommend_reason写视频中的具体看点和你想分享的感受，不把“题材符合人设”当作唯一理由；不分享时填false且理由留空。
- 你的兴趣和{on}的兴趣是两回事。可以说“我觉得这里有意思”，没有明确反馈就不能说“你喜欢这个”“你肯定爱看”。你看过、喜欢或分享过的视频，都不能证明对方喜欢。
- 对方明确表达的兴趣可以作为交流参考，没有这类记忆也可以自发分享；不要编造对方的偏好、请求或生活习惯，也不要把一次需求变成长期爱好。
偏好可以变化：近期厌倦、负面体验或新题材的吸引力都能推翻旧倾向；未知题材保持好奇，单次喜欢不等于稳定爱好。

interactions和want_follow：看完真想做什么就选什么，分数只是参考，不是按分数线自动执行。
- 分数一般也可以去评论（有个具体的槽点、想问的问题），分数高也可以只是默默看完、不评论不关注
- 点赞是顺手的认可；投币、收藏留给真心喜欢、以后还想再看的；关注是想以后继续看这个UP
- 不要每个视频都把能选的全选上，也不要为了显得活跃硬选

want_dynamic判断：看完是否真有话想在自己的B站动态里说。和评分高低无关，烂得好笑的视频也可以；没有具体想说的就填false，大多数视频都是false。
- dynamic_angle写想说的具体角度（例如“结尾反转其实早有伏笔”），不想发时留空。
- dynamic_kind是想发成哪种：吐槽（有具体槽点）、段子（由它想到的玩笑或脑洞）、分享（安利、点评或由它引出的联想）、搬运（视频本身就值得让关注你的人直接看到，转发它并配一句话）。想发的不一定马上发，会先攒着。"""
        custom_proactive_inst = self.settings.get("CUSTOM_PROACTIVE_INSTRUCTION")
        if custom_proactive_inst:
            prompt += f"\n\n【补充提示词】{custom_proactive_inst}"
        prompt += "\n\n" + NATURAL_VOICE_RULES + "\n\n" + VIDEO_EVALUATION_SCHEMA_PROMPT
        text = None
        started = time.monotonic()
        set_call_diagnostic()
        try:
            text = await getattr(self.models, "_generation_llm_call", self.models._llm_call)(prompt, system_prompt=sp, max_tokens=1200)
            if not text:
                reason = get_call_diagnostic().get("reason") or "模型返回空内容，未取得有效评价"
                evaluation_failure(video_info, "model_request", reason, self.config, elapsed=time.monotonic() - started)
                return None
            try:
                evaluation = parse_video_evaluation(text)
                if not evaluation.get("partition"):
                    evaluation["partition"] = str(video_info.get("tname") or "")[:30]
                return evaluation
            except VideoEvaluationError as exc:
                evaluation_failure(video_info, "evaluation_schema", str(exc), self.config,
                                   elapsed=time.monotonic() - started, raw=text)
                logger.warning(f"[BiliBot] 视频评价结构校验失败 bvid={video_info.get('bvid', '')}: {exc}（回复 {len(text)} 字）")
                return None
        except Exception as e:
            reason = exception_detail(e, self.config)
            evaluation_failure(video_info, "evaluation_exception", reason, self.config, elapsed=time.monotonic() - started)
            logger.error(f"[BiliBot] 视频评价失败: {reason}")
            return None

    async def _generate_proactive_comment(self, video_info, video_description):
        sp = await self.models._get_system_prompt()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt = f"""当前时间：{now}

你刚看完一个B站视频，现在想在评论区留一条评论。

视频信息：
- UP主：{video_info.get('up_name', '')}
- 标题：{video_info.get('title', '')}
- 视频内容：{video_description}

写评论的要点：
- 先选一个最想回应的具体细节，只写一个中心，不概括整部视频
- 像真正的B站用户看完顺手留一句：可以接梗、吐槽、追问或说瞬间感受，不写影评
- 不照抄标题/简介，不假装亲历视频里没有提供的信息，不编造UP主背景
- 禁止万能夸奖和任务腔：不要写“UP主辛苦了”“视频很好”“学到了”“感谢分享”“期待下一期”
- 不用“作为……”“整体来说”“不得不说”“让人不禁”这类书面转折，也不要说明自己正在评论
- 不要为了像B站而硬塞“哈哈哈”“绷不住了”“泪目”或网络梗；内容确实支持时才用
- 内容一般时可以写一个真实的小观察，也可以保持克制，不硬夸
- 允许很短；没有真正想说的细节就保持沉默，不要为了完成任务硬评
- 12-38字，通常一句，不堆感叹号

{NATURAL_VOICE_RULES}
{PROACTIVE_COMMENT_SCHEMA_PROMPT}"""
        custom_proactive_inst = self.settings.get("CUSTOM_PROACTIVE_INSTRUCTION")
        if custom_proactive_inst:
            prompt += f"\n\n【补充提示词】{custom_proactive_inst}"
        result = await getattr(self.models, "_generation_llm_call", self.models._llm_call)(prompt, system_prompt=sp, max_tokens=120)
        if not result:
            return None
        try:
            parsed = parse_proactive_comment(result)
        except ContentProtocolError as exc:
            logger.warning(f"[BiliBot] 主动评论结构校验失败，保持沉默: {exc}")
            return None
        return parsed["text"] if parsed["decision"] == "comment" else None
