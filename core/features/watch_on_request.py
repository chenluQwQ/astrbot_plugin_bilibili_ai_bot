"""按需看片：工具、私信、QQ 分享叫 Bot 看一个视频。

看片本身在 ``domain/video_watch.py``；这里是调用方：查缓存和看过记录、看完写视频缓存、记看过、
评价成功才写长期记忆，最后拼成给回复用的文字。
"""
from datetime import datetime

from astrbot.api import logger
from ..infra.host_view import HostView
from ..video_timeline import watch_digest


class WatchOnRequest(HostView):
    HOST_API = frozenset({"memory_docs", 
        "bili_videos", "json_store", "memory_writer", "seen_videos", "settings", "video_evaluate",
        "video_watch",
    })

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
                oid = await self.bili_videos._get_video_oid(raw_id)
            if not oid:
                return {"ok": False, "message": f"找不到视频 {raw_id}"}

            vi = await self.bili_videos._get_video_info(oid)
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

            if not force_rewatch:
                self.video_watch._cached_video_memory(actual_bvid)
            video_cache = self.memory_docs.video_memory.read({})
            cached = video_cache.get(actual_bvid, {})
            cached_text = str(cached.get("analysis") or cached.get("summary") or "").strip()
            removed_bad_cache = False
            if self.video_watch._analysis_has_subtitle_mismatch(cached_text):
                logger.warning(
                    f"[BiliBot] 丢弃疑似字幕错配的旧视频缓存，重新分析: {actual_bvid}"
                )
                video_cache.pop(actual_bvid, None)
                cached = {}
                removed_bad_cache = True
            if self.video_watch._compact_video_cache(video_cache) or removed_bad_cache:
                self.memory_docs.video_memory.write(video_cache)
            cached = video_cache.get(actual_bvid, {})
            cached_analysis = str(cached.get("analysis", "") or "").strip()
            cached_summary = str(cached.get("summary", "") or "").strip()
            preview_only = cached.get("content_analyzed") is False or (cached.get("source") == "group_share" and not cached.get("content_analyzed"))
            if (cached_analysis or cached_summary) and not force_rewatch and not preview_only:
                video_description = self.video_watch._clip_media_text(cached_analysis or cached_summary, 3500)
                score = cached.get("score")
                score_reason = str(cached.get("score_reason", "") or "")
                mood = cached.get("mood", "平静")
                review = self.video_watch._clip_media_text(cached.get("review", "") or "完整分析已清理，仅保留简短摘要", 320)
                from_cache = True
                await self.seen_videos.mark(
                    actual_bvid, analysis_info, source=memory_source, increment=False
                )
                logger.info(f"[BiliBot] 📹 私信看片命中视频短期缓存/摘要索引：《{vi.get('title', '')}》")
            else:
                seen_trace = await self.seen_videos.record(actual_bvid)
                if seen_trace and not force_rewatch and not preview_only:
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
                analysis_info["_force_rewatch"] = force_rewatch or removed_bad_cache
                video_description = self.video_watch._clip_media_text(
                    await self.video_watch._analyze_video_with_vision(analysis_info), 3500
                )
                if analysis_info.get("_analysis_from_cache"):
                    return await self._watch_video_and_save_memory(actual_bvid, memory_source=memory_source)
                evaluation = await self.video_evaluate._evaluate_video(
                    analysis_info, video_description
                )
                score = (evaluation or {}).get("score")
                score_reason = str((evaluation or {}).get("score_reason", ""))
                mood = (evaluation or {}).get("mood", "平静")
                review = self.video_watch._clip_media_text((evaluation or {}).get("review", ""), 320)
                preference_signals = list((evaluation or {}).get("preference_signals", []) or [])
                search_keywords = list((evaluation or {}).get("search_keywords", []) or [])
                from_cache = False

                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                video_cache = self.memory_docs.video_memory.read({})
                video_cache[actual_bvid] = {
                    "bvid": actual_bvid,
                    "title": vi.get("title", ""),
                    "desc": str(vi.get("desc", "") or "")[:200],
                    "owner_name": vi.get("owner_name", ""),
                    "owner_mid": str(vi.get("owner_mid", "")),
                    "tname": vi.get("tname", ""),
                    "analysis": video_description,
                    "summary": watch_digest(video_description, 220),
                    "score": score,
                    "score_reason": score_reason,
                    "mood": mood,
                    "review": review,
                    "preference_signals": preference_signals,
                    "search_keywords": search_keywords,
                    "time": now_str,
                    "source": memory_source,
                    "analysis_basis": analysis_info.get("analysis_basis", "legacy"),
                    "content_analyzed": analysis_info.get("analysis_basis") != "metadata",
                }
                self.memory_docs.video_memory.write(video_cache)
                if analysis_info.get("analysis_basis") != "metadata":
                    await self.seen_videos.mark(
                        actual_bvid, analysis_info, source=memory_source
                    )

                # 和主动看片一致：评价失败时不写长期记忆。视频内容（上面的缓存）照样留着，
                # 下次不用重新下载分析，但不会留下一条「评分:None 感想:空」的记忆。
                if evaluation and self.settings.get("ENABLE_VIDEO_LONG_TERM_MEMORY"):
                    memory_text = (
                        f"[{now_str}] 视频摘要《{vi.get('title', '')}》"
                        f"(UP主:{vi.get('owner_name', '')}) 评分:{score}/10 "
                        f"理由:{score_reason[:80]} 感想:{review[:80]} 内容:{watch_digest(video_description, 220)}"
                    )
                    if preference_signals:
                        memory_text += " 兴趣信号:" + "、".join(
                            f"{item.get('polarity')}:{item.get('value')}"
                            for item in preference_signals[:5]
                        )
                    await self.memory_writer._save_self_memory_record(
                        f"{memory_source}:{actual_bvid}", self.video_watch._clip_media_text(memory_text, 520), memory_type="video",
                        extra={"bvid": actual_bvid, "owner_mid": str(vi.get("owner_mid", "")), "owner_name": vi.get("owner_name", ""), "video_title": vi.get("title", ""), "tname": (evaluation or {}).get("partition") or vi.get("tname", ""), "score": score, "score_reason": score_reason, "mood": mood, "review": review, "impression": str((evaluation or {}).get("impression") or ""), "preference_signals": preference_signals, "search_keywords": search_keywords, "analysis_basis": analysis_info.get("analysis_basis", "legacy"), "content_analyzed": analysis_info.get("analysis_basis") != "metadata"},
                    )
                logger.info(f"[BiliBot] ✅ 私信分享视频已看完并写入短期缓存：{actual_bvid}")

            link = f"https://www.bilibili.com/video/{actual_bvid}"
            memory_note = "记忆已老化，仅保留摘要与当时感受，未重新观看。\n" if from_cache and cached.get("memory_stage") == "faded" else ""
            message = (
                f"[{'已从记忆读取视频' if from_cache else '已完成视频分析'}]\n"
                f"标题：{vi.get('title', '')}\n"
                f"{memory_note}"
                f"UP主：{vi.get('owner_name', '')}(UID:{vi.get('owner_mid', '')}) | 分区：{vi.get('tname', '')}\n"
                f"链接：{link}\n"
                f"视频简介：{str(vi.get('desc', '') or '')[:150]}\n"
                f"内容详情：{video_description[:800]}\n"
                f"我的感受：{review[:200]}\n"
                f"个人评分：{str(score) + '/10' if score is not None else '未取得可靠评分，未评分'} | 心情：{mood}\n"
                f"评分理由：{score_reason}\n"
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
