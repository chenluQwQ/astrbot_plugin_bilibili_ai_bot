"""主动看片：定好额度 → 找候选 → 逐个看 → 把想发动态的交给发动态。

选片在 ``domain/video_discovery.py``，看一个视频在 ``_watch_video.py``。
"""
from __future__ import annotations

import asyncio
import random
import re
import traceback
from datetime import datetime, timezone

from astrbot.api import logger

from ..infra.host_view import HostView
from ..lifecycle.handoffs import WantsToPostDynamic
from ._watch_round import PROACTIVE_WATCH, _WatchRound


class ProactiveBrowse(HostView):
    HOST_API = frozenset({"memory_docs", "activity_logs", "activities", "bili_http", "bili_users", "environment", "json_store", "memo_watch", "models", "now_doing", "owner_recommend", "processed_keys", "qq_chat", "schedule_rules", "seen_videos", "settings", "taste", "up_skip", "video_discovery", "video_pools", "watch_video"})

    async def _should_trigger_proactive_from_text(self, text):
        text = (text or "").strip()
        if not text or text.startswith("/"):
            return False
        direct_patterns = [
            r'去.*(随机|随便).*(看|刷).*(视频|B站)',
            r'(随机|随便).*(看|刷).*(视频|B站)',
            r'帮我.*(看|刷).*(视频|B站)',
            r'你去.*(看|刷).*(视频|B站)',
        ]
        lowered = text.lower()
        if any(re.search(p, text, re.IGNORECASE) for p in direct_patterns):
            return True
        if not any(k in lowered for k in ["b站", "视频", "刷", "看看", "bilibili", "小破站"]):
            return False
        prompt = (
            "判断下面这句话是否是在要求你现在去随机看一些B站视频，并执行一次主动看视频行为。"
            "只回答 yes 或 no。\n\n"
            f"用户话语：{text}"
        )
        result = await getattr(self.models, "_generation_llm_call", self.models._llm_call)(prompt, max_tokens=5)
        return (result or "").strip().lower().startswith("y")

    async def _maybe_trigger_proactive_from_llm(self, event, req):
        if not self.settings.get("ENABLE_PROACTIVE"):
            return
        if not self.bili_http._has_cookie():
            return
        if self.activities._activity_busy("proactive"):
            return
        msg = event.message_str or ""
        if not await self._should_trigger_proactive_from_text(msg):
            return
        # LLM 判断期间可能有另一条消息也通过了上面的检查，这里才真正占住
        if self.activities._run_activity_now("proactive", max_watch=1) is None:
            return
        self.activity_logs.trigger.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "manual_proactive_request", "scheduled": "llm_request", "status": "triggered", "content": msg[:100]})
        sender_name = event.get_sender_name() or "用户"
        self.qq_chat._inject_context_block_before_user(req, f"【系统提示】{sender_name}叫你去看B站视频，你已在后台开始执行一次看视频流程。回复时让对方知道你去看了，看完后相关记忆会存入你的评论区记忆中，之后可以回忆起来。")

    async def _run_proactive(self, max_watch=None, max_comment=None, focus=None):
        """``focus``：每日计划给这一段定的重点（``domain/day_focus.py``），没有就照常混着挑。"""
        self.now_doing.report("proactive", "正在挑选视频")
        try:
            await self._run_proactive_inner(max_watch=max_watch, max_comment=max_comment, focus=focus)
        except asyncio.CancelledError:
            logger.info("[BiliBot] 主动看视频任务被取消")
            raise
        except Exception as e:
            logger.error(f"[BiliBot] 主动看视频任务异常退出: {e}\n{traceback.format_exc()}")
        finally:
            self.now_doing.finish("proactive")

    async def _run_proactive_inner(self, max_watch=None, max_comment=None, focus=None):
        env = self.environment._get_environment_status()
        if not env["features"]["proactive_video_media"]:
            logger.warning("[BiliBot] 当前环境不满足视频媒体分析条件，将回退为纯文本视频分析。")
        is_manual = max_watch is not None
        watch_log = self.activity_logs.watch.recent()
        recommend_ledger, owner_recommend_count = self.owner_recommend._load_owner_recommendation_state(watch_log)
        daily_watch, daily_comment_limit = self._proactive_round_budget(watch_log, max_watch, max_comment)
        if daily_watch is None:
            return
        external_memory = self.memory_docs.external_memory.read({})
        commented_videos = self.processed_keys.commented_video.all()
        watch_round = _WatchRound(
            policy=PROACTIVE_WATCH,
            watch_log=watch_log,
            watched_bvids=await self.seen_videos.bvids(),
            commented_videos=commented_videos,
            external_memory=external_memory,
            daily_comment_limit=daily_comment_limit,
            per_video_comment=self.watch_video._proactive_per_video_comment(),
            is_manual=is_manual,
            recommend_ledger=recommend_ledger,
            owner_recommend_count=owner_recommend_count,
        )
        unique = await self._collect_proactive_candidates(watch_log, watch_round.watched_bvids, daily_watch, focus)
        # 历史口味仅用于生成搜索词和标题筛选，不再单独占第四种来源。
        taste_tids = self.taste._get_taste_tids()
        if not taste_tids:
            taste_tids = list(self.video_discovery.FALLBACK_TIDS)
            logger.info("[BiliBot] 🎯 口味数据不足，使用兜底分区")
        logger.info(f"[BiliBot] 📋 共找到 {len(unique)} 个视频")
        watch_round.comment_count = self.watch_video._today_proactive_comment_count(
            watch_log, self.activity_logs.proactive.recent()
        )
        comments_before_run = watch_round.comment_count
        prefilter_rejected = 0
        prefilter_max_rejects = max(0, int(self.settings.get("PROACTIVE_LLM_PREFILTER_MAX_REJECTS")))
        for video in unique:
            if watch_round.watch_count >= daily_watch:
                break
            if str(video.get("up_mid", "")) == self.settings.get("DEDE_USER_ID"):
                continue
            if video.get("_source") == "memo":  # 备忘是自己挑过的，不再过标题筛选
                allow_watch, prefilter_reason = True, ""
            else:
                allow_watch, prefilter_reason = await self.video_discovery._should_watch_video_before_download(video, taste_tids, prefilter_rejected, prefilter_max_rejects)
            if not allow_watch:
                prefilter_rejected += 1
                logger.info(f"[BiliBot] 🧭 标题筛选跳过({prefilter_rejected}/{prefilter_max_rejects})：{video['title']} | {prefilter_reason}")
                continue
            watch_reservation = await self.watch_video._reserve_proactive_watch(
                video["bvid"], video.get("_source", "proactive")
            )
            if not watch_reservation.success:
                continue
            self.now_doing.report("proactive", "正在分析视频", title=video.get("title", ""), up_name=video.get("up_name", ""))
            source_note = {"follow": "关注", "search": "搜索", "pool": "视频池", "memo": "备忘"}.get(video.get("_source", ""), "候选")
            logger.info(f"[BiliBot] 🎬 [{watch_round.watch_count + 1}/{daily_watch}] [{source_note}] {video['title']} by {video.get('up_name', '')}")
            if not await self.watch_video._watch_one_video(watch_round, video):
                continue
            if video.get("_memo_id") is not None:
                self.memo_watch.finish(video["_memo_id"], f"看了《{str(video.get('title', ''))[:40]}》")
            wait = random.randint(30, 120)
            logger.info(f"[BiliBot] ⏳ 等待 {wait} 秒...")
            await asyncio.sleep(wait)
        logger.info(
            f"[BiliBot] 🎉 刷B站完成！看了 {watch_round.watch_count} 个视频，"
            f"本轮评论了 {max(0, watch_round.comment_count - comments_before_run)} 条"
        )
        await self._post_video_dynamic(watch_round.dynamic_candidates)

    def _proactive_round_budget(self, watch_log, max_watch=None, max_comment=None):
        """本轮最多看几个、全天最多评论几条；今天已到上限时看视频数量返回 None。"""
        daily_watch = max_watch if max_watch is not None else self.settings.get("PROACTIVE_VIDEO_COUNT")
        daily_comment_limit = max_comment if max_comment is not None else self.settings.get("PROACTIVE_COMMENT_DAILY_LIMIT")
        daily_comment_limit = max(0, int(daily_comment_limit or 0))
        today_str = datetime.now().strftime("%Y-%m-%d")
        # 日限检查：所有来源（含手动/LLM触发）均计入总量
        today_watched = [l for l in watch_log if l.get("time", "").startswith(today_str)]
        daily_limit = max(0, int(self.settings.get("PROACTIVE_DAILY_LIMIT") or 0))
        autonomous_limit = self.schedule_rules._autonomous_limit_max("proactive")
        if autonomous_limit:
            daily_limit = min(daily_limit, autonomous_limit) if daily_limit else autonomous_limit
        if daily_limit > 0 and len(today_watched) >= daily_limit:
            logger.info(f"[BiliBot] 今天已看 {len(today_watched)} 个视频（上限{daily_limit}），不再刷")
            return None, daily_comment_limit
        # 本轮实际可看数量 = min(请求量, 剩余配额)
        if daily_limit > 0:
            remaining = daily_limit - len(today_watched)
            daily_watch = min(daily_watch, remaining)
        logger.info(
            f"[BiliBot] 🎯 主动刷B站 | 本轮最多看 {daily_watch} 个视频 | "
            f"主动评论全天上限 {daily_comment_limit} 条"
        )
        return daily_watch, daily_comment_limit

    async def _collect_proactive_candidates(self, watch_log, watched_bvids, daily_watch, focus=None):
        """按关注、搜索、视频池三种来源取候选，并按当天配额合并排序。

        ``focus`` 是关注 / 话题 / 池子时，这一轮的配额整个给那个来源，不够了合并时用别的补；
        是备忘时，备忘的视频排在最前面，剩下的名额照常挑。
        """
        focus = focus if isinstance(focus, dict) else {}
        kind, detail = focus.get("focus"), focus.get("detail")
        memo_candidates = []
        if kind == "memo":
            memo_candidates = await self.memo_watch.video_candidates(detail, watched_bvids, daily_watch)
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_watched = [l for l in watch_log if l.get("time", "").startswith(today_str)]
        # 用 UTC 年初：naive datetime.timestamp() 会按本机时区折算，导致同一份快照在不同时区的机器上产生不同数值。
        min_pubdate_hot = int(datetime(datetime.now().year, 1, 1, tzinfo=timezone.utc).timestamp())
        prefilter_extra = (
            max(0, int(self.settings.get("PROACTIVE_LLM_PREFILTER_MAX_REJECTS")))
            if self.settings.get("ENABLE_PROACTIVE_LLM_PREFILTER")
            else 0
        )
        candidate_target = daily_watch + max(3, prefilter_extra)
        today_source_counts = {"follow": 0, "search": 0, "pool": 0}
        for entry in today_watched:
            source = self.video_discovery._proactive_log_source(entry.get("source"))
            if source:
                today_source_counts[source] += 1
        source_quotas = self.video_discovery._proactive_batch_source_quotas(
            daily_watch,
            today_source_counts,
        )
        focus_source = {"follow": "follow", "topic": "search", "pool": "pool"}.get(kind)
        if focus_source:
            source_quotas = {source: daily_watch if source == focus_source else 0 for source in source_quotas}
            logger.info(f"[BiliBot] 🧭 这一段的重点：{ {'follow': '关注的UP', 'topic': '话题', 'pool': '视频池'}[kind] }"
                        f"{'：' + str(detail) if detail else ''}")
        logger.info(
            "[BiliBot] 🧭 今日已看来源=%s/%s/%s | 本轮配额：关注=%s 搜索=%s 视频池=%s",
            today_source_counts["follow"],
            today_source_counts["search"],
            today_source_counts["pool"],
            source_quotas["follow"],
            source_quotas["search"],
            source_quotas["pool"],
        )

        follow_candidates = await self._collect_follow_candidates(watched_bvids, candidate_target)
        skipped_ups = self._skipped_ups(watch_log)

        # 视频池候选：保留现有热门/推荐/排行/最新等地址池配置。
        pool_videos = await self.video_pools._get_pool_videos(min_pubdate_hot, pools=[detail] if kind == "pool" else None)
        pool_candidates = [
            self.video_discovery._tag_video_source(video, "pool")
            for video in pool_videos
            if video.get("bvid") not in watched_bvids and str(video.get("up_mid") or "") not in skipped_ups
        ]
        random.shuffle(pool_candidates)

        # 搜索候选：轮到搜索或其他来源不足时，结合观看证据与探索方向选词。
        need_search_candidates = (
            source_quotas["search"] > 0
            or len(follow_candidates) < source_quotas["follow"]
            or len(pool_candidates) < source_quotas["pool"]
        )
        search_candidates = []
        if need_search_candidates:
            search_keywords = ([detail] if kind == "topic" and detail
                               else await self.video_discovery._decide_proactive_search_queries(watch_log))
            raw_search_videos = await self.video_discovery._get_proactive_search_videos(
                search_keywords,
                candidate_target,
                watched_bvids=watched_bvids,
            )
            search_candidates = [
                self.video_discovery._tag_video_source(video, "search", video.get("_search_keyword", ""))
                for video in raw_search_videos
                if video.get("bvid") not in watched_bvids and str(video.get("up_mid") or "") not in skipped_ups
            ]

        candidates = {
            "follow": follow_candidates,
            "search": search_candidates,
            "pool": pool_candidates,
        }
        unique = self.video_discovery._merge_proactive_source_candidates(
            candidates,
            source_quotas,
            candidate_target,
        )
        selected_counts = {
            source: sum(1 for video in unique[:daily_watch] if video.get("_source") == source)
            for source in ("follow", "search", "pool")
        }
        logger.info(
            "[BiliBot] 📊 来源候选：关注=%s 搜索=%s 视频池=%s | 前%s项分布=%s/%s/%s | 总候选=%s",
            len(follow_candidates),
            len(search_candidates),
            len(pool_candidates),
            daily_watch,
            selected_counts["follow"],
            selected_counts["search"],
            selected_counts["pool"],
            len(unique),
        )
        if memo_candidates:
            memo_bvids = {video["bvid"] for video in memo_candidates}
            logger.info(f"[BiliBot] 📝 这一段先看备忘 #{detail}：{len(memo_candidates)} 个视频")
            unique = memo_candidates + [video for video in unique if video.get("bvid") not in memo_bvids]
        return unique

    def _skipped_ups(self, watch_log):
        """低分 UP 跳过名单（``domain/up_skip.py``）：先按观看记录更新，再返回名单上的 UID。特别关注、主人不会进名单。"""
        exempt = [str(uid).strip() for uid in self.settings.get("PROACTIVE_FOLLOW_UIDS") or []]
        exempt.append(str(self.settings.get("OWNER_MID") or ""))
        try:
            for item in self.up_skip.review(watch_log, exempt=[uid for uid in exempt if uid]):
                logger.info(f"[BiliBot] 🙈 {item['name'] or item['uid']} 最近刷到 {item['count']} 个、平均 {item['average']} 分，"
                            "这阵子搜索和视频池里先不看他的视频")
            return set(self.up_skip.entries())
        except Exception as exc:
            logger.warning(f"[BiliBot] 跳过名单出错（这次不跳过）: {exc}")
            return set()

    async def _collect_follow_candidates(self, watched_bvids, candidate_target):
        """特别关注优先，其后从普通关注中找今天更新的视频。"""
        follow_candidates = []
        follow_seen = set()
        special_mids = self.settings.get("PROACTIVE_FOLLOW_UIDS")
        for mid in special_mids:
            video = await self.bili_users._get_up_latest_video(mid)
            if video and video["bvid"] not in watched_bvids and video["bvid"] not in follow_seen:
                follow_seen.add(video["bvid"])
                follow_candidates.append(self.video_discovery._tag_video_source(video, "follow", "special_follow"))
                logger.info(f"[BiliBot] ⭐ 特别关心：{video['up_name']} - {video['title']}")
            if len(follow_candidates) >= candidate_target:
                break
        following_mids = await self.bili_users.get_followings()
        logger.info(f"[BiliBot] 📡 关注列表：{len(following_mids)} 个UP主")
        following_mids = [mid for mid in following_mids if str(mid) not in {str(v) for v in special_mids}]
        random.shuffle(following_mids)
        today = datetime.now().date()
        for mid in following_mids:
            if len(follow_candidates) >= candidate_target:
                break
            video = await self.bili_users._get_up_latest_video(mid)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            if video and video["bvid"] not in watched_bvids and video["bvid"] not in follow_seen:
                pubdate = video.get("pubdate", 0)
                if isinstance(pubdate, str):
                    try:
                        pubdate = int(pubdate)
                    except Exception:
                        pubdate = 0
                if pubdate and datetime.fromtimestamp(pubdate).date() == today:
                    follow_seen.add(video["bvid"])
                    follow_candidates.append(self.video_discovery._tag_video_source(video, "follow", "following"))
                    logger.info(f"[BiliBot] 🔔 今日更新：{video['up_name']} - {video['title']}")
        return follow_candidates

    async def _post_video_dynamic(self, candidates):
        """这一轮有想发动态的，就叫发动态那边从池子里挑一条发（一轮最多一条）。

        挑哪条、开关、每日上限和重复都由动态那边定；这一轮的编号让它知道哪些是刚看完的。
        """
        if not candidates:
            return
        await self.activities._handoffs().send(WantsToPostDynamic(fresh=tuple(candidates)))
