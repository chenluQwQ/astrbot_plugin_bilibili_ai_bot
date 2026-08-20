"""视频分析：内容概括、媒体处理、视频/动态上下文构建。"""
import os
import base64
import json
import re
import shutil
from datetime import datetime
from astrbot.api import logger
from .config import VIDEO_MEMORY_FILE, TEMP_VIDEO_DIR


class VideoMixin:
    """视频分析、下载、截帧、上下文。"""

    @staticmethod
    def _clip_media_text(value, limit):
        """限制媒体派生文本长度；原始图片、帧和 base64 从不进入长期对话历史。"""
        text = str(value or "").strip()
        try:
            max_chars = max(1, int(limit))
        except (TypeError, ValueError):
            max_chars = 1
        return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"

    @staticmethod
    def _analysis_has_subtitle_mismatch(analysis):
        """识别旧缓存中已经暴露字幕错配的分析，命中后应重新分析。"""
        text = str(analysis or "")[:240]
        markers = (
            "字幕与本视频内容不符",
            "字幕与本视频不符",
            "字幕内容与本视频不符",
            "字幕内容不符，需要先说明",
            "字幕似乎与视频内容无关",
            "字幕抓取时串了片源",
        )
        return any(marker in text for marker in markers)

    async def _watch_video_and_save_memory(
        self, video_id, memory_source="tool_watch", *, force_rewatch=False
    ):
        """完整分析一个 BV/av 视频并写入统一视频记忆，供工具和私信共用。"""
        raw_id = str(video_id or "").strip()
        if not raw_id:
            return {"ok": False, "message": "没有找到要观看的视频编号"}
        try:
            if raw_id.lower().startswith("av") and raw_id[2:].isdigit():
                oid = int(raw_id[2:])
            elif raw_id.isdigit():
                oid = int(raw_id)
            else:
                oid = await self._get_video_oid(raw_id)
            if not oid:
                return {"ok": False, "message": f"找不到视频 {raw_id}"}

            vi = await self._get_video_info(oid)
            if not vi:
                return {"ok": False, "message": f"获取视频信息失败 {raw_id}"}
            actual_bvid = str(vi.get("bvid", "") or raw_id).strip()
            analysis_info = {
                "bvid": actual_bvid,
                "title": vi.get("title", ""),
                "desc": vi.get("desc", ""),
                "up_name": vi.get("owner_name", ""),
                "up_mid": vi.get("owner_mid", ""),
                "tname": vi.get("tname", ""),
                "duration": vi.get("duration", 0),
                "pic": vi.get("pic", ""),
                "cid": vi.get("cid", 0),
                "oid": oid,
            }

            video_cache = self._load_json(VIDEO_MEMORY_FILE, {})
            cached = video_cache.get(actual_bvid, {})
            cached_text = str(cached.get("analysis") or cached.get("summary") or "").strip()
            removed_bad_cache = False
            if self._analysis_has_subtitle_mismatch(cached_text):
                logger.warning(
                    f"[BiliBot] 丢弃疑似字幕错配的旧视频缓存，重新分析: {actual_bvid}"
                )
                video_cache.pop(actual_bvid, None)
                cached = {}
                removed_bad_cache = True
            if self._compact_video_cache(video_cache) or removed_bad_cache:
                self._save_json(VIDEO_MEMORY_FILE, video_cache)
            cached = video_cache.get(actual_bvid, {})
            cached_analysis = str(cached.get("analysis", "") or "").strip()
            cached_summary = str(cached.get("summary", "") or "").strip()
            if (cached_analysis or cached_summary) and not force_rewatch:
                video_description = self._clip_media_text(cached_analysis or cached_summary, 1600)
                score = cached.get("score", 5)
                mood = cached.get("mood", "平静")
                review = self._clip_media_text(cached.get("review", "") or "完整分析已清理，仅保留简短摘要", 320)
                from_cache = True
                await self._mark_video_seen(
                    actual_bvid, analysis_info, source=memory_source, increment=False
                )
                logger.info(f"[BiliBot] 📹 私信看片命中视频短期缓存/摘要索引：《{vi.get('title', '')}》")
            else:
                seen_trace = await self._seen_video_record(actual_bvid)
                if seen_trace and not force_rewatch:
                    link = f"https://www.bilibili.com/video/{actual_bvid}"
                    title = vi.get("title", "") or seen_trace.get("title", "")
                    owner_name = vi.get("owner_name", "") or seen_trace.get("owner_name", "")
                    message = (
                        "[曾经看过这个视频]\n"
                        f"标题：{title}\nUP主：{owner_name}\n链接：{link}\n"
                        "详细内容已经淡忘了。为了避免把旧视频重新当成新视频分析，"
                        "只有主人明确要求“重新看一次”时才会重看。"
                    )
                    share_info = dict(vi)
                    share_info.update({"bvid": actual_bvid, "aid": oid, "oid": oid})
                    return {
                        "ok": True,
                        "message": message,
                        "video_info": share_info,
                        "bvid": actual_bvid,
                        "from_cache": True,
                        "seen_only": True,
                    }
                logger.info(f"[BiliBot] 🎬 开始观看私信分享视频：《{vi.get('title', '')}》")
                video_description = self._clip_media_text(
                    await self._analyze_video_with_vision(analysis_info), 1600
                )
                evaluation = await self._evaluate_video(
                    analysis_info, video_description
                )
                score = (evaluation or {}).get("score", 5)
                score_reason = str((evaluation or {}).get("score_reason", ""))
                mood = (evaluation or {}).get("mood", "平静")
                review = self._clip_media_text((evaluation or {}).get("review", ""), 320)
                preference_signals = list((evaluation or {}).get("preference_signals", []) or [])
                search_keywords = list((evaluation or {}).get("search_keywords", []) or [])
                from_cache = False

                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                video_cache[actual_bvid] = {
                    "bvid": actual_bvid,
                    "title": vi.get("title", ""),
                    "desc": str(vi.get("desc", "") or "")[:200],
                    "owner_name": vi.get("owner_name", ""),
                    "owner_mid": str(vi.get("owner_mid", "")),
                    "tname": vi.get("tname", ""),
                    "analysis": video_description,
                    "summary": self._clip_media_text(video_description, 220),
                    "score": score,
                    "score_reason": score_reason,
                    "mood": mood,
                    "review": review,
                    "preference_signals": preference_signals,
                    "search_keywords": search_keywords,
                    "time": now_str,
                    "source": memory_source,
                }
                self._save_json(VIDEO_MEMORY_FILE, video_cache)
                await self._mark_video_seen(
                    actual_bvid, analysis_info, source=memory_source
                )

                if self.config.get("ENABLE_VIDEO_LONG_TERM_MEMORY", True):
                    memory_text = (
                        f"[{now_str}] 视频摘要《{vi.get('title', '')}》"
                        f"(UP主:{vi.get('owner_name', '')}) 评分:{score}/10 "
                        f"理由:{score_reason[:80]} 感想:{review[:80]} 内容:{video_description[:220]}"
                    )
                    if preference_signals:
                        memory_text += " 兴趣信号:" + "、".join(
                            f"{item.get('polarity')}:{item.get('value')}"
                            for item in preference_signals[:5]
                        )
                    await self._save_self_memory_record(
                        f"{memory_source}:{actual_bvid}", self._clip_media_text(memory_text, 520), memory_type="video",
                        extra={"bvid": actual_bvid, "owner_mid": str(vi.get("owner_mid", "")), "owner_name": vi.get("owner_name", ""), "video_title": vi.get("title", ""), "tname": (evaluation or {}).get("partition") or vi.get("tname", ""), "score": score, "score_reason": score_reason, "mood": mood, "review": review, "preference_signals": preference_signals, "search_keywords": search_keywords},
                    )
                logger.info(f"[BiliBot] ✅ 私信分享视频已看完并写入短期缓存：{actual_bvid}")

            link = f"https://www.bilibili.com/video/{actual_bvid}"
            message = (
                f"[{'已从记忆读取视频' if from_cache else '已看完视频'}]\n"
                f"标题：{vi.get('title', '')}\n"
                f"UP主：{vi.get('owner_name', '')}(UID:{vi.get('owner_mid', '')}) | 分区：{vi.get('tname', '')}\n"
                f"链接：{link}\n"
                f"视频简介：{str(vi.get('desc', '') or '')[:150]}\n"
                f"内容详情：{video_description[:800]}\n"
                f"我的感受：{review[:200]}\n"
                f"个人评分：{score}/10 | 看完心情：{mood}\n"
                f"oid={oid}"
            )
            share_info = dict(vi)
            share_info.update({"bvid": actual_bvid, "aid": oid, "oid": oid})
            return {
                "ok": True,
                "message": message,
                "video_info": share_info,
                "bvid": actual_bvid,
                "from_cache": from_cache,
            }
        except Exception as exc:
            logger.error(f"[BiliBot] 看视频异常: {exc}")
            return {"ok": False, "message": f"看视频时出错了: {exc}"}

    # ── 补充上下文（标签+热评+联网搜索） ──
    async def _enrich_video_context(self, video_info):
        bvid = video_info.get("bvid", "")
        oid = video_info.get("oid") or (await self._get_video_oid(bvid) if bvid else None)
        tags = await self._get_video_tags(bvid) if bvid else []
        comments = await self._get_hot_comments(oid) if oid else []
        extra = ""
        if tags:
            extra += f"\n标签：{'、'.join(tags[:10])}"
        if comments:
            extra += "\n热门评论：\n" + "\n".join([f"- {c}" for c in comments[:5]])
        if self.config.get("ENABLE_WEB_SEARCH", False):
            search_query = await self._should_search_for_video(video_info, extra)
            if search_query:
                search_result = await self._web_search(search_query)
                if search_result:
                    extra += f"\n\n【联网搜索补充】\n{search_result[:800]}"
                    logger.info(f"[BiliBot] 🔍 视频搜索补充完成: {search_query[:40]} -> {len(search_result)}字")
        return extra

    # ── 联合上下文（字幕+热评统一构建，所有分析路径共用） ──
    async def _validate_video_subtitle(self, video_info, subtitle_text, extra=""):
        """只在字幕明显属于别的视频时拒绝；不确定时保留，避免误伤正常字幕。"""
        if not subtitle_text:
            return ""
        prompt = f"""你是视频字幕归属校验器。判断下面的候选字幕是否明显不属于这个视频。

判断原则：
- 只有主题、人物或事件明显完全冲突时判 false。
- 音乐、混剪、玩梗、反差内容或信息不足时判 true，宁可保留。
- 不要概括视频，只输出 JSON：{{"relevant": true或false, "reason": "15字以内原因"}}

视频标题：{video_info.get('title', '')}
视频简介：{str(video_info.get('desc', '') or '')[:350]}
视频分区：{video_info.get('tname', '')}
补充信息：{str(extra or '')[:500]}
候选字幕：{subtitle_text[:1000]}"""
        try:
            result = await self._llm_call(prompt, max_tokens=100)
            if not result:
                return subtitle_text
            cleaned = self._repair_llm_json(result)
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            decision = json.loads(match.group() if match else cleaned)
            if decision.get("relevant") is False:
                logger.warning(
                    f"[BiliBot] 已忽略疑似错配字幕: bvid={video_info.get('bvid', '')} "
                    f"reason={str(decision.get('reason', '主题明显不符'))[:40]}"
                )
                return ""
        except Exception as exc:
            logger.debug(f"[BiliBot] 字幕归属校验失败，交由最终分析兜底: {exc}")
        return subtitle_text

    async def _build_joint_context(self, video_info):
        """构建字幕+热评联合上下文。

        返回 dict：
          extra      - 标签/热评/联网搜索补充（_enrich_video_context 的结果）
          subtitle   - 格式化后的字幕段（含前缀，可直接拼入 prompt）
          has_signal - 是否拿到了字幕或热评（决定是否值得做联合整合）
          joint_hint - 联合分析指令（指导 LLM 把内容与观众反响结合）
        """
        extra = await self._enrich_video_context(video_info)
        subtitle_text = await self._get_video_subtitles(
            video_info.get("bvid", ""), video_info.get("cid", 0)
        )
        subtitle_text = await self._validate_video_subtitle(
            video_info, subtitle_text, extra
        )
        subtitle_section = ""
        if subtitle_text:
            subtitle_section = f"\n候选字幕内容：{subtitle_text[:1500]}"
        has_comments = "热门评论" in extra
        joint_hint = ""
        if subtitle_text or has_comments:
            hint_parts = []
            if subtitle_text:
                hint_parts.append(
                    "候选字幕可能由B站自动生成；仅在它与标题、简介、画面和标签一致时采用，明显冲突时静默忽略"
                )
            if has_comments:
                hint_parts.append("热门评论反映观众的真实反应，结尾用1-2句描述评论区的氛围、观众在玩什么梗或在讨论什么")
            joint_hint = "\n联合分析要求：" + "；".join(hint_parts) + "。"
        return {
            "extra": extra,
            "subtitle": subtitle_section,
            "has_signal": bool(subtitle_text or has_comments),
            "joint_hint": joint_hint,
        }

    async def _merge_visual_and_joint(self, video_info, visual_summary, joint):
        """视觉分析结果与字幕/热评做最终联合整合。"""
        if not joint.get("has_signal"):
            return None
        visual_summary = self._clip_media_text(visual_summary, 1200)
        subtitle = self._clip_media_text(joint.get("subtitle", ""), 1800)
        extra = self._clip_media_text(joint.get("extra", ""), 1200)
        prompt = f"""以下是关于B站视频《{video_info.get('title', '未知')}》（UP主：{video_info.get('owner_name', video_info.get('up_name', '未知'))}）的多维信息，请整合为一段完整的内容概括（500字以内）：

【画面分析】（来自视觉模型，描述视频画面内容）
{visual_summary}
{subtitle}
【补充信息】{extra or '无'}
{joint.get('joint_hint', '')}
整合要点：画面分析与可靠字幕互相印证；信息冲突时优先相信实际画面、标题、简介和标签，忽略明显错配的候选字幕。不要在结果中说明字幕抓取、数据冲突或内部判断过程。直接输出概括内容，不要加前缀。"""
        result = await self._llm_call(prompt, max_tokens=600)
        if result:
            logger.info("[BiliBot] 🔗 视觉+字幕+热评联合整合完成")
        return self._clip_media_text(result, 1600) if result else None

    def _video_memory_windows_seconds(self):
        try:
            detail_days = max(
                1,
                min(60, int(self.config.get("VIDEO_MEMORY_DETAIL_DAYS", 15) or 15)),
            )
        except (TypeError, ValueError):
            detail_days = 15
        try:
            fade_days = max(
                30,
                min(730, int(self.config.get("VIDEO_MEMORY_FADE_DAYS", 90) or 90)),
            )
        except (TypeError, ValueError):
            fade_days = 90
        fade_days = max(detail_days, fade_days)
        return detail_days * 86400, fade_days * 86400

    def _video_cache_ttl_seconds(self):
        """Backward-compatible name for the detailed-memory window."""
        return self._video_memory_windows_seconds()[0]

    def _compact_video_cache(self, cache):
        """Apply detail → long-term → faded lifecycle to the no-rewatch index."""
        now = datetime.now()
        detail_seconds, fade_seconds = self._video_memory_windows_seconds()
        changed = False
        for bvid, item in list(cache.items()):
            if not isinstance(item, dict):
                cache.pop(bvid, None); changed = True; continue
            try:
                created = datetime.strptime(str(item.get("time", "")), "%Y-%m-%d %H:%M")
                age_seconds = max(0.0, (now - created).total_seconds())
            except (TypeError, ValueError):
                age_seconds = fade_seconds + 1

            if age_seconds >= fade_seconds:
                trace = (
                    item.get("summary")
                    or item.get("analysis")
                    or item.get("desc")
                    or "曾经看过，具体内容已经淡忘。"
                )
                faded_summary = self._clip_media_text(trace, 120)
                keep = {
                    "bvid": str(item.get("bvid") or bvid),
                    "title": str(item.get("title") or ""),
                    "owner_name": str(item.get("owner_name") or ""),
                    "owner_mid": str(item.get("owner_mid") or ""),
                    "tname": str(item.get("tname") or ""),
                    "summary": faded_summary,
                    "time": str(item.get("time") or ""),
                    "source": str(item.get("source") or ""),
                    "memory_stage": "faded",
                    "faded_at": str(
                        item.get("faded_at") or now.strftime("%Y-%m-%d %H:%M")
                    ),
                }
                if item != keep:
                    cache[bvid] = keep
                    changed = True
                continue

            if age_seconds >= detail_seconds:
                trace = item.get("analysis") or item.get("summary") or item.get("desc") or ""
                summary = self._clip_media_text(trace, 220)
                if item.get("summary") != summary:
                    item["summary"] = summary
                    changed = True
                for key in ("analysis", "review"):
                    if key in item:
                        item.pop(key, None)
                        changed = True
                if item.get("memory_stage") != "long_term":
                    item["memory_stage"] = "long_term"
                    changed = True
                if "detail_expired_at" not in item:
                    item["detail_expired_at"] = now.strftime("%Y-%m-%d %H:%M")
                    changed = True
            elif item.get("memory_stage") != "detail":
                item["memory_stage"] = "detail"
                changed = True
        return changed

    def _should_use_visual_video_analysis(self, joint):
        return str(self.config.get("VIDEO_VISUAL_ANALYSIS_POLICY", "when_text_insufficient")) == "always" or not joint.get("has_signal")

    # ── 视频分析 ──
    async def _analyze_video_with_vision(self, video_info):
        joint = await self._build_joint_context(video_info)
        media_result = await self._analyze_video_media(video_info) if self._should_use_visual_video_analysis(joint) else None
        if media_result:
            merged = await self._merge_visual_and_joint(video_info, media_result, joint)
            return self._clip_media_text(merged or media_result, 1600)
        client = self._get_video_vision_client()
        model = self.config.get("VIDEO_VISION_MODEL", "")
        # Text/subtitle signals were enough and visual policy is conservative:
        # summarize without sending even a cover image to a vision provider.
        if joint.get("has_signal") and not self._should_use_visual_video_analysis(joint):
            return await self._analyze_video_text(video_info, joint=joint)
        dur_min = video_info.get("duration", 0) // 60
        dur_sec = video_info.get("duration", 0) % 60
        text_prompt = f"""请根据以下B站视频信息，写一段详细的内容概括（500字以内），包括：这个视频的主要内容和讲了什么、关键观点或亮点、视频类型/风格、可能的受众。

视频标题：{video_info.get('title', '未知')}
UP主：{video_info.get('owner_name', '未知')}
分区：{video_info.get('tname', '未知')}
时长：{dur_min}分{dur_sec}秒
简介：{video_info.get('desc', '无')[:500]}{joint['extra']}{joint['subtitle']}
{joint['joint_hint']}
直接输出概括内容，不要加前缀。"""
        provider_id = self.config.get("VIDEO_VISION_PROVIDER_ID", "")
        provider_result = await self._astrbot_multimodal_generate(provider_id, [{"type": "text", "text": text_prompt}], max_tokens=500)
        if provider_result:
            return self._clip_media_text(provider_result, 1600)
        if client and model and video_info.get("pic"):
            try:
                b64 = await self._fetch_image_base64(video_info["pic"])
                if b64:
                    content = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}, {"type": "text", "text": text_prompt}]
                    result = await self._vision_call(client, model, content, max_tokens=500)
                    if result:
                        return self._clip_media_text(result, 1600)
            except Exception as e:
                logger.warning(f"[BiliBot] 视觉分析封面失败: {e}")
        result = await self._llm_call(text_prompt, max_tokens=500)
        fallback = f"视频《{video_info.get('title', '未知')}》，UP主：{video_info.get('owner_name', '未知')}，分区：{video_info.get('tname', '未知')}。简介：{video_info.get('desc', '无')[:100]}"
        return self._clip_media_text(result or fallback, 1600)

    async def _analyze_video_text(self, video_info, joint=None):
        joint = joint or await self._build_joint_context(video_info)
        prompt = f"""请根据以下B站视频信息，写一段详细的内容概括（500字以内），包括：这个视频的主要内容和讲了什么、关键观点或亮点、视频类型/风格、可能的受众。

视频标题：{video_info.get('title', '未知')}
UP主：{video_info.get('up_name', '未知')}
分区：{video_info.get('tname', '未知')}
简介：{video_info.get('desc', '无')[:500]}{joint['extra']}{joint['subtitle']}
{joint['joint_hint']}
直接输出概括内容，不要加前缀。"""
        result = await self._llm_call(prompt, max_tokens=500)
        fallback = f"视频《{video_info.get('title', '未知')}》，UP主：{video_info.get('up_name', '未知')}"
        return self._clip_media_text(result or fallback, 1600)

    async def _analyze_video_media(self, video_info):
        provider_id = self.config.get("VIDEO_VISION_PROVIDER_ID", "")
        client = self._get_video_vision_client()
        model = self.config.get("VIDEO_VISION_MODEL", "")
        if not provider_id and (not client or not model):
            return None
        bvid = video_info.get("bvid", "")
        if not bvid:
            return None
        fmt = self._detect_video_format(model)
        video_path = await self._download_video(bvid)
        if not video_path:
            return None

        segments = []
        try:
            # 切片（短视频返回单段，长视频返回多段）
            segment_min = self.config.get("VIDEO_SEGMENT_MINUTES", 0)
            if segment_min:
                segment_sec = max(60, int(segment_min) * 60)
            else:
                # 兼容旧配置键（秒）
                segment_sec = int(self.config.get("VIDEO_SEGMENT_SEC", 300))
            max_segments = max(1, int(self.config.get("VIDEO_SEGMENT_MAX_COUNT", 10)))
            segments = await self._slice_video_segments(video_path, segment_sec=segment_sec, max_segments=max_segments)
            if not segments:
                return None

            segment_analyses = []
            for idx, seg_path in enumerate(segments):
                label = f"第{idx+1}/{len(segments)}段" if len(segments) > 1 else ""
                result = await self._analyze_single_segment(
                    seg_path, video_info, fmt, provider_id, client, model, label,
                )
                if result:
                    segment_analyses.append(result)

            if not segment_analyses:
                return None

            # 单段直接返回
            if len(segment_analyses) == 1:
                return segment_analyses[0]

            # 多段：LLM 整合
            return await self._consolidate_segment_analyses(video_info, segment_analyses)

        except Exception as e:
            logger.warning(f"[BiliBot] 视频媒体分析失败({bvid})：{e}")
            return None
        finally:
            # 清理所有切片和帧
            for seg in segments:
                self._cleanup_video_artifacts(seg)

    async def _analyze_single_segment(self, seg_path, video_info, fmt, provider_id, client, model, label=""):
        """分析单个视频片段，返回文本结果。"""
        frames = []
        try:
            segment_note = (
                f"这是按 {self.config.get('VIDEO_SEGMENT_MINUTES', 5)} 分钟左右切片后的{label}。"
                if label else "这是完整短视频。"
            )
            text_prompt = (
                f"这是B站视频「{video_info.get('title', '未知')}」{('的' + label) if label else ''}。{segment_note}\n"
                f"简介：「{video_info.get('desc', '无')[:300]}」。\n"
                f"请具体描述{'这一段' if label else '这个视频'}：①讲了什么/发生了什么 ②画面与风格 "
                f"③最有记忆点的细节（名场面、梗、金句、字幕关键信息）。"
                f"这一段只写本段内容，不要替整条视频下结论；不要泛泛而谈，120字以内。"
            )
            # 尝试视频直读
            if fmt != "none":
                with open(seg_path, "rb") as f:
                    video_b64 = base64.b64encode(f.read()).decode()
                content = self._build_video_content(video_b64, text_prompt, fmt)
                result = await self._astrbot_multimodal_generate(provider_id, content, max_tokens=200)
                if not result and client and model:
                    result = await self._vision_call(client, model, content, max_tokens=200)
                if result:
                    return self._clip_media_text(result, 600)
                logger.warning(f"[BiliBot] 视频直读失败（{fmt}），回退截帧{label}")
            # 回退：截帧分析
            if not os.path.exists(seg_path):
                logger.warning(f"[BiliBot] 片段文件不存在，无法截帧{label}: {seg_path}")
                return None
            frames = await self._extract_video_frames(seg_path, count=5)
            if not frames:
                return None
            frame_content = []
            for frame_path in frames:
                with open(frame_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode()
                frame_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
            frame_content.append({"type": "text", "text": text_prompt})
            result = await self._astrbot_multimodal_generate(provider_id, frame_content, max_tokens=200)
            if not result and client and model:
                result = await self._vision_call(client, model, frame_content, max_tokens=200)
            return self._clip_media_text(result, 600) if result else None
        except Exception as e:
            logger.warning(f"[BiliBot] 片段分析失败{label}: {e}")
            return None
        finally:
            self._cleanup_video_artifacts(None, frames)

    async def _consolidate_segment_analyses(self, video_info, segment_analyses):
        """多段分析结果整合为一段完整概括。"""
        parts = "\n".join(
            f"【第{i+1}段】{self._clip_media_text(text, 600)}"
            for i, text in enumerate(segment_analyses)
        )
        prompt = f"""以下是B站视频《{video_info.get('title', '未知')}》（UP主：{video_info.get('owner_name', '未知')}）分段分析的结果，请整合为一段完整的内容概括（300字以内）。

整合要求：
- 按视频推进顺序串起来，覆盖开头、中段、结尾的主要内容。
- 合并重复信息，保留具体画面、观点、梗、金句或关键字幕。
- 如果某些分段信息不足，就明确只依据已看到的分段，不要脑补。
- 最后用一句话概括视频整体风格或看点。

{parts}

直接输出整合后的概括，不要加前缀。"""
        result = await self._llm_call(prompt, max_tokens=400)
        if result:
            logger.info(f"[BiliBot] 📹 长视频{len(segment_analyses)}段整合完成")
            return self._clip_media_text(result, 1600)
        # 整合失败就拼接返回，同时限制注入后续上下文的总体长度。
        return self._clip_media_text(" | ".join(segment_analyses), 1600)

    def _detect_video_format(self, model: str) -> str:
        """读取用户配置的视频直读格式。"""
        fmt = self.config.get("VIDEO_VISION_FORMAT", "none").lower().strip()
        if fmt in ("gemini", "qwen"):
            return fmt
        return "none"

    def _build_video_content(self, video_b64: str, text_prompt: str, fmt: str) -> list:
        """根据格式构造视频直读的 content 列表。"""
        if fmt == "qwen":
            fps = self.config.get("VIDEO_VISION_FPS", 2)
            try:
                fps = max(1, int(fps))
            except (ValueError, TypeError):
                fps = 2
            return [
                {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{video_b64}"}, "fps": fps},
                {"type": "text", "text": text_prompt},
            ]
        else:
            # gemini 格式（也是默认）
            return [
                {"type": "image_url", "image_url": {"url": f"data:video/mp4;base64,{video_b64}"}},
                {"type": "text", "text": text_prompt},
            ]

    # ── 视频下载 / 压缩 / 截帧 ──

    # 分辨率回退链：先尝试无需合并的 MP4，避免无 ffmpeg 时只留下 m4a；再尝试高质量音视频合并。
    def _format_fallbacks(self, max_height=480):
        try:
            cap = max(144, int(max_height or 480))
        except Exception:
            cap = 480
        heights = [h for h in (360, 480, 720) if h <= cap]
        if not heights:
            heights = [cap]
        elif heights[-1] != cap and cap not in (360, 480, 720):
            heights.append(cap)

        formats = []
        for h in heights:
            formats.extend([
                # 横屏视频的短边通常是 height，竖屏视频的短边则是 width。
                # 两种方向都纳入同一档回退，避免 360x640 这类竖屏视频
                # 因 height>360 被误判为“没有 360p 格式”。
                f"best[ext=mp4][vcodec!=none][acodec!=none][height<={h}]/"
                f"best[ext=mp4][vcodec!=none][acodec!=none][width<={h}]/"
                f"best[height<={h}][vcodec!=none][acodec!=none]/"
                f"best[width<={h}][vcodec!=none][acodec!=none]",
                f"bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]/"
                f"bestvideo[ext=mp4][width<={h}]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={h}]+bestaudio/"
                f"bestvideo[width<={h}]+bestaudio/"
                f"best[height<={h}]/best[width<={h}]",
            ])
        formats.extend([
            "bestvideo+bestaudio/best",
            "worst[ext=mp4][vcodec!=none]/worst[vcodec!=none]",
        ])
        return formats
    _VIDEO_FILE_EXTS = {".mp4", ".mkv", ".webm", ".mov"}
    _AUDIO_FILE_EXTS = {".m4a", ".mp3", ".aac", ".opus", ".flac", ".wav"}

    def _pick_downloaded_video_file(self, bvid):
        if not os.path.isdir(TEMP_VIDEO_DIR):
            return None
        candidates = []
        for name in os.listdir(TEMP_VIDEO_DIR):
            if not name.startswith(bvid) or name.endswith("_cookies.txt"):
                continue
            fp = os.path.join(TEMP_VIDEO_DIR, name)
            if not os.path.isfile(fp) or name.endswith((".part", ".ytdl")):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in self._AUDIO_FILE_EXTS:
                continue
            if ext not in self._VIDEO_FILE_EXTS:
                continue
            size = os.path.getsize(fp)
            if size > 0:
                candidates.append((ext == ".mp4", size, fp))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][2]

    async def _download_video(self, bvid, max_height=480):
        output_template = os.path.join(TEMP_VIDEO_DIR, f"{bvid}.%(ext)s")
        # 生成 Netscape 格式 cookie 文件，兼容新版 yt-dlp
        cookie_file = os.path.join(TEMP_VIDEO_DIR, f"{bvid}_cookies.txt")
        sessdata = self.config.get('SESSDATA', '')
        bili_jct = self.config.get('BILI_JCT', '')
        dede_uid = self.config.get('DEDE_USER_ID', '')
        buvid3 = self.config.get('BUVID3', '')
        cookie_content = (
            "# Netscape HTTP Cookie File\n"
            f".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\t{sessdata}\n"
            f".bilibili.com\tTRUE\t/\tFALSE\t0\tbili_jct\t{bili_jct}\n"
            f".bilibili.com\tTRUE\t/\tFALSE\t0\tDedeUserID\t{dede_uid}\n"
        )
        if buvid3:
            cookie_content += f".bilibili.com\tTRUE\t/\tFALSE\t0\tbuvid3\t{buvid3}\n"
        try:
            fd = os.open(cookie_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(cookie_content)
        except Exception as e:
            logger.warning(f"[BiliBot] Cookie文件写入失败: {e}")
            return None

        last_err = ""
        try:
            for fmt in self._format_fallbacks(max_height):
                # 清理上一轮可能残留的部分文件
                self._cleanup_partial_downloads(bvid)
                code, _, stderr = await self._run_process(
                    "yt-dlp", "-o", output_template,
                    "--format", fmt,
                    "--no-playlist", "--merge-output-format", "mp4",
                    "--recode-video", "mp4",
                    "--cookies", cookie_file,
                    "--add-header", "Referer: https://www.bilibili.com",
                    "--limit-rate", "2M",
                    f"https://www.bilibili.com/video/{bvid}",
                    timeout=600,
                )
                if code == 0:
                    fp = self._pick_downloaded_video_file(bvid)
                    if fp:
                        logger.info(f"[BiliBot] 视频下载成功({bvid})，格式: {fmt}，文件: {os.path.basename(fp)}")
                        return fp
                    last_err = "yt-dlp 成功退出，但没有产出可发送的视频文件（可能只下载到音频）"
                    logger.info(f"[BiliBot] {last_err}({bvid})，尝试下一个格式")
                    continue
                last_err = stderr[:200] if stderr else "unknown error"
                logger.info(f"[BiliBot] 格式 {fmt} 下载失败({bvid})，尝试下一个: {last_err[:80]}")
        finally:
            try:
                os.remove(cookie_file)
            except OSError:
                pass

        logger.warning(f"[BiliBot] 视频下载全部失败({bvid}): {last_err}")
        return None

    def _cleanup_partial_downloads(self, bvid):
        """清理某个 bvid 的残留下载文件（不删 cookie）"""
        if not os.path.isdir(TEMP_VIDEO_DIR):
            return
        for name in os.listdir(TEMP_VIDEO_DIR):
            if name.startswith(bvid) and not name.endswith("_cookies.txt"):
                try:
                    fp = os.path.join(TEMP_VIDEO_DIR, name)
                    if os.path.isfile(fp):
                        os.remove(fp)
                except OSError:
                    pass

    async def _get_video_duration(self, video_path):
        """获取视频实际时长（秒）"""
        code, stdout, _ = await self._run_process(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path, timeout=60,
        )
        try:
            return float(stdout.strip()) if code == 0 and stdout.strip() else 0.0
        except ValueError:
            return 0.0

    async def _compress_video(self, input_path):
        """压缩单段视频，AI分析用，极速编码优先。"""
        output_path = input_path.rsplit(".", 1)[0] + "_compressed.mp4"
        code, _, stderr = await self._run_process(
            "ffmpeg", "-y", "-i", input_path,
            "-vf", "scale=360:-2", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35",
            output_path, timeout=300,
        )
        if code != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.warning(f"[BiliBot] 视频压缩失败或输出为空，回退原视频: {stderr[:160] if stderr else ''}")
            return input_path
        try:
            os.remove(input_path)
        except OSError:
            pass
        return output_path

    async def _slice_video_segments(self, video_path, segment_sec=300, max_segments=10):
        """将长视频切成多个片段，每段 segment_sec 秒，最多 max_segments 段，返回片段路径列表。
        短视频（<=segment_sec）直接压缩返回单段。"""
        duration = await self._get_video_duration(video_path)
        if duration <= 0:
            duration = segment_sec  # 拿不到时长就当一段处理

        if duration <= segment_sec:
            # 短视频：直接压缩整段
            compressed = await self._compress_video(video_path)
            return [compressed]

        # 长视频：切片
        segments = []
        num_segments = min(int(duration // segment_sec) + (1 if duration % segment_sec > 10 else 0), max_segments)
        base = video_path.rsplit(".", 1)[0]
        for i in range(num_segments):
            start = i * segment_sec
            seg_path = f"{base}_seg{i}.mp4"
            code, _, stderr = await self._run_process(
                "ffmpeg", "-y", "-ss", str(start), "-i", video_path,
                "-t", str(segment_sec),
                "-vf", "scale=360:-2", "-an",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35",
                seg_path, timeout=300,
            )
            if code == 0 and os.path.exists(seg_path):
                segments.append(seg_path)
            else:
                logger.warning(f"[BiliBot] 切片{i}失败: {stderr[:100] if stderr else 'unknown'}")
        # 清理原始文件
        try:
            os.remove(video_path)
        except OSError:
            pass
        if not segments:
            logger.warning("[BiliBot] 所有切片均失败")
        return segments

    async def _extract_video_frames(self, video_path, count=5):
        frame_dir = video_path.rsplit(".", 1)[0] + "_frames"
        os.makedirs(frame_dir, exist_ok=True)
        code, stdout, _ = await self._run_process(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path, timeout=60,
        )
        try:
            duration = float(stdout.strip()) if code == 0 and stdout.strip() else 30.0
        except ValueError:
            duration = 30.0
        frames = []
        for i in range(count):
            ts = duration * (i + 1) / (count + 1)
            frame_path = os.path.join(frame_dir, f"frame_{i}.jpg")
            code, _, _ = await self._run_process(
                "ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", video_path,
                "-vframes", "1", "-vf", "scale=360:-2", "-q:v", "8",
                frame_path, timeout=120,
            )
            if code == 0 and os.path.exists(frame_path):
                frames.append(frame_path)
        return frames

    def _cleanup_video_artifacts(self, video_path, frames=None):
        paths = list(frames or [])
        if video_path:
            paths.append(video_path)
            frame_dir = video_path.rsplit(".", 1)[0] + "_frames"
        else:
            frame_dir = ""
        for path in paths:
            try:
                if path and os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        if frame_dir and os.path.isdir(frame_dir):
            try:
                shutil.rmtree(frame_dir)
            except OSError:
                pass

    # ── 视频上下文（评论区用） ──
    async def _get_video_context(self, oid, comment_type):
        if comment_type != 1:
            return "", None
        vc = self._load_json(VIDEO_MEMORY_FILE, {})
        if self._compact_video_cache(vc):
            self._save_json(VIDEO_MEMORY_FILE, vc)
        bvid = await self._oid_to_bvid(oid)
        if not bvid:
            return "", None
        if bvid in vc:
            c = vc[bvid]
            if self._analysis_has_subtitle_mismatch(c.get("analysis") or c.get("summary", "")):
                logger.warning(f"[BiliBot] 评论上下文丢弃字幕错配缓存: {bvid}")
                vc.pop(bvid, None)
                self._save_json(VIDEO_MEMORY_FILE, vc)
            else:
                await self._mark_video_seen(
                    bvid, c, source="comment_video_context", increment=False
                )
                has_mem = any(m.get("bvid") == bvid or m.get("thread_id") == f"video:{bvid}" for m in self._memory)
                cached_summary = c.get("analysis") or c.get("summary") or "已看过该视频；完整分析缓存已清理。"
                if (
                    not has_mem
                    and self.config.get("ENABLE_VIDEO_LONG_TERM_MEMORY", True)
                    and c.get("memory_stage") != "faded"
                ):
                    mem_time = c.get("time", datetime.now().strftime("%Y-%m-%d %H:%M"))
                    memory_text = (
                        f"[{mem_time}] 视频摘要：标题《{c.get('title', '')}》 "
                        f"UP主:{c.get('owner_name', '')} 内容:{self._clip_media_text(cached_summary, 180)}"
                    )
                    await self._save_self_memory_record(
                        f"video:{bvid}", memory_text, memory_type="video",
                        extra={"bvid": bvid, "owner_mid": str(c.get("owner_mid", "")), "owner_name": c.get("owner_name", ""), "video_title": c.get("title", ""), "time": mem_time},
                    )
                ctx = f"【当前视频】\n标题：{c.get('title', '')}\nUP主：{c.get('owner_name', '')}（UID:{c.get('owner_mid', '')}）\n分区：{c.get('tname', '')}\n简介：{c.get('desc', '')[:150]}\n内容概括：{self._clip_media_text(cached_summary, 240)}"
                tags = await self._get_video_tags(bvid)
                comments = await self._get_hot_comments(oid)
                if tags:
                    ctx += f"\n标签：{'、'.join(tags[:10])}"
                if comments:
                    ctx += "\n热门评论：" + " / ".join(comments[:3])
                return ctx, c
        vi = await self._get_video_info(oid)
        if not vi:
            return "", None
        seen_trace = await self._seen_video_record(bvid)
        if seen_trace:
            ctx = (
                f"【当前视频·曾经看过】\n标题：{vi['title']}\n"
                f"UP主：{vi['owner_name']}（UID:{vi['owner_mid']}）\n"
                f"分区：{vi['tname']}\n简介：{vi.get('desc', '')[:150]}\n"
                "观看细节已经淡忘；本次不会自动重新下载分析。"
            )
            return ctx, seen_trace
        logger.info(f"[BiliBot] 📹 新视频，分析中：《{vi['title']}》by {vi['owner_name']}")
        analysis = self._clip_media_text(await self._analyze_video_with_vision(vi), 1600)
        logger.info(f"[BiliBot] 📹 分析结果：{analysis[:60]}...")
        analyzed_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        cache_entry = {"bvid": bvid, "title": vi["title"], "desc": vi.get("desc", "")[:200], "owner_name": vi["owner_name"], "owner_mid": str(vi["owner_mid"]), "tname": vi["tname"], "analysis": analysis, "summary": self._clip_media_text(analysis, 220), "time": analyzed_at}
        vc[bvid] = cache_entry
        self._save_json(VIDEO_MEMORY_FILE, vc)
        await self._mark_video_seen(
            bvid, cache_entry, source="comment_video_context"
        )
        memory_text = (
            f"[{analyzed_at}] 视频分析记忆：标题《{vi['title']}》 "
            f"UP主:{vi['owner_name']} 分区:{vi['tname']} "
            f"简介:{vi.get('desc', '')[:120]} 内容概括:{analysis[:200]}"
        )
        if self.config.get("ENABLE_VIDEO_LONG_TERM_MEMORY", True):
            await self._save_self_memory_record(
                f"video:{bvid}", memory_text, memory_type="video",
                extra={"bvid": bvid, "owner_mid": str(vi["owner_mid"]), "owner_name": vi.get("owner_name", ""), "video_title": vi["title"]},
            )
        ctx = f"【当前视频】\n标题：{vi['title']}\nUP主：{vi['owner_name']}（UID:{vi['owner_mid']}）\n分区：{vi['tname']}\n简介：{vi.get('desc', '')[:150]}\n内容概括：{analysis}"
        tags = await self._get_video_tags(bvid)
        comments = await self._get_hot_comments(oid)
        if tags:
            ctx += f"\n标签：{'、'.join(tags[:10])}"
        if comments:
            ctx += "\n热门评论：" + " / ".join(comments[:3])
        return ctx, cache_entry

    # ── 动态上下文 ──
    async def _get_dynamic_context(self, oid, comment_type=17):
        # comment_type=11: 图文动态，oid是doc_id（相簿ID），用相簿API
        # comment_type=17: 纯文字动态，oid是dynamic_id，用动态详情API
        try:
            if comment_type == 11:
                # 图文动态：用相簿API获取内容（内部已含空间列表fallback）
                ctx = await self._get_draw_context(oid)
                if ctx:
                    return ctx
                # _get_draw_context 所有方案都失败了，直接走记忆兜底
                # 注意：oid 是 doc_id，不能当 dynamic_id 用，所以不走下面的详情API
            else:
                # 纯文字动态：oid 就是 dynamic_id，直接查详情API
                d, _ = await self._http_get("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail", params={
                    "id": oid, "timezone_offset": -480,
                    "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
                })
                if not isinstance(d, dict):
                    logger.debug(f"[BiliBot] 动态详情API返回非dict: {type(d)}")
                elif d.get("code") == 0:
                    item = (d.get("data") or {}).get("item") or {}
                    modules = item.get("modules") or {}
                    desc = (modules.get("module_dynamic") or {}).get("desc") or {}
                    text = desc.get("text", "")
                    author = modules.get("module_author") or {}
                    author_name = author.get("name", "")
                    author_mid = str(author.get("mid", ""))
                    pub_time = author.get("pub_time", "")
                    bot_mid = self.config.get("DEDE_USER_ID", "")
                    is_self = author_mid == bot_mid

                    # 提取图片（兼容opus和draw两种格式）
                    major = (modules.get("module_dynamic") or {}).get("major") or {}
                    major_type = major.get("type", "")
                    if major_type == "MAJOR_TYPE_OPUS" or "opus" in major:
                        opus = major.get("opus") or {}
                        opus_text = (opus.get("summary") or {}).get("text", "") or opus.get("title", "")
                        if opus_text and not text:
                            text = opus_text
                        image_urls = [p.get("url", "") for p in (opus.get("pics") or []) if p.get("url")]
                    elif "draw" in major:
                        draw = major.get("draw") or {}
                        image_urls = [img.get("src", "") for img in (draw.get("items") or []) if img.get("src")]
                    else:
                        image_urls = []

                    if not text and not image_urls:
                        pass
                    else:
                        label = "Bot自己发的" if is_self else f"{author_name}发的"
                        ctx = f"【当前动态（{label}）】\n内容：{self._clip_media_text(text, 1200) or '（无文字）'}"
                        if pub_time:
                            ctx += f"\n发布时间：{pub_time}"

                        if image_urls:
                            logger.info(f"[BiliBot] 🖼️ 动态含 {len(image_urls)} 张图片，识别中...")
                            image_desc = await self._recognize_images(image_urls[:4])
                            if image_desc:
                                ctx += f"\n图片内容：{self._clip_media_text(image_desc, 500)}"
                            else:
                                ctx += f"\n（动态含{len(image_urls)}张图片，识别失败）"

                        return ctx
                else:
                    logger.debug(f"[BiliBot] 动态详情API返回非0: code={d.get('code')} msg={d.get('message', '')}")
        except Exception as e:
            logger.debug(f"[BiliBot] 动态API获取失败: {e}")
        dynamic_mems = [m for m in self._memory if m.get("memory_type") == "dynamic"]
        if dynamic_mems:
            latest = dynamic_mems[-1]
            return f"【最近发布的动态】\n{self._clip_media_text(latest.get('text', ''), 1200)}"
        return ""

    async def _get_draw_context(self, doc_id):
        """通过相簿API获取图文动态内容（comment_type=11时oid是doc_id）"""
        # 方案1: 相簿详情API
        for api_url in [
            f"https://api.bilibili.com/x/dynamic/feed/draw/doc_detail?doc_id={doc_id}",
            f"https://api.vc.bilibili.com/link_draw/v1/doc/detail?doc_id={doc_id}",
        ]:
            try:
                d, _ = await self._http_get(api_url)
                if isinstance(d, dict) and d.get("code") == 0:
                    item = d.get("data", {}).get("item", {})
                    description = item.get("description", "")
                    pictures = item.get("pictures", [])
                    image_urls = [p.get("img_src", "") for p in pictures if p.get("img_src")]

                    user = d.get("data", {}).get("user", {})
                    author_name = user.get("name", user.get("head_url", ""))
                    author_mid = str(user.get("uid", user.get("mid", "")))
                    bot_mid = self.config.get("DEDE_USER_ID", "")
                    is_self = author_mid == bot_mid

                    label = "Bot自己发的" if is_self else f"{author_name}发的"
                    ctx = f"【当前动态（{label}）】\n内容：{self._clip_media_text(description, 1200) or '（无文字）'}"

                    if image_urls:
                        logger.info(f"[BiliBot] 🖼️ 图文动态含 {len(image_urls)} 张图片，识别中...")
                        image_desc = await self._recognize_images(image_urls[:4])
                        if image_desc:
                            ctx += f"\n图片内容：{self._clip_media_text(image_desc, 500)}"
                            mem_key = f"dynamic_img:{doc_id}"
                            has_mem = any(m.get("thread_id") == mem_key for m in self._memory)
                            if not has_mem:
                                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                                mem_text = f"[{now_str}] 动态图片记忆：{author_name}的动态「{description[:60]}」图片内容：{image_desc[:200]}"
                                await self._save_self_memory_record(
                                    mem_key, mem_text, memory_type="dynamic",
                                    extra={"dynamic_id": str(doc_id), "author_mid": author_mid},
                                )
                                logger.info(f"[BiliBot] 📸 存入动态图片记忆")
                        else:
                            ctx += f"\n（动态含{len(image_urls)}张图片，识别失败）"

                    logger.info(f"[BiliBot] ✅ 相簿API成功获取动态内容: doc_id={doc_id}")
                    return ctx
            except Exception as e:
                logger.debug(f"[BiliBot] 相簿API({api_url})获取失败: {e}")

        # 方案2: 从Bot自己的动态列表中查找匹配的dynamic_id
        try:
            bot_mid = self.config.get("DEDE_USER_ID", "")
            if bot_mid:
                d, _ = await self._http_get(
                    "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                    params={
                        "host_mid": bot_mid, "offset": "",
                        "timezone_offset": -480,
                        "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
                    },
                )
                if d.get("code") == 0:
                    for item in d.get("data", {}).get("items", []):
                        basic = item.get("basic", {})
                        # rid_str 对应 doc_id
                        if basic.get("rid_str") == str(doc_id) or basic.get("comment_id_str") == str(doc_id):
                            dynamic_id = item.get("id_str", "")
                            if dynamic_id:
                                logger.info(f"[BiliBot] 🔄 通过空间动态列表找到 dynamic_id={dynamic_id} (doc_id={doc_id})")
                                # 用 dynamic_id 调详情API
                                return await self._get_dynamic_context_by_id(dynamic_id)
        except Exception as e:
            logger.debug(f"[BiliBot] 空间动态列表查找失败: {e}")

        return ""

    async def _get_dynamic_context_by_id(self, dynamic_id):
        """用 dynamic_id 调动态详情API"""
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail", params={
                "id": dynamic_id, "timezone_offset": -480,
                "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
            })
            if not isinstance(d, dict):
                logger.debug(f"[BiliBot] 动态详情API返回非dict: {type(d)} (id={dynamic_id})")
                return ""
            if d.get("code") == 0:
                item = (d.get("data") or {}).get("item") or {}
                if not item:
                    logger.debug(f"[BiliBot] 动态详情API返回code=0但item为空 (id={dynamic_id})")
                    return ""
                modules = item.get("modules") or {}
                desc = (modules.get("module_dynamic") or {}).get("desc") or {}
                text = desc.get("text", "")
                author = modules.get("module_author") or {}
                author_name = author.get("name", "")
                pub_time = author.get("pub_time", "")
                bot_mid = self.config.get("DEDE_USER_ID", "")
                is_self = str(author.get("mid", "")) == bot_mid

                major = (modules.get("module_dynamic") or {}).get("major") or {}
                major_type = major.get("type", "")

                # opus格式（features=itemOpusStyle时）
                if major_type == "MAJOR_TYPE_OPUS" or "opus" in major:
                    opus = major.get("opus") or {}
                    # opus格式的文字在 summary.text 或 title
                    opus_summary = (opus.get("summary") or {}).get("text", "")
                    opus_title = opus.get("title", "")
                    if opus_summary:
                        text = opus_summary
                    elif opus_title:
                        text = opus_title
                    image_urls = [p.get("url", "") for p in (opus.get("pics") or []) if p.get("url")]
                # 传统draw格式
                elif "draw" in major:
                    draw = major.get("draw") or {}
                    image_urls = [img.get("src", "") for img in (draw.get("items") or []) if img.get("src")]
                else:
                    image_urls = []
                    if major:
                        logger.debug(f"[BiliBot] 未知major类型 (id={dynamic_id}): type={major_type} keys={list(major.keys())}")

                label = "Bot自己发的" if is_self else f"{author_name}发的"
                ctx = f"【当前动态（{label}）】\n内容：{self._clip_media_text(text, 1200) or '（无文字）'}"
                if pub_time:
                    ctx += f"\n发布时间：{pub_time}"

                if image_urls:
                    logger.info(f"[BiliBot] 🖼️ 动态含 {len(image_urls)} 张图片，识别中...")
                    image_desc = await self._recognize_images(image_urls[:4])
                    if image_desc:
                        ctx += f"\n图片内容：{self._clip_media_text(image_desc, 500)}"
                    else:
                        ctx += f"\n（动态含{len(image_urls)}张图片，识别失败）"

                logger.info(f"[BiliBot] ✅ 动态详情获取成功 (id={dynamic_id}): {text[:50] if text else '无文字'}")
                return ctx
            else:
                logger.debug(f"[BiliBot] 动态详情API返回非0 (id={dynamic_id}): code={d.get('code')} msg={d.get('message', '')} data_keys={list((d.get('data') or {}).keys())}")
        except Exception as e:
            logger.debug(f"[BiliBot] 动态详情API获取失败(id={dynamic_id}): {e}")
        return ""



