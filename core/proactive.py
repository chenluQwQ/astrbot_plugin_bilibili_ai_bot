"""主动看视频：视频池拉取、评价、互动、推荐。"""
import re
import json
import random
import asyncio
import traceback
from datetime import datetime
from astrbot.api import logger
from .config import (
    COMMENTED_FILE, EXTERNAL_MEMORY_FILE, PROACTIVE_LOG_FILE,
    PROACTIVE_TRIGGER_LOG_FILE, WATCH_LOG_FILE,
)


class ProactiveMixin:
    """主动刷B站看视频。"""

    PREFERRED_TIDS = [17, 160, 211, 3, 13, 167, 321, 36, 129]

    # ── 视频池 ──
    async def _get_hot_videos(self, min_pubdate=0):
        MIN_VIEWS = 10000
        videos = []
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/popular", params={"ps": 50, "pn": random.randint(1, 5)})
            if d["code"] == 0:
                for v in d.get("data", {}).get("list", []):
                    play = int(v.get("stat", {}).get("view", 0) or 0)
                    pubdate = v.get("pubdate", 0)
                    if play >= MIN_VIEWS and pubdate >= min_pubdate:
                        videos.append({"bvid": v.get("bvid", ""), "title": v.get("title", ""), "desc": v.get("desc", ""), "up_name": v.get("owner", {}).get("name", ""), "up_mid": v.get("owner", {}).get("mid", 0), "pubdate": pubdate, "pic": v.get("pic", ""), "view": play, "tname": v.get("tname", "")})
                logger.info(f"[BiliBot] 🔥 热门API返回 {len(videos)} 个符合条件的视频")
            else:
                logger.warning(f"[BiliBot] 热门API返回非0: code={d['code']}")
        except Exception as e:
            logger.warning(f"[BiliBot] 热门API失败: {e}")
        return videos

    async def _get_newlist_videos(self, tid, min_pubdate=0):
        MIN_VIEWS = 10000
        videos = []
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/newlist", params={"rid": tid, "ps": 50, "pn": 1, "type": 0})
            if d["code"] == 0:
                for v in d.get("data", {}).get("archives", []):
                    play = int(v.get("stat", {}).get("view", 0) or 0)
                    pubdate = v.get("pubdate", 0)
                    if play >= MIN_VIEWS and pubdate >= min_pubdate:
                        videos.append({"bvid": v["bvid"], "title": v["title"], "desc": v.get("desc", ""), "up_name": v["owner"]["name"], "up_mid": v["owner"]["mid"], "pubdate": pubdate, "pic": v.get("pic", ""), "view": play, "tname": v.get("tname", "")})
            else:
                logger.warning(f"[BiliBot] newlist返回非0: code={d['code']} tid={tid}")
        except Exception as e:
            logger.warning(f"[BiliBot] newlist失败: {e}")
        seen = set()
        unique = []
        for v in videos:
            if v["bvid"] and v["bvid"] not in seen:
                seen.add(v["bvid"])
                unique.append(v)
        unique.sort(key=lambda x: x.get("view", 0), reverse=True)
        return unique

    async def _get_weekly_videos(self):
        videos = []
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/popular/series/list", params={"page_size": 1, "page_number": 1})
            if d["code"] != 0:
                return videos
            series_list = d.get("data", {}).get("list", [])
            if not series_list:
                return videos
            latest_number = series_list[0].get("number", 1)
            d2, _ = await self._http_get("https://api.bilibili.com/x/web-interface/popular/series/one", params={"number": latest_number})
            if d2["code"] == 0:
                for v in d2.get("data", {}).get("list", []):
                    videos.append({"bvid": v.get("bvid", ""), "title": v.get("title", ""), "desc": v.get("desc", ""), "up_name": v.get("owner", {}).get("name", ""), "up_mid": v.get("owner", {}).get("mid", 0), "pubdate": v.get("pubdate", 0), "pic": v.get("pic", ""), "view": int(v.get("stat", {}).get("view", 0) or 0), "tname": v.get("tname", "")})
                logger.info(f"[BiliBot] 📅 每周必看第{latest_number}期：{len(videos)} 个视频")
        except Exception as e:
            logger.warning(f"[BiliBot] 每周必看API失败: {e}")
        return videos

    async def _get_precious_videos(self):
        videos = []
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/popular/precious", params={"page_size": 50, "page": 1})
            if d["code"] == 0:
                for v in d.get("data", {}).get("list", []):
                    videos.append({"bvid": v.get("bvid", ""), "title": v.get("title", ""), "desc": v.get("desc", ""), "up_name": v.get("owner", {}).get("name", ""), "up_mid": v.get("owner", {}).get("mid", 0), "pubdate": v.get("pubdate", 0), "pic": v.get("pic", ""), "view": int(v.get("stat", {}).get("view", 0) or 0), "tname": v.get("tname", "")})
                logger.info(f"[BiliBot] 💎 入站必刷：{len(videos)} 个视频")
        except Exception as e:
            logger.warning(f"[BiliBot] 入站必刷API失败: {e}")
        return videos

    async def _get_ranking_videos(self, rid=0):
        videos = []
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/ranking/v2", params={"rid": rid, "type": "all"})
            if d["code"] == 0:
                for v in d.get("data", {}).get("list", []):
                    videos.append({"bvid": v.get("bvid", ""), "title": v.get("title", ""), "desc": v.get("desc", ""), "up_name": v.get("owner", {}).get("name", ""), "up_mid": v.get("owner", {}).get("mid", 0), "pubdate": v.get("pubdate", 0), "pic": v.get("pic", ""), "view": int(v.get("stat", {}).get("view", 0) or 0), "tname": v.get("tname", "")})
                logger.info(f"[BiliBot] 🏆 排行榜(rid={rid})：{len(videos)} 个视频")
        except Exception as e:
            logger.warning(f"[BiliBot] 排行榜API失败: {e}")
        return videos

    async def _get_pool_videos(self, min_pubdate=0):
        pools = self.config.get("PROACTIVE_VIDEO_POOLS", ["popular"])
        if not pools:
            pools = ["popular"]
        all_videos = []
        for pool_raw in pools:
            pool = str(pool_raw).lower().strip()
            if pool == "popular":
                all_videos.extend(await self._get_hot_videos(min_pubdate))
            elif pool == "weekly":
                all_videos.extend(await self._get_weekly_videos())
            elif pool == "precious":
                all_videos.extend(await self._get_precious_videos())
            elif pool.startswith("ranking"):
                ids = [int(x.strip()) for x in pool.split(":", 1)[1].split(",")] if ":" in pool else [0]
                for rid in ids:
                    all_videos.extend(await self._get_ranking_videos(rid))
            elif pool.startswith("newlist"):
                ids = [int(x.strip()) for x in pool.split(":", 1)[1].split(",")] if ":" in pool else []
                if not ids:
                    logger.warning("[BiliBot] newlist 需要指定子分区 tid，如 newlist:17")
                for tid in ids:
                    all_videos.extend(await self._get_newlist_videos(tid, min_pubdate))
            else:
                logger.warning(f"[BiliBot] 未知视频池: {pool}")
        logger.info(f"[BiliBot] 📦 视频池合计: {len(all_videos)} 个（来源: {', '.join(str(p) for p in pools)}）")
        return all_videos

    # ── 评价 & 评论 ──
    async def _evaluate_video(self, video_info, video_description):
        sp = self._get_system_prompt()
        prompt = f"""你刚看完一个B站视频：
- UP主：{video_info.get('up_name', '')}
- 标题：{video_info.get('title', '')}
- 简介：{video_info.get('desc', '')[:100]}
- 视频内容：{video_description}

请以JSON格式回复：
{{"score": 1到10的整数评分, "comment": "你想在评论区说的话（15-30字）", "mood": "看完后的心情（开心/平静/无聊/感动/好笑/震撼/困惑 选一个）", "review": "稍微详细的感想（50字以内）", "want_follow": true或false, "recommend_owner": true或false, "recommend_reason": "推荐理由（20字以内，不推荐则留空）"}}

comment要求：像B站用户真实评论，可以玩梗吐槽。
评分：1-3差，4-5一般，6-7不错，8-9很好，10神作。不要无脑高分。
直接输出JSON。"""
        custom_proactive_inst = self.config.get("CUSTOM_PROACTIVE_INSTRUCTION", "")
        if custom_proactive_inst:
            prompt += f"\n\n【补充提示词】{custom_proactive_inst}"
        text = None
        try:
            text = await self._llm_call(prompt, system_prompt=sp, max_tokens=350)
            if not text:
                return None
            raw = text
            text = self._repair_llm_json(text)
            # 修复LLM返回的中文引号导致JSON解析失败
            m = re.search(r'\{.*\}', text, re.DOTALL)
            candidate = m.group() if m else text
            try:
                return json.loads(candidate)
            except Exception:
                # 容错：去掉尾逗号、尝试 ast.literal_eval
                fixed = re.sub(r',\s*([}\]])', r'\1', candidate)
                try:
                    return json.loads(fixed)
                except Exception:
                    try:
                        import ast
                        return ast.literal_eval(fixed)
                    except Exception:
                        logger.warning(f"[BiliBot] 视频评价 JSON 解析失败，原始返回: {raw[:500]}")
                        return None
        except Exception as e:
            logger.error(f"[BiliBot] 视频评价失败: {e} | raw={str(text)[:300]}")
            return None

    async def _generate_proactive_comment(self, video_info, video_description):
        sp = self._get_system_prompt()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt = f"""当前时间：{now}

你刚刚看完了一个视频：
- UP主：{video_info.get('up_name', '')}
- 标题：{video_info.get('title', '')}
- 视频内容：{video_description}

请以B站观众的身份，发一条自然真实的评论。要求：
1. 根据视频内容说有意义的话，不要无脑夸
2. 体现你的性格
3. 不超过40字
4. 直接输出评论内容，不加任何前缀"""
        custom_proactive_inst = self.config.get("CUSTOM_PROACTIVE_INSTRUCTION", "")
        if custom_proactive_inst:
            prompt += f"\n\n【补充提示词】{custom_proactive_inst}"
        result = await self._llm_call(prompt, system_prompt=sp, max_tokens=100)
        return result or "这个视频还不错"

    # ── 触发判断 ──
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
        result = await self._llm_call(prompt, max_tokens=5)
        return (result or "").strip().lower().startswith("y")

    async def _maybe_trigger_proactive_from_llm(self, event, req):
        if not self.config.get("ENABLE_PROACTIVE", False):
            return
        if not self._has_cookie():
            return
        if self._proactive_task is not None and not self._proactive_task.done():
            return
        msg = event.message_str or ""
        if not await self._should_trigger_proactive_from_text(msg):
            return
        self._proactive_task = asyncio.create_task(self._run_proactive(max_watch=1))
        trigger_log = self._load_json(PROACTIVE_TRIGGER_LOG_FILE, [])
        trigger_log.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "manual_proactive_request", "scheduled": "llm_request", "status": "triggered", "content": msg[:100]})
        self._save_json(PROACTIVE_TRIGGER_LOG_FILE, trigger_log[-200:])
        req.system_prompt += "\n\n【系统提示】你已经决定现在去随机看一些B站视频，并已在后台开始执行一次主动看视频流程。回复用户时明确告诉对方你已经去看了。"

    # ── 主流程 ──
    async def _run_proactive(self, max_watch=None, max_comment=None):
        try:
            await self._run_proactive_inner(max_watch=max_watch, max_comment=max_comment)
        except asyncio.CancelledError:
            logger.info("[BiliBot] 主动看视频任务被取消")
        except Exception as e:
            logger.error(f"[BiliBot] 主动看视频任务异常退出: {e}\n{traceback.format_exc()}")

    async def _run_proactive_inner(self, max_watch=None, max_comment=None):
        env = self._get_environment_status()
        if not env["features"]["proactive_video_media"]:
            logger.warning("[BiliBot] 当前环境不满足视频媒体分析条件，将回退为纯文本视频分析。")
        is_manual = max_watch is not None
        daily_watch = max_watch if is_manual else self.config.get("PROACTIVE_VIDEO_COUNT", 3)
        daily_comment = max_comment if max_comment is not None else random.randint(1, 3)
        watch_log = self._load_json(WATCH_LOG_FILE, [])
        today_str = datetime.now().strftime("%Y-%m-%d")
        if not is_manual:
            today_watched = [l for l in watch_log if l.get("time", "").startswith(today_str) and not l.get("manual")]
            if len(today_watched) >= daily_watch:
                logger.info(f"[BiliBot] 今天已看 {len(today_watched)} 个视频，不再刷")
                return
        logger.info(f"[BiliBot] 🎯 主动刷B站 | 目标：看 {daily_watch} 个视频，评论 {daily_comment} 条")
        external_memory = self._load_json(EXTERNAL_MEMORY_FILE, {})
        commented_videos = set(self._load_json(COMMENTED_FILE, []))
        watched_bvids = set(commented_videos)
        for entry in watch_log:
            watched_bvids.add(entry.get("bvid", ""))
        min_pubdate_hot = int(datetime(datetime.now().year, 1, 1).timestamp())
        target_videos = []
        special_mids = self.config.get("PROACTIVE_FOLLOW_UIDS", [])
        for mid in special_mids:
            video = await self._get_up_latest_video(mid)
            if video and video["bvid"] not in watched_bvids:
                target_videos.insert(0, video)
                logger.info(f"[BiliBot] ⭐ 特别关心：{video['up_name']} - {video['title']}")
        following_mids = await self.get_followings()
        logger.info(f"[BiliBot] 📡 关注列表：{len(following_mids)} 个UP主")
        today = datetime.now().date()
        for mid in following_mids:
            video = await self._get_up_latest_video(mid)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            if video and video["bvid"] not in watched_bvids:
                pubdate = video.get("pubdate", 0)
                if isinstance(pubdate, str):
                    try:
                        pubdate = int(pubdate)
                    except Exception:
                        pubdate = 0
                if pubdate and datetime.fromtimestamp(pubdate).date() == today:
                    target_videos.append(video)
                    logger.info(f"[BiliBot] 🔔 今日更新：{video['up_name']} - {video['title']}")
        pool_videos = await self._get_pool_videos(min_pubdate_hot)
        for v in pool_videos:
            if v["bvid"] not in watched_bvids:
                target_videos.append(v)
        if len(target_videos) < daily_watch:
            tids = list(self.PREFERRED_TIDS)
            random.shuffle(tids)
            for tid in tids[:3]:
                if len(target_videos) >= daily_watch + 5:
                    break
                fallback = await self._get_newlist_videos(tid, min_pubdate_hot)
                for v in fallback:
                    if v["bvid"] not in watched_bvids:
                        target_videos.append(v)
        logger.info(f"[BiliBot] 📊 视频来源统计：特别关注={len(special_mids)}个UP | 关注列表={len(following_mids)}个UP | 收集到={len(target_videos)}个视频")
        seen = set()
        unique = []
        for v in target_videos:
            if v["bvid"] not in seen:
                seen.add(v["bvid"])
                unique.append(v)
        sc = len(special_mids)
        if len(unique) > sc:
            tail = unique[sc:]
            random.shuffle(tail)
            unique = unique[:sc] + tail
        logger.info(f"[BiliBot] 📋 共找到 {len(unique)} 个视频")
        watch_count = 0
        comment_count = 0
        for video in unique:
            if watch_count >= daily_watch:
                break
            bvid = video["bvid"]
            if str(video.get("up_mid", "")) == self.config.get("DEDE_USER_ID", ""):
                continue
            logger.info(f"[BiliBot] 🎬 [{watch_count + 1}/{daily_watch}] {video['title']} by {video.get('up_name', '')}")
            oid = video.get("oid") or await self._get_video_oid(bvid) or 0
            vi = await self._get_video_info(oid) if oid else None
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
            video_description = await self._analyze_video_with_vision(analysis_info)
            logger.info(f"[BiliBot] 📝 分析：{video_description[:60]}...")
            evaluation = await self._evaluate_video(analysis_info, video_description)
            if not evaluation:
                logger.warning("[BiliBot] 评价失败，跳过互动")
                watch_log.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "bvid": bvid, "title": video.get("title", ""), "up_name": video.get("up_name", ""), "score": 0, "mood": "未知", "comment": "评价失败", "review": "", "actions": [], "pic": video.get("pic", ""), "manual": is_manual})
                self._save_json(WATCH_LOG_FILE, watch_log[-200:])
                watched_bvids.add(bvid)
                watch_count += 1
                continue
            score = evaluation.get("score", 5)
            comment = evaluation.get("comment", "")
            mood = evaluation.get("mood", "平静")
            review = evaluation.get("review", "")
            want_follow = evaluation.get("want_follow", False)
            logger.info(f"[BiliBot] ⭐ 评分：{score}/10 | 心情：{mood} | 短评：{comment}")
            actions = []
            interaction_failed = False
            if oid:
                # 交互前快速校验 Cookie
                cookie_ok, _ = await self.check_cookie()
                if not cookie_ok:
                    logger.warning("[BiliBot] ⚠️ Cookie 已失效，跳过本轮所有互动操作")
                    interaction_failed = True
                elif score >= 6 and self.config.get("PROACTIVE_LIKE", True):
                    if await self._like_video(oid):
                        actions.append("👍点赞")
                        logger.info("[BiliBot] 👍 点赞成功")
                    else:
                        # 点赞是最轻量的操作，如果连这个都失败大概率是风控
                        interaction_failed = True
                if not interaction_failed:
                    if score >= 8 and self.config.get("PROACTIVE_COIN", False):
                        if await self._coin_video(oid):
                            actions.append("🪙投币")
                            logger.info("[BiliBot] 🪙 投币成功")
                    if score >= 8 and self.config.get("PROACTIVE_FAV", True):
                        if await self._fav_video(oid):
                            actions.append("⭐收藏")
                            logger.info("[BiliBot] ⭐ 收藏成功")
                    if score >= 7 and comment_count < daily_comment and self.config.get("PROACTIVE_COMMENT", True):
                        proactive_comment = await self._generate_proactive_comment(analysis_info, video_description)
                        if await self._send_comment(oid, proactive_comment):
                            actions.append("💬评论")
                            comment_count += 1
                            logger.info(f"[BiliBot] 💬 评论成功：{proactive_comment}")
                            commented_videos.add(bvid)
                            self._save_json(COMMENTED_FILE, list(commented_videos))
                            pl = self._load_json(PROACTIVE_LOG_FILE, [])
                            pl.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "bvid": bvid, "title": video.get("title", ""), "comment": proactive_comment})
                            self._save_json(PROACTIVE_LOG_FILE, pl[-100:])
                    if evaluation.get("recommend_owner", False):
                        on = self.config.get("OWNER_NAME", "") or "主人"
                        owner_bili = self.config.get("OWNER_BILI_NAME", "")
                        if owner_bili:
                            try:
                                rec_prompt = f"""你刚看完视频「{video.get('title', '')}」，觉得很不错想推荐给{on}。
写一句简短的推荐语，要求：
- 用你自己的语气，自然随意
- 不超过25字
- 不要带@、不要带任何人名或称呼
- 直接输出推荐语"""
                                custom_rec_inst = self.config.get("CUSTOM_RECOMMEND_INSTRUCTION", "")
                                if custom_rec_inst:
                                    rec_prompt += f"\n【补充提示词】{custom_rec_inst}"
                                rec_text = await self._llm_call(rec_prompt, system_prompt=self._get_system_prompt(), max_tokens=60)
                                rec_text = re.sub(r'@\S+\s*', '', rec_text or "你可能会喜欢这个")
                                owner_name = (self.config.get("OWNER_NAME", "") or "").strip()
                                _name_patterns = ["主人", "亲爱的"] + ([re.escape(owner_name)] if owner_name else [])
                                rec_text = re.sub(rf'^({"|".join(_name_patterns)})[，,\s]*', '', rec_text)
                                rec_msg = f"@{owner_bili} {rec_text}"
                                if await self._send_comment(oid, rec_msg):
                                    actions.append("📢推荐给主人")
                                    logger.info(f"[BiliBot] 📢 已@主人：{rec_msg}")
                            except Exception:
                                pass
            if not interaction_failed and (score >= 9 or want_follow) and self.config.get("PROACTIVE_FOLLOW", True):
                if str(video.get("up_mid", "")) != str(self.config.get("OWNER_MID", "")):
                    if await self._follow_user(video["up_mid"]):
                        actions.append("➕关注")
                        logger.info(f"[BiliBot] ➕ 关注了 {video.get('up_name', '')}")
            log_entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "bvid": bvid, "title": video.get("title", ""), "up_name": video.get("up_name", ""), "up_mid": str(video.get("up_mid", "")), "score": score, "mood": mood, "comment": comment, "review": review, "actions": actions, "pic": video.get("pic", ""), "manual": is_manual}
            watch_log.append(log_entry)
            self._save_json(WATCH_LOG_FILE, watch_log[-200:])
            memory_text = (
                f"[{log_entry['time']}] Bot看了视频《{video.get('title', '')}》"
                f"(UP主:{video.get('up_name', '')}) "
                f"评分:{score}/10 心情:{mood} "
                f"感想:{review[:80]} "
                f"内容:{video_description[:120]}"
            )
            await self._save_self_memory_record("proactive_watch", memory_text, memory_type="video", extra={"bvid": bvid, "owner_mid": str(video.get("up_mid", "")), "video_title": video.get("title", "")})
            if bvid not in external_memory:
                external_memory[bvid] = {"title": video.get("title", ""), "up_name": video.get("up_name", ""), "up_mid": str(video.get("up_mid", "")), "description": video_description, "score": score, "mood": mood, "review": review, "watched_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "comments": []}
                self._save_json(EXTERNAL_MEMORY_FILE, external_memory)
            watched_bvids.add(bvid)
            watch_count += 1
            action_str = " ".join(actions) if actions else "（默默看完）"
            logger.info(f"[BiliBot] 📊 互动：{action_str}")
            wait = random.randint(30, 120)
            logger.info(f"[BiliBot] ⏳ 等待 {wait} 秒...")
            await asyncio.sleep(wait)
        logger.info(f"[BiliBot] 🎉 刷B站完成！看了 {watch_count} 个视频，评论了 {comment_count} 条")
