"""追番：选一部番、逐集看、评价和互动，每天第一个番剧时刻先查追番更新。

番剧看不了画面（PGC 下载不了），只用封面、字幕和热评，所以不走 ``_watch_one_video``。
看过哪些集记在 ``BangumiMemory``，这里只决定看什么、看完做什么。
"""
from __future__ import annotations

import asyncio
import random
import traceback
from datetime import datetime

from astrbot.api import logger

from ..content_protocol import ContentProtocolError, parse_bangumi_evaluation
from ..domain.bangumi_memory import find_missing_episodes, sort_episodes
from ..domain.bangumi_series import season_to_backfill
from ..domain.memos import MemoRejected
from ..infra.host_view import HostView
from ..video_diagnostics import exception_detail, get_call_diagnostic, safe_detail


class Bangumi(HostView):
    HOST_API = frozenset({
        "activity_logs", "activities", "bangumi_memory", "bili_auth", "bili_bangumi",
        "bili_comments", "bili_http", "bili_videos", "config", "dynamic_post", "json_store", "memory_writer",
        "memo_tool", "memo_watch", "models", "now_doing", "outward_actions", "settings", "video_watch", "watch_video",
    })

    async def _progress_context(self, season_id, season_title):
        """获取番剧上下文（含总结）。按集号排序，标注缺集，>5集自动总结。"""
        mem = self.bangumi_memory.all()
        record = mem.get(str(season_id))
        if not record or not record.get("episodes"):
            return ""
        eps = sort_episodes(record["episodes"])
        missing = find_missing_episodes(eps)
        missing_str = ""
        if missing:
            missing_str = f"\n【未看的集】第{'、'.join(str(m) for m in missing)}话\n"

        if len(eps) <= 5:
            lines = []
            for e in eps:
                lines.append(f"[第{e.get('ep_index', '?')}话] {e.get('title', '')} 评分:{e.get('score', '?')}/10 心情:{e.get('mood', '?')} 感想:{e.get('review', '')}")
            return f"【已看 {len(eps)} 集】\n" + "\n".join(lines) + missing_str
        # >5集：总结 + 最近3集
        old_eps = eps[:-3]
        recent_eps = eps[-3:]
        summary = await self._summarize_episodes(season_title, old_eps)
        recent_lines = [f"[第{e.get('ep_index', '?')}话] {e.get('title', '')} 评分:{e.get('score', '?')}/10 心情:{e.get('mood', '?')} 感想:{e.get('review', '')}" for e in recent_eps]
        parts = []
        if summary:
            parts.append(f"【前{len(old_eps)}集总结】{summary}")
        parts.append("【最近几集】\n" + "\n".join(recent_lines))
        if missing_str:
            parts.append(missing_str)
        return "\n".join(parts)

    async def _summarize_episodes(self, season_title, episodes):
        """LLM 总结前几集（不落盘，仅当次使用）。"""
        if not episodes:
            return ""
        try:
            ep_text = "\n".join(f"第{e.get('ep_index', '?')}话: {e.get('description', '')} 感想:{e.get('review', '')}" for e in episodes)
            if len(ep_text) > 2000:
                ep_text = ep_text[:2000] + "…（已截断）"
            prompt = f"你之前追过番剧《{season_title}》，以下是你看过的各集内容和感想，请用200字以内总结前情提要（重点剧情线和关键人物）：\n\n{ep_text}\n\n直接输出总结，不加前缀。"
            result = await self.models._summary_llm_call(prompt, max_tokens=300)
            if result:
                logger.info(f"[BiliBot] 📝 番剧总结完成: 《{season_title}》前{len(episodes)}集")
            return result or ""
        except Exception as e:
            logger.error(f"[BiliBot] 番剧总结失败: {e}")
            return ""

    async def _analyze_episode(self, season_info, ep_info):
        """分析番剧单集：封面视觉+字幕+热评（跳过视频下载，番剧PGC内容无法下载）。"""
        try:
            aid = ep_info.get("aid", 0)
            cid = ep_info.get("cid", 0)
            bvid = ""
            if aid:
                try:
                    bvid = await self.bili_videos._oid_to_bvid(aid)
                except Exception:
                    pass

            # 构造 video_info 格式，复用文本分析（不走视频下载）
            ep_title = ep_info.get("long_title", "") or ep_info.get("title", "")
            video_info = {
                "bvid": bvid,
                "title": f"{season_info.get('title', '')} 第{ep_info.get('ep_index', '?')}话 {ep_title}",
                "desc": season_info.get("evaluate", "")[:300],
                "up_name": "",
                "owner_name": "",
                "tname": "番剧",
                "duration": ep_info.get("duration", 0) * 60 if ep_info.get("duration") else 0,
                "pic": season_info.get("cover", ""),
                "cid": cid,
                "oid": aid,
            }

            # 番剧用纯文本分析（封面+字幕+热评+联网搜索），跳过视频下载
            # PGC内容无法通过yt-dlp下载，走_analyze_video_media会白白失败
            analysis = await self.video_watch._analyze_video_text(video_info)

            # 保底
            if not analysis or len(analysis) < 20:
                parts = []
                if season_info.get("evaluate"):
                    parts.append(f"番剧简介：{season_info['evaluate'][:200]}")
                parts.append(f"番剧《{season_info.get('title', '?')}》第{ep_info.get('ep_index', '')}话 {ep_title}")
                analysis = "\n".join(parts) if parts else f"番剧《{season_info.get('title', '?')}》"

            return analysis
        except Exception as e:
            logger.error(f"[BiliBot] 番剧集分析失败: {e}")
            return f"番剧《{season_info.get('title', '?')}》第{ep_info.get('ep_index', '')}话"

    async def _evaluate_episode(self, season_info, ep_info, analysis, bangumi_context):
        """返回 ``(evaluation, reason)``：成功时 reason 为空，失败时 evaluation 为 None、reason 写给日志看。"""
        """LLM 评价番剧单集。"""
        sp = await self.models._get_system_prompt()
        ctx = f"\n\n【你之前看过的进度】\n{bangumi_context}" if bangumi_context else ""
        prompt = f"""你刚看完一集番剧：
- 番名：{season_info.get('title', '')}
- 类型：{season_info.get('styles', '')} | 地区：{season_info.get('areas', '')}
- B站评分：{season_info.get('score', '暂无')}
- 本集：第{ep_info.get('ep_index', '?')}话 {ep_info.get('long_title', '') or ep_info.get('title', '')}
- 内容分析：{analysis[:800]}{ctx}

以JSON格式回复你的观后感：
{{"score": 1到10的整数评分, "comment": "评论区留言（15-30字）", "mood": "看完的心情（开心/平静/无聊/感动/好笑/震撼/困惑/燃/虐 选一个）", "review": "本集感想（50字以内）", "want_continue": true或false, "want_dynamic": false, "dynamic_angle": "", "dynamic_kind": ""}}

comment要求：像追番观众会打的弹幕或评论，可以对剧情发展发表看法、吐槽角色行为、或者表达情绪。
want_continue：是否值得继续追，烂番可以果断弃。
want_dynamic：看完这集是否真有话想在自己的B站动态里说，大多数时候填false；为true时dynamic_angle写想说的具体角度（80字以内），dynamic_kind填吐槽|段子|分享之一，否则两项都留空。想发的不一定马上发，会先攒着。
评分：大部分正常番应该在5-7分，不要动不动就8分以上。1-3烂片、4-5凑合、6-7还行、8-9好看、10封神。
直接输出JSON。"""
        custom_inst = self.settings.get("CUSTOM_PROACTIVE_INSTRUCTION")
        if custom_inst:
            prompt += f"\n\n【补充提示词】{custom_inst}"
        text = None
        try:
            text = await self.models._generation_llm_call(prompt, system_prompt=sp, max_tokens=350)
            if not text:
                reason = get_call_diagnostic().get("reason") or "模型返回空内容"
                return None, safe_detail(reason, self.config)
            try:
                return parse_bangumi_evaluation(text), ""
            except ContentProtocolError as exc:
                logger.warning(f"[BiliBot] 番剧评价结构校验失败: {exc}")
                return None, f"评价格式校验没过：{exc}"
        except Exception as e:
            logger.error(f"[BiliBot] 番剧评价失败: {e} | raw={str(text)[:200]}")
            return None, f"评价出错：{exception_detail(e, self.config)}"

    async def _pick_season(self):
        """选一部番：当季新番 > 热度排行 > 随机保底。返回 season_id 或 None。"""
        mem = self.bangumi_memory.all()
        # 已经看完的番（有记录且不再追）不重复选；但追更中的可以继续
        pools = self.settings.get("BANGUMI_POOLS")
        if not pools:
            pools = ["trending"]

        candidates = []
        for pool_raw in pools:
            pool = str(pool_raw).lower().strip()
            try:
                if pool == "trending":
                    for st in [1, 4]:
                        items = await self.bili_bangumi.get_bangumi_trending(season_type=st)
                        for item in items:
                            sid = item.get("season_id", 0)
                            if sid:
                                candidates.append({"season_id": sid, "title": item.get("title", ""), "score": item.get("score", 0), "source": "trending", "priority": 1})
                elif pool == "timeline":
                    items = await self.bili_bangumi.get_bangumi_timeline(day_before=2, day_after=0)
                    for item in items:
                        sid = item.get("season_id", 0)
                        if sid and item.get("published"):
                            candidates.append({"season_id": sid, "title": item.get("title", ""), "score": 0, "source": "timeline", "priority": 3})
                else:
                    logger.warning(f"[BiliBot] 未知番剧池: {pool}")
            except Exception as e:
                logger.warning(f"[BiliBot] 番剧池 {pool} 拉取失败: {e}")

        # 保底
        if not candidates:
            logger.info("[BiliBot] 🎬 番剧候选池为空，尝试保底热门")
            try:
                items = await self.bili_bangumi.get_bangumi_trending(season_type=1)
                candidates = [{"season_id": it["season_id"], "title": it.get("title", ""), "score": it.get("score", 0), "source": "fallback", "priority": 0} for it in (items or [])]
            except Exception:
                pass
        if not candidates:
            logger.warning("[BiliBot] 🎬 无法获取任何番剧候选")
            return None

        # 去重
        seen = set()
        unique = []
        for c in candidates:
            sid = c["season_id"]
            if sid not in seen:
                seen.add(sid)
                unique.append(c)

        # 排除已看完的番（bangumi_memory 中 completed=True 的）
        completed_sids = {sid for sid, record in mem.items() if record.get("completed")}
        if completed_sids:
            before_count = len(unique)
            unique = [c for c in unique if str(c["season_id"]) not in completed_sids]
            filtered = before_count - len(unique)
            if filtered:
                logger.info(f"[BiliBot] 🎬 排除已看完的番剧 {filtered} 部")

        # 优先选有未看集数的（追更中的优先）
        prioritized = []
        fresh = []
        for c in unique:
            sid = str(c["season_id"])
            if sid in mem and mem[sid].get("episodes"):
                # 已追过，看看还有没有新集
                prioritized.append(c)
            else:
                fresh.append(c)

        # 排序：追更中 > 热度高 > 新番
        prioritized.sort(key=lambda x: x.get("priority", 0), reverse=True)
        fresh.sort(key=lambda x: (x["priority"], x.get("score", 0)), reverse=True)

        # 30% 概率追更旧番，70% 看新番（如果都有的话）
        pool_to_pick = fresh
        if prioritized and (not fresh or random.random() < 0.3):
            pool_to_pick = prioritized

        top = pool_to_pick[:min(8, len(pool_to_pick))]
        if not top:
            top = (prioritized + fresh)[:8]
        if not top:
            return None

        chosen = random.choice(top)
        logger.info(f"[BiliBot] 🎯 选番：《{chosen['title']}》(sid={chosen['season_id']}) 来源:{chosen['source']}")
        return chosen["season_id"]

    async def _watch_episode(self, season_info, ep_info, bangumi_context):
        """看一集番剧，返回 ``(score, evaluation, status, reason)``。

        ``status`` 是 ``"watched"``、``"not_admitted"``（没拿到观看额度）、``"evaluation_failed"``
        或 ``"error"``；不是 ``"watched"`` 时这一集不写记忆、不互动，也不算看过，``reason`` 是没看成的原因。
        """
        try:
            ep_index = ep_info.get("ep_index", "?")
            ep_title = ep_info.get("long_title", "") or ep_info.get("title", "")
            episode_key = str(
                ep_info.get("ep_id")
                or ep_info.get("aid")
                or f"{season_info['season_id']}:{ep_index}"
            )
            admission = await self.outward_actions._execute_proactive_action(
                f"bangumi_watch:{episode_key}",
                "bangumi_watch",
                episode_key,
                lambda: True,
                metadata={"source": "bangumi", "reservation_only": True},
            )
            if not admission.success:
                return 0, None, "not_admitted", ""
            logger.info(f"[BiliBot] 🎬 看番：《{season_info['title']}》第{ep_index}话 {ep_title}")

            analysis = await self._analyze_episode(season_info, ep_info)
            logger.info(f"[BiliBot] 📝 分析完成：{analysis[:60]}...")

            evaluation, reason = await self._evaluate_episode(season_info, ep_info, analysis, bangumi_context)
            if not evaluation:
                # 和主动看片一致：评价失败不编一个感受出来，不写记忆、不互动。
                # 这一集也不算看过，下次从这集接着看，免得剧情断掉。
                logger.warning(f"[BiliBot] 番剧评价失败（{reason}），这集不记忆、不互动，下次重看")
                return 0, None, "evaluation_failed", reason

            try:
                score = int(float(str(evaluation.get("score", 5)).strip()))
            except (TypeError, ValueError):
                score = 5
            comment = evaluation.get("comment", "")
            mood = evaluation.get("mood", "平静")
            review = evaluation.get("review", "")
            logger.info(f"[BiliBot] ⭐ 评分：{score}/10 | 心情：{mood} | 短评：{comment[:30]}")

            # 保存番剧专属记忆
            self.bangumi_memory.save_episode(
                season_id=season_info["season_id"], season_title=season_info["title"],
                ep_info=ep_info, score=score, mood=mood, review=review,
                comment=comment, description=analysis[:300],
            )

            # 保存到通用记忆（memory_type=bangumi）
            memory_text = (
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Bot看了番剧"
                f"《{season_info['title']}》第{ep_index}话{ep_title} "
                f"评分:{score}/10 心情:{mood} 感想:{review[:60]}"
            )
            await self.memory_writer._save_self_memory_record(
                f"bangumi:{season_info['season_id']}", memory_text,
                memory_type="bangumi",
                extra={"season_id": str(season_info["season_id"]), "ep_id": str(ep_info.get("ep_id", "")), "tname": "番剧"},
            )
            if evaluation.get("want_dynamic") and ep_info.get("ep_id"):
                self.dynamic_post.collect_idea(
                    key=f"ep:{ep_info.get('ep_id', '')}", kind=evaluation["dynamic_kind"], source="bangumi",
                    title=f"《{season_info['title']}》第{ep_index}话 {ep_title}".strip(), score=score, review=review,
                    angle=evaluation["dynamic_angle"], summary=analysis[:360],
                )

            # 互动
            aid = ep_info.get("aid", 0)
            actions = []
            interaction_ok = True

            if aid:
                cookie_ok, _ = await self.bili_auth.check_cookie()
                if not cookie_ok:
                    logger.warning("[BiliBot] ⚠️ Cookie 失效，跳过互动")
                    interaction_ok = False
                if interaction_ok and score >= 6 and self.settings.get("PROACTIVE_LIKE"):
                    try:
                        if (
                            await self.outward_actions._execute_proactive_action(
                                f"bangumi_like:{episode_key}",
                                "like",
                                episode_key,
                                lambda: self.bili_videos._like_video(aid),
                            )
                        ).success:
                            actions.append("👍点赞")
                        else:
                            interaction_ok = False
                    except Exception:
                        interaction_ok = False
                if interaction_ok:
                    if score >= 8 and self.settings.get("PROACTIVE_COIN"):
                        try:
                            if (
                                await self.outward_actions._execute_proactive_action(
                                    f"bangumi_coin:{episode_key}",
                                    "coin",
                                    episode_key,
                                    lambda: self.bili_videos._coin_video(aid),
                                )
                            ).success:
                                actions.append("🪙投币")
                        except Exception:
                            pass
                    if score >= 8 and self.settings.get("PROACTIVE_FAV"):
                        try:
                            if (
                                await self.outward_actions._execute_proactive_action(
                                    f"bangumi_favorite:{episode_key}",
                                    "favorite",
                                    episode_key,
                                    lambda: self.bili_videos._fav_video(aid),
                                )
                            ).success:
                                actions.append("⭐收藏")
                        except Exception:
                            pass
                    daily_comment_limit = max(0, int(self.settings.get("PROACTIVE_COMMENT_DAILY_LIMIT") or 0))
                    daily_comment_count = self.watch_video._today_proactive_comment_count(
                        self.activity_logs.watch.recent(),
                        self.activity_logs.proactive.recent(),
                    )
                    allow_per_video_comment = max(0, int(self.settings.get("PROACTIVE_COMMENT_COUNT") or 0)) > 0
                    if (
                        score >= 6
                        and comment
                        and allow_per_video_comment
                        and daily_comment_limit > daily_comment_count
                        and self.settings.get("BANGUMI_COMMENT")
                    ):
                        try:
                            sync = self.dynamic_post.comment_syncs()
                            sent = await self.outward_actions._execute_proactive_action(
                                f"bangumi_comment:{episode_key}",
                                "proactive_comment",
                                episode_key,
                                lambda: self.bili_comments._send_comment(
                                    aid, comment, oid_type=1, sync_to_dynamic=sync
                                ),
                                metadata={"text": comment[:200], "title": f"《{season_info['title']}》第{ep_index}话"[:80]},
                            )
                            if sent.success:
                                if sync:
                                    # 池子里番剧的想法按 ``ep:<id>`` 记
                                    self.dynamic_post.comment_synced(
                                        f"ep:{ep_info.get('ep_id', '')}" if ep_info.get("ep_id") else episode_key, comment,
                                        f"《{season_info['title']}》第{ep_index}话")
                                actions.append("💬评论")
                                logger.info(f"[BiliBot] 💬 番剧评论：{comment}")
                                entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "type": "bangumi", "title": f"《{season_info['title']}》第{ep_index}话", "comment": comment}
                                own_rpid = getattr(getattr(sent, "value", None), "rpid", 0)
                                if own_rpid:
                                    entry["own_rpid"] = str(own_rpid)
                                self.activity_logs.proactive.append(entry)
                        except Exception as e:
                            logger.warning(f"[BiliBot] 番剧评论失败: {e}")

            action_str = " ".join(actions) if actions else "（默默看完）"
            logger.info(f"[BiliBot] 📊 互动：{action_str}")

            # 评分高自动追番
            if interaction_ok and score >= 7 and self.settings.get("BANGUMI_AUTO_FOLLOW"):
                try:
                    if (
                        await self.outward_actions._execute_proactive_action(
                            f"bangumi_follow:{season_info['season_id']}",
                            "follow_bangumi",
                            season_info["season_id"],
                            lambda: self.bili_bangumi._follow_bangumi(season_info["season_id"]),
                        )
                    ).success:
                        logger.info(f"[BiliBot] 📌 自动追番：《{season_info['title']}》")
                except Exception:
                    pass

            return score, evaluation, "watched", ""

        except Exception as e:
            logger.error(f"[BiliBot] 看番异常: {e}\n{traceback.format_exc()}")
            return 0, None, "error", exception_detail(e, self.config)

    async def run(self, season_id=None, max_episodes=None, start_ep_id=None, memo_id=None):
        """安全入口。``memo_id``：每日计划给这一格排的番剧备忘；番名搜不到就照常挑番，备忘留着。"""
        self.now_doing.report("bangumi", "正在挑选番剧")
        try:
            if memo_id is not None and not season_id:
                season_id = await self.memo_watch.bangumi_season(memo_id)
                if not season_id:
                    memo_id = None
            await self._watch_season(season_id=season_id, max_episodes=max_episodes, start_ep_id=start_ep_id)
            if memo_id is not None:
                self.memo_watch.finish(memo_id, "看了这部番")
        except asyncio.CancelledError:
            logger.info("[BiliBot] 看番任务被取消")
            raise
        except Exception as e:
            logger.error(f"[BiliBot] 看番任务异常退出: {e}\n{traceback.format_exc()}")
        finally:
            self.now_doing.finish("bangumi")

    async def _watch_season(self, season_id=None, max_episodes=None, start_ep_id=None):
        """看番主流程：选番 → 逐集看 → 评分高则追更。"""
        if max_episodes is None:
            max_episodes = self.settings.get("BANGUMI_EPISODE_COUNT")
        continue_score = self.settings.get("BANGUMI_CONTINUE_SCORE")

        if not season_id:
            season_id = await self._pick_season()
        if not season_id:
            logger.warning("[BiliBot] 选番失败，跳过本次看番")
            return

        detail = await self.bili_bangumi.get_bangumi_detail(season_id=season_id)
        if not detail or not detail.get("episodes"):
            logger.warning(f"[BiliBot] 番剧详情获取失败或无剧集 (sid={season_id})")
            return

        season_info = detail
        self.now_doing.report("bangumi", "正在准备观看", title=season_info.get("title", ""))
        all_eps = detail["episodes"]
        watched_ids = self.bangumi_memory.watched_ep_ids(season_id)
        logger.info(f"[BiliBot] 📺 开始看番：《{season_info['title']}》共{len(all_eps)}集，已看{len(watched_ids)}集")

        unwatched = [ep for ep in all_eps if ep.get("ep_id") and ep["ep_id"] not in watched_ids]
        # 如果指定了起始集，从该集开始
        if start_ep_id:
            found = False
            for i, ep in enumerate(all_eps):
                if ep.get("ep_id") == start_ep_id:
                    # 显式指定的这一集尊重用户意图（即使已看也重看），
                    # 但后续集仍跳过已看的，避免重复分析和重复发公开评论
                    rest = [e for e in all_eps[i + 1:] if e.get("ep_id") and e["ep_id"] not in watched_ids]
                    unwatched = [ep] + rest
                    if ep.get("ep_id") in watched_ids:
                        logger.info(f"[BiliBot] 指定的 ep_id={start_ep_id} 之前看过，按指定重看这一集")
                    found = True
                    break
            if not found:
                logger.warning(f"[BiliBot] 指定的 ep_id={start_ep_id} 未找到，从未看的开始")
        if not unwatched:
            logger.info(f"[BiliBot] 《{season_info['title']}》已全部看完")
            # 标记为已看完，选番时排除
            if self.bangumi_memory.mark_completed(season_id):
                logger.info(f"[BiliBot] 📌 标记《{season_info['title']}》为已看完")
            return

        watched_count = 0
        scores = []

        for ep in unwatched:
            if watched_count >= max_episodes:
                break
            self.now_doing.report("bangumi", "正在分析内容", title=season_info.get("title", ""), episode=f"第{ep.get('ep_index', '?')}话")
            bangumi_context = await self._progress_context(season_id, season_info["title"])
            score, evaluation, status, reason = await self._watch_episode(season_info, ep, bangumi_context)
            if status == "not_admitted":
                logger.info("[BiliBot] 今天的看番额度用完了，停止看番")
                break
            watched_count += 1

            entry = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "season_id": season_id, "title": season_info["title"],
                "ep_index": ep.get("ep_index", "?"),
                "ep_title": ep.get("long_title", "") or ep.get("title", ""),
                "score": score, "mood": (evaluation or {}).get("mood", "未知"),
                "review": (evaluation or {}).get("review", ""),
                "comment": (evaluation or {}).get("comment", ""),
            }
            if status != "watched":
                entry.update(comment="评价失败" if status == "evaluation_failed" else "看番出错",
                             evaluation_status="failed", fail_reason=reason, mood="")
            self.activity_logs.bangumi.append(entry)
            if status != "watched":
                logger.info("[BiliBot] 这集没看成，停止看番，下次从这集接着看")
                break
            scores.append(score)

            # 判断是否继续
            if score < continue_score:
                want = (evaluation or {}).get("want_continue", False)
                if not want:
                    logger.info(f"[BiliBot] 📊 评分{score}<{continue_score}，停止看番")
                    break
                logger.info(f"[BiliBot] 📊 评分{score}偏低但LLM想继续追")

            # 集间等待
            if watched_count < max_episodes and watched_count < len(unwatched):
                wait = random.randint(20, 60)
                logger.info(f"[BiliBot] ⏳ 集间等待 {wait}秒...")
                await asyncio.sleep(wait)

        logger.info(f"[BiliBot] 🎉 看番结束：《{season_info['title']}》本次看了 {watched_count} 集")
        if scores and sum(scores) / len(scores) >= continue_score:
            self._remember_earlier_season(season_info, season_id)

    def _remember_earlier_season(self, season_info, season_id):
        """觉得好看、但前面还有没看完的季：记一条明天的备忘从前面补起（``domain/bangumi_series.py``）。"""
        if not self.memo_tool.enabled():
            return
        memory = self.bangumi_memory.all()
        earlier = season_to_backfill(
            season_info.get("seasons") or [], season_id,
            started={sid for sid, record in memory.items() if record.get("episodes") or record.get("watched_ep_ids")},
            completed={sid for sid, record in memory.items() if record.get("completed")},
        )
        if not earlier:
            return
        title = str(earlier.get("title") or "")[:40]
        try:
            memo_id, created = self.memo_tool._memos().add(
                kind="bangumi", target=f"{title} ss{earlier['season_id']}", when="tomorrow", source="self",
                note=f"看了《{str(season_info.get('title') or '')[:30]}》觉得不错，从前面的季补起", unlimited=True,
            )
        except MemoRejected as exc:
            logger.warning(f"[BiliBot] 想记「从前面的季看起」没记上：{exc}")
            return
        if created:
            logger.info(f"[BiliBot] 📝 记了备忘 #{memo_id}：明天从《{title}》看起")

    async def check_updates(self):
        """检查已追番剧是否有更新，有就触发看番。每天调用一次。"""
        try:
            if not self.settings.get("ENABLE_BANGUMI"):
                return
            if not self.bili_http._has_cookie():
                return

            # 没有更新时照常主动看番，这个时刻不落空。
            followed = await self.bili_bangumi._get_followed_bangumi(follow_status=2)
            if not followed:
                logger.info("[BiliBot] 📋 没有在追的番，改为主动看番")
                await self.run()
                return

            mem = self.bangumi_memory.all()
            to_watch = []

            for f in followed:
                sid = str(f["season_id"])
                record = mem.get(sid)
                if not record or not record.get("episodes"):
                    # 追了但没看过，加入待看
                    to_watch.append(f)
                    continue
                # 检查有没有新集
                watched_ids = self.bangumi_memory.watched_ep_ids(f["season_id"])
                detail = await self.bili_bangumi.get_bangumi_detail(season_id=f["season_id"])
                if not detail or not detail.get("episodes"):
                    continue
                new_eps = [ep for ep in detail["episodes"] if ep.get("ep_id") and ep["ep_id"] not in watched_ids]
                if new_eps:
                    to_watch.append(f)
                    logger.info(f"[BiliBot] 📺 追番更新：《{f['title']}》有 {len(new_eps)} 集新内容")
                await asyncio.sleep(1)  # 避免请求过快

            if not to_watch:
                logger.info("[BiliBot] 📺 追番都是最新的，没有更新，改为主动看番")
                await self.run()
                return

            # 看一部更新的番（随机选，一天一部）
            target = random.choice(to_watch)
            logger.info(f"[BiliBot] 📺 触发追番更新：《{target['title']}》")
            max_ep = self.settings.get("BANGUMI_EPISODE_COUNT")
            await self.run(season_id=target["season_id"], max_episodes=max_ep)

        except Exception as e:
            logger.error(f"[BiliBot] 检查追番更新异常: {e}\n{traceback.format_exc()}")

    async def watch_on_request(self, season_id=None, ep_id=None):
        """QQ 端工具调用入口。"""
        if not self.settings.get("ENABLE_BANGUMI"):
            return "番剧功能未开启（ENABLE_BANGUMI）。"
        if not self.bili_http._has_cookie():
            return "未登录B站，无法看番。"
        if self.activities._activity_busy("bangumi"):
            return "已经在看番了，等这轮看完吧。"
        # 如果只传了 ep_id 没传 season_id，先查出 season_id
        if ep_id and not season_id:
            try:
                detail = await self.bili_bangumi.get_bangumi_detail(ep_id=ep_id)
                if detail:
                    season_id = detail.get("season_id")
            except Exception:
                pass
        # 查 season_id 期间可能已经有别的在看了，这里才真正占住
        if self.activities._run_activity_now("bangumi", season_id=season_id, start_ep_id=ep_id, max_episodes=1) is None:
            return "已经在看番了，等这轮看完吧。"
        return "好的，开始看番了！看完会记录感想。"
