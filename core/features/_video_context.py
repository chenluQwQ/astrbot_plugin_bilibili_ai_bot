"""私信、QQ 回复时拼「当前视频」：读视频缓存，没看过就分析一遍并记下来，再带上标签和热评。
"""
from datetime import datetime

from astrbot.api import logger
from ..infra.host_view import HostView


class VideoContext(HostView):
    HOST_API = frozenset({"memory_docs", 
        "bili_comments", "bili_videos", "json_store", "memories", "memory_writer", "seen_videos",
        "settings", "video_watch",
    })

    async def _get_video_context(self, oid, comment_type):
        if comment_type != 1:
            return "", None
        bvid = await self.bili_videos._oid_to_bvid(oid)
        if bvid:
            self.video_watch._cached_video_memory(bvid)
        vc = self.memory_docs.video_memory.read({})
        if self.video_watch._compact_video_cache(vc):
            self.memory_docs.video_memory.write(vc)
        if not bvid:
            return "", None
        if bvid in vc and vc[bvid].get("content_analyzed") is not False and not (vc[bvid].get("source") == "group_share" and not vc[bvid].get("content_analyzed")):
            c = vc[bvid]
            if self.video_watch._analysis_has_subtitle_mismatch(c.get("analysis") or c.get("summary", "")):
                logger.warning(f"[BiliBot] 评论上下文丢弃字幕错配缓存: {bvid}")
                vc.pop(bvid, None)
                self.memory_docs.video_memory.write(vc)
            else:
                await self.seen_videos.mark(
                    bvid, c, source="comment_video_context", increment=False
                )
                has_mem = await self.memories.exists(bvid=bvid)
                cached_summary = c.get("analysis") or c.get("summary") or "已看过该视频；完整分析缓存已清理。"
                if (
                    not has_mem
                    and self.settings.get("ENABLE_VIDEO_LONG_TERM_MEMORY")
                    and c.get("memory_stage") != "faded"
                ):
                    mem_time = c.get("time", datetime.now().strftime("%Y-%m-%d %H:%M"))
                    memory_text = (
                        f"[{mem_time}] 视频摘要：标题《{c.get('title', '')}》 "
                        f"UP主:{c.get('owner_name', '')} 内容:{self.video_watch._clip_media_text(cached_summary, 180)}"
                    )
                    # 以前看过、缓存里有评分的直接用；没有的（以前也是评论区顺带看的）现在补打
                    cached_score = c.get("score")
                    if isinstance(cached_score, (int, float)) and not isinstance(cached_score, bool):
                        scored = {"score": cached_score}
                    else:
                        scored = await self.memory_writer._score_video_memory(memory_text)
                    await self.memory_writer._save_self_memory_record(
                        f"video:{bvid}", memory_text, memory_type="video",
                        extra={"bvid": bvid, "owner_mid": str(c.get("owner_mid", "")), "owner_name": c.get("owner_name", ""), "video_title": c.get("title", ""), "time": mem_time, **scored},
                    )
                ctx = f"【当前视频·从观看记忆读取】\n标题：{c.get('title', '')}\nUP主：{c.get('owner_name', '')}（UID:{c.get('owner_mid', '')}）\n分区：{c.get('tname', '')}\n简介：{c.get('desc', '')[:150]}\n内容概括：{self.video_watch._clip_media_text(cached_summary, 240)}"
                if c.get("score") is not None:
                    ctx += f"\n当时评分：{c.get('score')}/10；理由：{c.get('score_reason', '')}；感想：{c.get('review', '')}"
                tags = await self.bili_videos._get_video_tags(bvid)
                comments = await self.bili_comments._get_hot_comments(oid)
                if tags:
                    ctx += f"\n标签：{'、'.join(tags[:10])}"
                if comments:
                    ctx += "\n热门评论：" + " / ".join(comments[:3])
                return ctx, c
        vi = await self.bili_videos._get_video_info(oid)
        if not vi:
            return "", None
        seen_trace = await self.seen_videos.record(bvid)
        if seen_trace:
            ctx = (
                f"【当前视频·曾经看过】\n标题：{vi['title']}\n"
                f"UP主：{vi['owner_name']}（UID:{vi['owner_mid']}）\n"
                f"分区：{vi['tname']}\n简介：{vi.get('desc', '')[:150]}\n"
                "观看细节已经淡忘；本次不会自动重新下载分析。"
            )
            return ctx, seen_trace
        logger.info(f"[BiliBot] 📹 新视频，分析中：《{vi['title']}》by {vi['owner_name']}")
        analysis = self.video_watch._clip_media_text(await self.video_watch._analyze_video_with_vision(vi), 1600)
        logger.info(f"[BiliBot] 📹 分析结果：{analysis[:60]}...")
        analyzed_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        cache_entry = {"bvid": bvid, "title": vi["title"], "desc": vi.get("desc", "")[:200], "owner_name": vi["owner_name"], "owner_mid": str(vi["owner_mid"]), "tname": vi["tname"], "analysis": analysis, "summary": self.video_watch._clip_media_text(analysis, 360), "time": analyzed_at, "analysis_basis": vi.get("analysis_basis", "legacy"), "content_analyzed": vi.get("analysis_basis") != "metadata"}
        vc = self.memory_docs.video_memory.read({})
        previous = vc.get(bvid, {})
        if vi.get("_analysis_from_cache") and isinstance(previous, dict):
            cache_entry = previous
        vc[bvid] = cache_entry
        self.memory_docs.video_memory.write(vc)
        if vi.get("analysis_basis") != "metadata":
            await self.seen_videos.mark(
                bvid, cache_entry, source="comment_video_context", increment=not vi.get("_analysis_from_cache", False)
            )
        memory_text = (
            f"[{analyzed_at}] 视频分析记忆：标题《{vi['title']}》 "
            f"UP主:{vi['owner_name']} 分区:{vi['tname']} "
            f"简介:{vi.get('desc', '')[:120]} 内容概括:{analysis[:200]}"
        )
        if self.settings.get("ENABLE_VIDEO_LONG_TERM_MEMORY") and vi.get("analysis_basis") != "metadata":
            scored = await self.memory_writer._score_video_memory(memory_text)
            await self.memory_writer._save_self_memory_record(
                f"video:{bvid}", memory_text, memory_type="video",
                extra={"bvid": bvid, "owner_mid": str(vi["owner_mid"]), "owner_name": vi.get("owner_name", ""), "video_title": vi["title"], "analysis_basis": vi.get("analysis_basis", "legacy"), "content_analyzed": vi.get("analysis_basis") != "metadata", **scored},
            )
        ctx = f"【当前视频】\n标题：{vi['title']}\nUP主：{vi['owner_name']}（UID:{vi['owner_mid']}）\n分区：{vi['tname']}\n简介：{vi.get('desc', '')[:150]}\n内容概括：{analysis}"
        tags = await self.bili_videos._get_video_tags(bvid)
        comments = await self.bili_comments._get_hot_comments(oid)
        if tags:
            ctx += f"\n标签：{'、'.join(tags[:10])}"
        if comments:
            ctx += "\n热门评论：" + " / ".join(comments[:3])
        return ctx, cache_entry
