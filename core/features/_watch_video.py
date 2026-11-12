"""看一个视频的完整流程，主动看片和特别关注共用：看 → 记看过 → 评价 → 互动 → 记录。

两边的差别都写在 ``WatchPolicy``。
"""
from __future__ import annotations

from datetime import datetime

from astrbot.api import logger

from ..infra.host_view import HostView


class WatchVideo(HostView):
    HOST_API = frozenset({"memory_docs", "activity_logs", "_owner_last_recommended_at", "_owner_recommended_bvids", "bili_auth", "bili_comments", "bili_users", "bili_videos", "dynamic_post", "json_store", "memory_writer", "outward_actions", "owner_recommend", "processed_keys", "seen_videos", "settings", "video_evaluate", "video_watch"})

    async def _reserve_proactive_watch(self, bvid, source="proactive"):
        return await self.outward_actions._execute_proactive_action(
            f"proactive_watch:{bvid}",
            "proactive_watch",
            bvid,
            lambda: True,
            metadata={"source": source, "reservation_only": True},
        )

    def _proactive_score_threshold(self, key, default):
        """Read an administrator-controlled action threshold in the 0-10 range."""
        try:
            return max(0, min(10, int(self.settings.get(key))))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _today_proactive_comment_count(watch_log, proactive_log=None):
        today = datetime.now().strftime("%Y-%m-%d")
        actions = set()
        for item in proactive_log or []:
            if not isinstance(item, dict):
                continue
            time_text = str(item.get("time") or "")
            if not time_text.startswith(today):
                continue
            identity = str(item.get("bvid") or item.get("title") or item.get("type") or "unknown")
            actions.add(f"{time_text}|{identity}")
        for item in watch_log or []:
            if not isinstance(item, dict) or "💬评论" not in (item.get("actions") or []):
                continue
            time_text = str(item.get("time") or "")
            if not time_text.startswith(today):
                continue
            identity = str(item.get("bvid") or item.get("title") or "unknown")
            actions.add(f"{time_text}|{identity}")
        return len(actions)

    def _proactive_per_video_comment(self):
        return max(0, min(1, int(self.settings.get("PROACTIVE_COMMENT_COUNT") or 0)))

    async def _watch_one_video(self, watch_round, video):
        """主流程和特别关注共用。返回 True 表示完整看完，需要在下一个视频前等待。"""
        bvid = video["bvid"]
        oid = video.get("oid") or await self.bili_videos._get_video_oid(bvid) or 0
        vi = await self.bili_videos._get_video_info(oid) if oid else None
        analysis_info = {
            **video,
            **({
                "bvid": vi.get("bvid", bvid), "title": vi.get("title", video.get("title", "")),
                "desc": vi.get("desc", video.get("desc", "")), "up_name": vi.get("owner_name", video.get("up_name", "")),
                "up_mid": vi.get("owner_mid", video.get("up_mid", "")), "tname": vi.get("tname", video.get("tname", "")),
                "duration": vi.get("duration", 0), "pic": vi.get("pic", video.get("pic", "")),
                "cid": vi.get("cid", 0),
            } if vi else {"bvid": bvid}),
        }
        path = []
        video_description = await self.video_watch._analyze_video_with_vision(analysis_info)
        path.append(f"分析:{analysis_info.get('analysis_basis', 'legacy')}")
        logger.info(f"[BiliBot] {watch_round.icon} 分析：{video_description[:60]}...")
        # 配图库：顺手采集评论区图片；失败只记 debug，不影响看片主流程。
        try:
            from ..server_ext import gallery_workflow
            await gallery_workflow.collect_video(self._host, oid, bvid)
        except Exception as exc:
            logger.debug(f"[BiliBot] 配图采集跳过: {exc}")
        # 分析完成即算“看过”；即使后续评价或互动失败，也不能在旧流水
        # 超过 200 条后被再次当成新视频下载分析。
        if analysis_info.get("analysis_basis") != "metadata":
            await self.seen_videos.mark(bvid, analysis_info, source=watch_round.seen_source(video))
        evaluation = await self.video_evaluate._evaluate_video(analysis_info, video_description)
        if not evaluation:
            logger.warning(f"[BiliBot] {watch_round.warn_prefix}评价失败，跳过互动")
            self._log_failed_watch(watch_round, video, analysis_info)
            watch_round.watched_bvids.add(bvid)
            watch_round.watch_count += 1
            path.append("评价失败")
            self._log_watch_path(watch_round, bvid, path)
            return False
        verdict = self._read_watch_evaluation(evaluation)
        path.append(f"评分{verdict['score']}")
        logger.info(f"[BiliBot] ⭐ 评分：{verdict['score']}/10 | 心情：{verdict['mood']} | 短评：{verdict['comment']}")
        if evaluation.get("interactions_error"):
            logger.warning(f"[BiliBot] 互动选择格式不对（{evaluation['interactions_error']}），这次不互动")
            path.append("互动选择作废")
        actions = []
        interaction_failed = False
        if oid:
            interaction_failed = await self._interact_with_video(
                watch_round, video, oid, analysis_info, video_description, evaluation, verdict, actions
            )
        if watch_round.policy.follows_up and not interaction_failed and evaluation.get("want_follow"):
            await self._maybe_follow_video_up(video, verdict["score"], actions)
        await self._log_watched_video(watch_round, video, analysis_info, video_description, evaluation, verdict, actions)
        watch_round.watched_bvids.add(bvid)
        watch_round.watch_count += 1
        action_str = " ".join(actions) if actions else "（默默看完）"
        logger.info(f"[BiliBot] {watch_round.summary_icon} 互动：{action_str}")
        path.append("跳过互动:Cookie失效" if interaction_failed else f"互动:{action_str}")
        self._log_watch_path(watch_round, bvid, path)
        return True

    @staticmethod
    def _log_watch_path(watch_round, bvid, path):
        logger.info(f"[BiliBot] 🧭 {watch_round.policy.name} {bvid}：{' → '.join(path)}")

    def _log_failed_watch(self, watch_round, video, analysis_info):
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"), "bvid": video["bvid"],
            "title": video.get("title", ""), "up_name": video.get("up_name", ""),
            "score": 0, "mood": "未知", "comment": "评价失败", "review": "",
            "actions": [], "pic": video.get("pic", ""), "tname": analysis_info.get("tname", ""),
            "source": watch_round.log_source(video),
        }
        if watch_round.policy.records_discovery:
            entry.update(source_detail=video.get("_source_detail", ""), manual=watch_round.is_manual)
        entry.update(
            evaluation_status="failed",
            diagnostic=analysis_info.get("_evaluation_diagnostic", {}),
            content_analyzed=analysis_info.get("analysis_basis") != "metadata",
            analysis_basis=analysis_info.get("analysis_basis", "legacy"),
        )
        if watch_round.policy.records_discovery:
            entry["discovery_mode"] = "explore" if video.get("_exploration") else "interest"
        watch_round.watch_log = self.activity_logs.watch.append(entry)

    @staticmethod
    def _read_watch_evaluation(evaluation):
        try:
            score = round(max(1.0, min(10.0, float(evaluation.get("score", 5)))), 1)
        except (TypeError, ValueError):
            score = 5.0
        return {
            "score": score,
            "score_reason": str(evaluation.get("score_reason") or ""),
            "preference_signals": list(evaluation.get("preference_signals") or []),
            "search_keywords": list(evaluation.get("search_keywords") or []),
            "comment": evaluation.get("comment", ""),
            "mood": evaluation.get("mood", "平静"),
            "review": evaluation.get("review", ""),
            "impression": str(evaluation.get("impression") or ""),
        }

    async def _interact_with_video(self, watch_round, video, oid, analysis_info, video_description, evaluation, verdict, actions):
        """点赞、投币、收藏、评论，主流程还会推荐给主人。返回 True 表示 Cookie 失效或疑似风控，后续互动全部跳过。

        做哪些由评价里模型自己选的 ``interactions`` 定；开关、管理员最低分、每天评论上限照旧是硬限制。
        """
        bvid = video["bvid"]
        score = verdict["score"]
        chosen = set(evaluation.get("interactions") or ())
        key = watch_round.action_key_prefix
        # 交互前快速校验 Cookie
        cookie_ok, _ = await self.bili_auth.check_cookie()
        if not cookie_ok:
            logger.warning("[BiliBot] ⚠️ Cookie 已失效，跳过本轮所有互动操作")
            return True
        if "like" in chosen and score >= self._proactive_score_threshold("PROACTIVE_LIKE_MIN_SCORE", 0) and self.settings.get("PROACTIVE_LIKE"):
            if (
                await self.outward_actions._execute_proactive_action(
                    f"{key}_like:{bvid}",
                    "like",
                    bvid,
                    lambda: self.bili_videos._like_video(oid),
                )
            ).success:
                actions.append("👍点赞")
                logger.info("[BiliBot] 👍 点赞成功")
            else:
                # 点赞是最轻量的操作，如果连这个都失败大概率是风控
                return True
        if "coin" in chosen and score >= self._proactive_score_threshold("PROACTIVE_COIN_MIN_SCORE", 8) and self.settings.get("PROACTIVE_COIN"):
            if (
                await self.outward_actions._execute_proactive_action(
                    f"{key}_coin:{bvid}",
                    "coin",
                    bvid,
                    lambda: self.bili_videos._coin_video(oid),
                )
            ).success:
                actions.append("🪙投币")
                logger.info("[BiliBot] 🪙 投币成功")
        if "favorite" in chosen and score >= self._proactive_score_threshold("PROACTIVE_FAV_MIN_SCORE", 8) and self.settings.get("PROACTIVE_FAV"):
            if (
                await self.outward_actions._execute_proactive_action(
                    f"{key}_favorite:{bvid}",
                    "favorite",
                    bvid,
                    lambda: self.bili_videos._fav_video(oid),
                )
            ).success:
                actions.append("⭐收藏")
                logger.info("[BiliBot] ⭐ 收藏成功")
        if "comment" in chosen and watch_round.per_video_comment and score >= self._proactive_score_threshold("PROACTIVE_COMMENT_MIN_SCORE", 0) and watch_round.comment_count < watch_round.daily_comment_limit and self.settings.get("PROACTIVE_COMMENT"):
            await self._send_proactive_video_comment(watch_round, video, oid, analysis_info, video_description, actions)
        if (
            watch_round.policy.recommends_to_owner
            and bvid not in getattr(self, "_owner_recommended_bvids", set())
            and self.owner_recommend._can_recommend_owner(evaluation, score, watch_round.owner_recommend_count)
        ):
            await self.owner_recommend._recommend_video_to_owner(watch_round, video, oid, video_description, evaluation, verdict["review"], actions)
        return False

    async def _send_proactive_video_comment(self, watch_round, video, oid, analysis_info, video_description, actions):
        bvid = video["bvid"]
        proactive_comment = await self.video_evaluate._generate_proactive_comment(analysis_info, video_description)
        sync = self.dynamic_post.comment_syncs()
        if not proactive_comment:
            return
        sent = await self.outward_actions._execute_proactive_action(
            f"{watch_round.action_key_prefix}_comment:{bvid}",
            "proactive_comment",
            bvid,
            lambda: self.bili_comments._send_comment(oid, proactive_comment, sync_to_dynamic=sync),
            metadata={"text": proactive_comment[:200], "title": video.get("title", "")[:80]},
        )
        if not sent.success:
            return
        if sync:
            self.dynamic_post.comment_synced(bvid, proactive_comment, video.get("title", ""))
        actions.append("💬评论")
        watch_round.comment_count += 1
        logger.info(f"[BiliBot] 💬 评论成功：{proactive_comment}")
        self.processed_keys.commented_video.add([bvid])
        watch_round.commented_videos.add(bvid)
        watch_round.commented_videos |= self.processed_keys.commented_video.all()
        entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "bvid": bvid, "title": video.get("title", ""), "comment": proactive_comment}
        if watch_round.policy.tags_comment_log:
            entry["type"] = watch_round.policy.name
        own_rpid = getattr(getattr(sent, "value", None), "rpid", 0)
        if own_rpid:
            entry["own_rpid"] = str(own_rpid)
        self.activity_logs.proactive.append(entry)

    async def _maybe_follow_video_up(self, video, score, actions):
        if score < self._proactive_score_threshold("PROACTIVE_FOLLOW_MIN_SCORE", 0) or not self.settings.get("PROACTIVE_FOLLOW"):
            return
        if str(video.get("up_mid", "")) == str(self.settings.get("OWNER_MID")):
            return
        if (
            await self.outward_actions._execute_proactive_action(
                f"proactive_follow:{video['up_mid']}",
                "follow",
                video["up_mid"],
                lambda: self.bili_users._follow_user(video["up_mid"]),
            )
        ).success:
            actions.append("➕关注")
            logger.info(f"[BiliBot] ➕ 关注了 {video.get('up_name', '')}")

    async def _log_watched_video(self, watch_round, video, analysis_info, video_description, evaluation, verdict, actions):
        """写观看流水、长期记忆、外部视频记忆；主流程还会登记想发的动态和共用视频缓存。"""
        bvid = video["bvid"]
        score = verdict["score"]
        score_reason = verdict["score_reason"]
        preference_signals = verdict["preference_signals"]
        search_keywords = verdict["search_keywords"]
        mood = verdict["mood"]
        review = verdict["review"]
        tname = evaluation.get("partition") or analysis_info.get("tname", "")
        log_entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "bvid": bvid, "title": video.get("title", ""), "up_name": video.get("up_name", ""), "up_mid": str(video.get("up_mid", "")), "score": score, "score_reason": score_reason, "mood": mood, "comment": verdict["comment"], "review": review, "preference_signals": preference_signals, "search_keywords": search_keywords, "actions": actions, "pic": video.get("pic", ""), "tname": tname, "source": watch_round.log_source(video)}
        want_dynamic = bool(evaluation.get("want_dynamic")) and bool(evaluation.get("dynamic_angle"))
        if want_dynamic and watch_round.policy.collects_dynamic_ideas:
            kind = evaluation.get("dynamic_kind") or "分享"
            logger.info(f"[BiliBot] 📝 看完想发动态（{kind}）：{evaluation.get('dynamic_angle', '')}")
            idea_id = self.dynamic_post.collect_idea(
                key=bvid, kind=kind, source=watch_round.policy.name,
                title=video.get("title", ""), up_name=video.get("up_name", ""), pic=video.get("pic", ""),
                score=score, review=review, angle=evaluation.get("dynamic_angle", ""),
                summary=self.video_watch._clip_media_text(video_description, 360),
            )
            if idea_id:
                watch_round.dynamic_candidates.append(idea_id)
        if watch_round.policy.records_discovery:
            log_entry.update(source_detail=video.get("_source_detail", ""), manual=watch_round.is_manual)
            log_entry.update(
                want_dynamic=want_dynamic,
                dynamic_angle=evaluation.get("dynamic_angle", "") if want_dynamic else "",
                discovery_mode="explore" if video.get("_exploration") else "interest",
                recommend_reason=evaluation.get("recommend_reason", ""),
                recommended_at=getattr(self, "_owner_last_recommended_at", "") if any(self.owner_recommend._is_owner_recommend_action(action) for action in actions) else "",
                content_summary=self.video_watch._clip_media_text(video_description, 360),
                analysis_basis=analysis_info.get("analysis_basis", "legacy"),
            )
        watch_round.watch_log = self.activity_logs.watch.append(log_entry)

        memory_text = (
            f"[{log_entry['time']}] {watch_round.memory_subject}看了视频《{video.get('title', '')}》"
            f"(UP主:{video.get('up_name', '')}) "
            f"评分:{score}/10 心情:{mood} "
            f"理由:{score_reason[:80]} 感想:{review[:80]} "
            f"内容:{video_description[:120]}"
        )
        if preference_signals:
            memory_text += " | 兴趣信号:" + "、".join(
                f"{item.get('polarity')}:{item.get('value')}"
                for item in preference_signals[:5]
            )
        if search_keywords:
            memory_text += f" | 可继续搜索:{'、'.join(search_keywords[:5])}"
        channels = []
        if "✉️私信推荐给主人" in actions:
            channels.append("B站私信")
        if "💬QQ推荐给主人" in actions:
            channels.append("QQ私信")
        if any(action in {"📢推荐给主人", "📢评论区推荐给主人"} for action in actions):
            channels.append("视频评论区@对方")
        if channels:
            on = self.settings.get("OWNER_NAME") or "主人"
            memory_text += f" | 觉得不错，已通过{'、'.join(channels)}分享给{on}"
        if self.settings.get("ENABLE_VIDEO_LONG_TERM_MEMORY"):
            await self.memory_writer._save_self_memory_record(f"{watch_round.action_key_prefix}_watch", self.video_watch._clip_media_text(memory_text, 520), memory_type="video", extra={"bvid": bvid, "owner_mid": str(video.get("up_mid", "")), "owner_name": video.get("up_name", ""), "video_title": video.get("title", ""), "tname": tname, "score": score, "score_reason": score_reason, "mood": mood, "review": review, "impression": verdict.get("impression", ""), "preference_signals": preference_signals, "search_keywords": search_keywords})
        external_memory = watch_round.external_memory
        if bvid not in external_memory:
            external_memory[bvid] = {"title": video.get("title", ""), "up_name": video.get("up_name", ""), "up_mid": str(video.get("up_mid", "")), "description": self.video_watch._clip_media_text(video_description, 220), "score": score, "score_reason": score_reason, "mood": mood, "review": self.video_watch._clip_media_text(review, 120), "preference_signals": preference_signals, "search_keywords": search_keywords, "watched_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "comments": []}
            self.memory_docs.external_memory.write(external_memory)
        if not watch_round.policy.writes_shared_video_cache:
            return
        # 写入与评论回复共用的视频分析缓存，避免同一视频被重复下载分析
        try:
            vc = self.memory_docs.video_memory.read({})
            vc[bvid] = {
                "bvid": bvid,
                "title": analysis_info.get("title", video.get("title", "")),
                "desc": (analysis_info.get("desc", "") or "")[:200],
                "owner_name": analysis_info.get("up_name", video.get("up_name", "")),
                "owner_mid": str(analysis_info.get("up_mid", video.get("up_mid", ""))),
                "tname": analysis_info.get("tname", ""),
                "analysis": video_description,
                "analysis_basis": analysis_info.get("analysis_basis", "legacy"),
                "content_analyzed": analysis_info.get("analysis_basis") != "metadata",
                "summary": self.video_watch._clip_media_text(video_description, 220),
                "score": score,
                "score_reason": score_reason,
                "mood": mood,
                "review": self.video_watch._clip_media_text(review, 120),
                "preference_signals": preference_signals,
                "search_keywords": search_keywords,
                "time": log_entry["time"],
            }
            self.memory_docs.video_memory.write(vc)
        except Exception as e:
            logger.debug(f"[BiliBot] 写入视频缓存失败: {e}")
