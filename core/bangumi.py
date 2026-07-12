"""番剧/动漫相关：搜索、详情、排行、看番、评价、记忆。"""
import re
import json
import random
import asyncio
import traceback
from datetime import datetime
from astrbot.api import logger
from .config import BANGUMI_MEMORY_FILE, BANGUMI_WATCH_LOG_FILE, PROACTIVE_LOG_FILE


class BangumiMixin:
    """B站番剧（PGC）功能。"""

    @staticmethod
    def _pgc_ok(d, label="PGC"):
        """检查 PGC API 响应是否为有效 dict 且 code==0。"""
        if not isinstance(d, dict):
            logger.warning(f"[BiliBot] {label}返回非dict: type={type(d).__name__} val={str(d)[:200]}")
            return False
        code = d.get("code", -1)
        if code != 0:
            logger.debug(f"[BiliBot] {label}失败: code={code} msg={d.get('message', '')}")
            return False
        return True

    async def _pgc_get(self, url, params=None, label="PGC"):
        """专用于 PGC API 的 GET：用 text+json.loads 避免 r.json() 的玄学问题。"""
        import json as _json
        try:
            text, _ = await self._http_get_text(url, params=params, timeout=10)
            if not text:
                logger.warning(f"[BiliBot] {label} 返回空响应")
                return None
            d = _json.loads(text)
            if not isinstance(d, dict):
                logger.warning(f"[BiliBot] {label} JSON非dict: type={type(d).__name__} val={str(d)[:200]}")
                return None
            return d
        except _json.JSONDecodeError as e:
            logger.warning(f"[BiliBot] {label} JSON解析失败: {e} text={str(text)[:200]}")
            return None
        except Exception as e:
            logger.warning(f"[BiliBot] {label} 请求失败: {e}")
            return None

    # ══════════════════════════════════════
    #  搜索 & 信息
    # ══════════════════════════════════════

    async def search_bilibili_bangumi(self, keyword, ps=5):
        """搜索B站番剧，返回番剧列表。"""
        try:
            params = await self.sign_wbi_params({
                "keyword": keyword, "search_type": "media_bangumi",
                "page": 1, "page_size": ps,
            })
            d, _ = await self._http_get(
                "https://api.bilibili.com/x/web-interface/wbi/search/type", params=params,
            )
            if not self._pgc_ok(d, "搜索番剧"):
                return []
            results = []
            for item in (d.get("data") or {}).get("result", [])[:ps]:
                title = re.sub(r"<[^>]+>", "", item.get("title", ""))
                score_info = item.get("media_score") or {}
                results.append({
                    "media_id": item.get("media_id", 0),
                    "season_id": item.get("season_id", 0),
                    "title": title,
                    "org_title": item.get("org_title", ""),
                    "season_type_name": item.get("season_type_name", "番剧"),
                    "areas": item.get("areas", ""),
                    "styles": item.get("styles", ""),
                    "cv": item.get("cv", ""),
                    "staff": item.get("staff", ""),
                    "desc": item.get("desc", "")[:150],
                    "score": score_info.get("score", 0),
                    "user_count": score_info.get("user_count", 0),
                    "ep_size": item.get("ep_size", 0),
                    "pubtime": item.get("pubtime", 0),
                    "url": item.get("url", ""),
                    "cover": item.get("cover", ""),
                })
            return results
        except Exception as e:
            logger.error(f"[BiliBot] 搜索番剧异常: {e}")
            return []

    async def get_bangumi_detail(self, season_id=None, ep_id=None):
        """获取番剧详情（剧集列表、评分、简介等）。"""
        try:
            params = {}
            if season_id:
                params["season_id"] = season_id
            elif ep_id:
                params["ep_id"] = ep_id
            else:
                return None
            d = await self._pgc_get(
                "https://api.bilibili.com/pgc/view/web/season", params=params, label="番剧详情",
            )
            if not d or not self._pgc_ok(d, "番剧详情"):
                return None
            result = d.get("result", {})
            if not isinstance(result, dict):
                logger.warning(f"[BiliBot] 番剧详情 result 非dict: type={type(result).__name__} val={str(result)[:200]}")
                return None
            rating = result.get("rating") or {}
            if not isinstance(rating, dict):
                rating = {}
            stat = result.get("stat") or {}
            if not isinstance(stat, dict):
                stat = {}
            episodes = []
            for ep in (result.get("episodes") or []):
                if not isinstance(ep, dict):
                    continue
                episodes.append({
                    "ep_id": ep.get("ep_id") or ep.get("id", 0),
                    "title": ep.get("share_copy", "") or ep.get("long_title", "") or f"第{ep.get('title', '?')}话",
                    "long_title": ep.get("long_title", ""),
                    "ep_index": ep.get("title", ""),
                    "badge": ep.get("badge", ""),
                    "duration": ep.get("duration", 0) // 60000 if ep.get("duration") else 0,
                    "aid": ep.get("aid", 0),
                    "cid": ep.get("cid", 0),
                })
            areas_raw = result.get("areas") or []
            styles_raw = result.get("styles") or []
            return {
                "season_id": result.get("season_id", 0),
                "media_id": result.get("media_id", 0),
                "title": result.get("season_title", "") or result.get("title", ""),
                "evaluate": str(result.get("evaluate", ""))[:300],
                "score": rating.get("score", 0),
                "count": rating.get("count", 0),
                "areas": ", ".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in areas_raw),
                "styles": ", ".join(s.get("name", "") if isinstance(s, dict) else str(s) for s in styles_raw),
                "total_ep": result.get("total", 0),
                "new_ep_desc": (result.get("new_ep") or {}).get("desc", "") if isinstance(result.get("new_ep"), dict) else "",
                "stat_views": stat.get("views", 0),
                "stat_danmakus": stat.get("danmakus", 0),
                "stat_favorites": stat.get("favorites", 0),
                "episodes": episodes,
                "link": result.get("link", ""),
                "cover": result.get("cover", ""),
            }
        except Exception as e:
            logger.error(f"[BiliBot] 番剧详情异常: {e}")
            return None

    async def get_bangumi_trending(self, season_type=1, day=3):
        """获取番剧排行榜。season_type: 1=番剧 4=国创 2=电影 3=纪录片。"""
        try:
            # 番剧(1)和国创(4)用不同的API端点
            if season_type == 1:
                url = "https://api.bilibili.com/pgc/web/rank/list"
            else:
                url = "https://api.bilibili.com/pgc/season/rank/web/list"
            d = await self._pgc_get(url, params={"season_type": season_type, "day": day}, label=f"番剧排行(type={season_type})")
            if not d or not self._pgc_ok(d, f"番剧排行(type={season_type})"):
                return []
            items = (d.get("data") or d.get("result") or {}).get("list", [])
            results = []
            for item in items[:15]:
                rating = item.get("rating", "") or ""
                # 评分可能是 "9.9分" 格式，去掉非数字后缀
                rating_clean = re.sub(r'[^\d.]', '', str(rating))
                stat = item.get("stat") or {}
                new_ep = item.get("new_ep") or {}
                results.append({
                    "season_id": item.get("season_id", 0),
                    "title": item.get("title", ""),
                    "badge": item.get("badge", ""),
                    "score": float(rating_clean) if rating_clean else 0,
                    "new_ep_desc": new_ep.get("index_show", ""),
                    "stat_follow": stat.get("follow", 0),
                    "stat_view": stat.get("view", 0),
                    "stat_danmaku": stat.get("danmaku", 0),
                    "url": item.get("url", ""),
                    "cover": item.get("cover", ""),
                })
            logger.info(f"[BiliBot] 🏆 番剧排行(type={season_type}): {len(results)}条")
            return results
        except Exception as e:
            logger.error(f"[BiliBot] 番剧排行异常: {e}")
            return []

    async def get_bangumi_timeline(self, day_before=0, day_after=6):
        """获取番剧时间表（新番放送表）。"""
        # 优先用 v1 API（更稳定），失败回退 v2
        result = await self._get_bangumi_timeline_v1(day_before, day_after)
        if result:
            return result
        return await self._get_bangumi_timeline_v2(day_before, day_after)

    async def _get_bangumi_timeline_v1(self, before, after):
        """v1 API: /pgc/web/timeline，参数 types/before/after。"""
        try:
            d = await self._pgc_get(
                "https://api.bilibili.com/pgc/web/timeline",
                params={"types": 1, "before": before, "after": after}, label="番剧时间表v1",
            )
            if not d or not self._pgc_ok(d, "番剧时间表v1"):
                return []
            results = []
            for day in (d.get("result") or []):
                date = day.get("date", "")
                day_of_week = day.get("day_of_week", 0)
                for ep in day.get("episodes", []):
                    results.append({
                        "date": date, "day_of_week": day_of_week,
                        "season_id": ep.get("season_id", 0),
                        "ep_id": ep.get("episode_id", 0) or ep.get("ep_id", 0),
                        "title": ep.get("title", ""),
                        "ep_index": ep.get("pub_index", "") or ep.get("ep_index", ""),
                        "pub_ts": ep.get("pub_ts", 0),
                        "published": ep.get("published", 0) == 1,
                        "cover": ep.get("cover", ""),
                    })
            if results:
                logger.info(f"[BiliBot] 📅 番剧时间表v1: {len(results)}条")
            return results
        except Exception as e:
            logger.debug(f"[BiliBot] 番剧时间表v1异常: {e}")
            return []

    async def _get_bangumi_timeline_v2(self, day_before, day_after):
        """v2 API: /pgc/web/timeline/v2，需要 season_type 参数。"""
        try:
            d = await self._pgc_get(
                "https://api.bilibili.com/pgc/web/timeline/v2",
                params={"season_type": 1, "day_before": day_before, "day_after": day_after}, label="番剧时间表v2",
            )
            if not d or not self._pgc_ok(d, "番剧时间表v2"):
                return []
            results = []
            timeline = (d.get("data") or d.get("result") or {})
            if isinstance(timeline, dict):
                timeline = timeline.get("timeline", [])
            for day in (timeline or []):
                date = day.get("date", "")
                day_of_week = day.get("day_of_week", 0)
                for ep in day.get("episodes", []):
                    results.append({
                        "date": date, "day_of_week": day_of_week,
                        "season_id": ep.get("season_id", 0),
                        "ep_id": ep.get("episode_id", 0) or ep.get("ep_id", 0),
                        "title": ep.get("title", ""),
                        "ep_index": ep.get("pub_index", "") or ep.get("ep_index", ""),
                        "pub_ts": ep.get("pub_ts", 0),
                        "published": ep.get("published", 0) == 1,
                        "cover": ep.get("cover", ""),
                    })
            if results:
                logger.info(f"[BiliBot] 📅 番剧时间表v2: {len(results)}条")
            return results
        except Exception as e:
            logger.debug(f"[BiliBot] 番剧时间表v2异常: {e}")
            return []

    # ══════════════════════════════════════
    #  番剧记忆
    # ══════════════════════════════════════

    def _load_bangumi_memory(self):
        return self._load_json(BANGUMI_MEMORY_FILE, {})

    def _save_bangumi_episode_memory(self, season_id, season_title, ep_info, score, mood, review, comment, description):
        """保存单集记忆到番剧记忆文件。"""
        try:
            mem = self._load_bangumi_memory()
            sid = str(season_id)
            if sid not in mem:
                mem[sid] = {"title": season_title, "season_id": season_id, "total_watched": 0, "last_ep_index": "", "last_score": 0, "episodes": []}
            # 去重：同一集不重复保存（覆盖旧记录）
            ep_id = ep_info.get("ep_id", 0)
            if ep_id:
                mem[sid]["episodes"] = [e for e in mem[sid]["episodes"] if e.get("ep_id") != ep_id]
            entry = {
                "ep_id": ep_id,
                "ep_index": ep_info.get("ep_index", ""),
                "title": ep_info.get("title", ""),
                "score": score, "mood": mood,
                "review": review[:100], "comment": comment[:60],
                "description": description[:200],
                "watched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            mem[sid]["episodes"].append(entry)
            # 按集号排序存储
            mem[sid]["episodes"] = self._sort_eps_by_index(mem[sid]["episodes"])
            mem[sid]["total_watched"] = len(mem[sid]["episodes"])
            mem[sid]["last_ep_index"] = ep_info.get("ep_index", "")
            mem[sid]["last_score"] = score
            # 更新已看集号列表（方便展示）
            watched_indices = [e.get("ep_index", "") for e in mem[sid]["episodes"] if e.get("ep_index")]
            mem[sid]["watched_eps"] = watched_indices
            if len(mem[sid]["episodes"]) > 50:
                mem[sid]["episodes"] = mem[sid]["episodes"][-50:]
            self._save_json(BANGUMI_MEMORY_FILE, mem)
            logger.info(f"[BiliBot] 🎬 番剧记忆已保存: 《{season_title}》第{ep_info.get('ep_index', '?')}话 (已看:{','.join(watched_indices)})")
        except Exception as e:
            logger.error(f"[BiliBot] 保存番剧记忆失败: {e}")

    @staticmethod
    def _sort_eps_by_index(eps):
        """按集号排序（ep_index 可能是 "1" "2" "PV" 等）。"""
        def _key(e):
            idx = e.get("ep_index", "")
            try:
                return (0, int(idx))
            except (ValueError, TypeError):
                return (1, idx)
        return sorted(eps, key=_key)

    @staticmethod
    def _find_missing_eps(eps):
        """检测缺失集号（只对纯数字集号生效）。"""
        indices = []
        for e in eps:
            try:
                indices.append(int(e.get("ep_index", "")))
            except (ValueError, TypeError):
                continue
        if len(indices) < 2:
            return []
        full = set(range(min(indices), max(indices) + 1))
        return sorted(full - set(indices))

    async def _get_bangumi_context_with_summary(self, season_id, season_title):
        """获取番剧上下文（含总结）。按集号排序，标注缺集，>5集自动总结。"""
        mem = self._load_bangumi_memory()
        record = mem.get(str(season_id))
        if not record or not record.get("episodes"):
            return ""
        eps = self._sort_eps_by_index(record["episodes"])
        missing = self._find_missing_eps(eps)
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
        summary = await self._summarize_bangumi_episodes(season_title, old_eps)
        recent_lines = [f"[第{e.get('ep_index', '?')}话] {e.get('title', '')} 评分:{e.get('score', '?')}/10 心情:{e.get('mood', '?')} 感想:{e.get('review', '')}" for e in recent_eps]
        parts = []
        if summary:
            parts.append(f"【前{len(old_eps)}集总结】{summary}")
        parts.append("【最近几集】\n" + "\n".join(recent_lines))
        if missing_str:
            parts.append(missing_str)
        return "\n".join(parts)

    async def _summarize_bangumi_episodes(self, season_title, episodes):
        """LLM 总结前几集（不落盘，仅当次使用）。"""
        if not episodes:
            return ""
        try:
            ep_text = "\n".join(f"第{e.get('ep_index', '?')}话: {e.get('description', '')} 感想:{e.get('review', '')}" for e in episodes)
            if len(ep_text) > 2000:
                ep_text = ep_text[:2000] + "…（已截断）"
            prompt = f"你之前追过番剧《{season_title}》，以下是你看过的各集内容和感想，请用200字以内总结前情提要（重点剧情线和关键人物）：\n\n{ep_text}\n\n直接输出总结，不加前缀。"
            result = await self._llm_call(prompt, max_tokens=300)
            if result:
                logger.info(f"[BiliBot] 📝 番剧总结完成: 《{season_title}》前{len(episodes)}集")
            return result or ""
        except Exception as e:
            logger.error(f"[BiliBot] 番剧总结失败: {e}")
            return ""

    def _get_watched_ep_ids(self, season_id):
        mem = self._load_bangumi_memory()
        record = mem.get(str(season_id))
        if not record:
            return set()
        return {e.get("ep_id") for e in record.get("episodes", []) if e.get("ep_id")}

    # ══════════════════════════════════════
    #  看番：分析 & 评价
    # ══════════════════════════════════════

    async def _analyze_bangumi_episode(self, season_info, ep_info):
        """分析番剧单集：封面视觉+字幕+热评（跳过视频下载，番剧PGC内容无法下载）。"""
        try:
            aid = ep_info.get("aid", 0)
            cid = ep_info.get("cid", 0)
            bvid = ""
            if aid:
                try:
                    bvid = await self._oid_to_bvid(aid)
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
            analysis = await self._analyze_video_text(video_info)

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

    async def _evaluate_bangumi_episode(self, season_info, ep_info, analysis, bangumi_context):
        """LLM 评价番剧单集。"""
        sp = await self._get_system_prompt()
        ctx = f"\n\n【你之前看过的进度】\n{bangumi_context}" if bangumi_context else ""
        prompt = f"""你刚看完一集番剧：
- 番名：{season_info.get('title', '')}
- 类型：{season_info.get('styles', '')} | 地区：{season_info.get('areas', '')}
- B站评分：{season_info.get('score', '暂无')}
- 本集：第{ep_info.get('ep_index', '?')}话 {ep_info.get('long_title', '') or ep_info.get('title', '')}
- 内容分析：{analysis[:800]}{ctx}

以JSON格式回复你的观后感：
{{"score": 1到10的整数评分, "comment": "评论区留言（15-30字）", "mood": "看完的心情（开心/平静/无聊/感动/好笑/震撼/困惑/燃/虐 选一个）", "review": "本集感想（50字以内）", "want_continue": true或false}}

comment要求：像追番观众会打的弹幕或评论，可以对剧情发展发表看法、吐槽角色行为、或者表达情绪。
want_continue：是否值得继续追，烂番可以果断弃。
评分：大部分正常番应该在5-7分，不要动不动就8分以上。1-3烂片、4-5凑合、6-7还行、8-9好看、10封神。
直接输出JSON。"""
        custom_inst = self.config.get("CUSTOM_PROACTIVE_INSTRUCTION", "")
        if custom_inst:
            prompt += f"\n\n【补充提示词】{custom_inst}"
        text = None
        try:
            text = await self._llm_call(prompt, system_prompt=sp, max_tokens=350)
            if not text:
                return None
            raw = text
            text = self._repair_llm_json(text)
            m = re.search(r'\{.*\}', text, re.DOTALL)
            candidate = m.group() if m else text
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                # 去尾逗号后重试
                fixed = re.sub(r',\s*([}\]])', r'\1', candidate)
                try:
                    return json.loads(fixed)
                except json.JSONDecodeError:
                    logger.warning(f"[BiliBot] 番剧评价JSON解析失败: {raw[:300]}")
                    return None
        except Exception as e:
            logger.error(f"[BiliBot] 番剧评价失败: {e} | raw={str(text)[:200]}")
            return None

    # ══════════════════════════════════════
    #  选番
    # ══════════════════════════════════════

    async def _pick_bangumi(self):
        """选一部番：当季新番 > 热度排行 > 随机保底。返回 season_id 或 None。"""
        mem = self._load_bangumi_memory()
        # 已经看完的番（有记录且不再追）不重复选；但追更中的可以继续
        pools = self.config.get("BANGUMI_POOLS", ["trending", "timeline"])
        if not pools:
            pools = ["trending"]

        candidates = []
        for pool_raw in pools:
            pool = str(pool_raw).lower().strip()
            try:
                if pool == "trending":
                    for st in [1, 4]:
                        items = await self.get_bangumi_trending(season_type=st)
                        for item in items:
                            sid = item.get("season_id", 0)
                            if sid:
                                candidates.append({"season_id": sid, "title": item.get("title", ""), "score": item.get("score", 0), "source": "trending", "priority": 1})
                elif pool == "timeline":
                    items = await self.get_bangumi_timeline(day_before=2, day_after=0)
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
                items = await self.get_bangumi_trending(season_type=1)
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

    # ══════════════════════════════════════
    #  看番主流程
    # ══════════════════════════════════════

    async def _watch_bangumi_episode(self, season_info, ep_info, bangumi_context):
        """看一集番剧，返回 (score, evaluation_dict) 或 (0, None)。"""
        try:
            ep_index = ep_info.get("ep_index", "?")
            ep_title = ep_info.get("long_title", "") or ep_info.get("title", "")
            logger.info(f"[BiliBot] 🎬 看番：《{season_info['title']}》第{ep_index}话 {ep_title}")

            analysis = await self._analyze_bangumi_episode(season_info, ep_info)
            logger.info(f"[BiliBot] 📝 分析完成：{analysis[:60]}...")

            evaluation = await self._evaluate_bangumi_episode(season_info, ep_info, analysis, bangumi_context)
            if not evaluation:
                logger.warning("[BiliBot] 番剧评价失败，使用保底")
                evaluation = {"score": 5, "comment": "", "mood": "平静", "review": "没什么特别的感觉", "want_continue": False}

            score = evaluation.get("score", 5)
            comment = evaluation.get("comment", "")
            mood = evaluation.get("mood", "平静")
            review = evaluation.get("review", "")
            logger.info(f"[BiliBot] ⭐ 评分：{score}/10 | 心情：{mood} | 短评：{comment[:30]}")

            # 保存番剧专属记忆
            self._save_bangumi_episode_memory(
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
            await self._save_self_memory_record(
                f"bangumi:{season_info['season_id']}", memory_text,
                memory_type="bangumi",
                extra={"season_id": str(season_info["season_id"]), "ep_id": str(ep_info.get("ep_id", "")), "tname": "番剧"},
            )

            # 互动
            aid = ep_info.get("aid", 0)
            actions = []
            interaction_ok = True

            if aid:
                cookie_ok, _ = await self.check_cookie()
                if not cookie_ok:
                    logger.warning("[BiliBot] ⚠️ Cookie 失效，跳过互动")
                    interaction_ok = False
                if interaction_ok and score >= 6 and self.config.get("PROACTIVE_LIKE", True):
                    try:
                        if await self._like_video(aid):
                            actions.append("👍点赞")
                        else:
                            interaction_ok = False
                    except Exception:
                        interaction_ok = False
                if interaction_ok:
                    if score >= 8 and self.config.get("PROACTIVE_COIN", False):
                        try:
                            if await self._coin_video(aid):
                                actions.append("🪙投币")
                        except Exception:
                            pass
                    if score >= 8 and self.config.get("PROACTIVE_FAV", True):
                        try:
                            if await self._fav_video(aid):
                                actions.append("⭐收藏")
                        except Exception:
                            pass
                    if score >= 6 and self.config.get("BANGUMI_COMMENT", True):
                        if not comment:
                            try:
                                sp = await self._get_system_prompt()
                                prompt = f"你刚看完番剧《{season_info.get('title', '')}》第{ep_index}话。在评论区留一条话，像追番观众随手打的评论（不超过30字）。可以说对剧情的反应、吐槽、或者表达情绪。直接输出内容。"
                                comment = await self._llm_call(prompt, system_prompt=sp, max_tokens=80) or "这集还行"
                            except Exception:
                                comment = "这集还行"
                        try:
                            if await self._send_comment(aid, comment, oid_type=1):
                                actions.append("💬评论")
                                logger.info(f"[BiliBot] 💬 番剧评论：{comment}")
                                pl = self._load_json(PROACTIVE_LOG_FILE, [])
                                pl.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "type": "bangumi", "title": f"《{season_info['title']}》第{ep_index}话", "comment": comment})
                                self._save_json(PROACTIVE_LOG_FILE, pl[-100:])
                        except Exception as e:
                            logger.warning(f"[BiliBot] 番剧评论失败: {e}")

            action_str = " ".join(actions) if actions else "（默默看完）"
            logger.info(f"[BiliBot] 📊 互动：{action_str}")

            # 评分高自动追番
            if interaction_ok and score >= 7 and self.config.get("BANGUMI_AUTO_FOLLOW", True):
                try:
                    if await self._follow_bangumi(season_info["season_id"]):
                        logger.info(f"[BiliBot] 📌 自动追番：《{season_info['title']}》")
                except Exception:
                    pass

            return score, evaluation

        except Exception as e:
            logger.error(f"[BiliBot] 看番异常: {e}\n{traceback.format_exc()}")
            return 0, None

    async def _run_bangumi(self, season_id=None, max_episodes=None, start_ep_id=None):
        """安全入口。"""
        try:
            await self._run_bangumi_inner(season_id=season_id, max_episodes=max_episodes, start_ep_id=start_ep_id)
        except asyncio.CancelledError:
            logger.info("[BiliBot] 看番任务被取消")
        except Exception as e:
            logger.error(f"[BiliBot] 看番任务异常退出: {e}\n{traceback.format_exc()}")

    async def _run_bangumi_inner(self, season_id=None, max_episodes=None, start_ep_id=None):
        """看番主流程：选番 → 逐集看 → 评分高则追更。"""
        if max_episodes is None:
            max_episodes = self.config.get("BANGUMI_EPISODE_COUNT", 3)
        continue_score = self.config.get("BANGUMI_CONTINUE_SCORE", 7)

        if not season_id:
            season_id = await self._pick_bangumi()
        if not season_id:
            logger.warning("[BiliBot] 选番失败，跳过本次看番")
            return

        detail = await self.get_bangumi_detail(season_id=season_id)
        if not detail or not detail.get("episodes"):
            logger.warning(f"[BiliBot] 番剧详情获取失败或无剧集 (sid={season_id})")
            return

        season_info = detail
        all_eps = detail["episodes"]
        watched_ids = self._get_watched_ep_ids(season_id)
        logger.info(f"[BiliBot] 📺 开始看番：《{season_info['title']}》共{len(all_eps)}集，已看{len(watched_ids)}集")

        unwatched = [ep for ep in all_eps if ep.get("ep_id") and ep["ep_id"] not in watched_ids]
        # 如果指定了起始集，从该集开始
        if start_ep_id:
            found = False
            for i, ep in enumerate(all_eps):
                if ep.get("ep_id") == start_ep_id:
                    unwatched = all_eps[i:]
                    found = True
                    break
            if not found:
                logger.warning(f"[BiliBot] 指定的 ep_id={start_ep_id} 未找到，从未看的开始")
        if not unwatched:
            logger.info(f"[BiliBot] 《{season_info['title']}》已全部看完")
            # 标记为已看完，选番时排除
            mem = self._load_bangumi_memory()
            sid = str(season_id)
            if sid in mem:
                mem[sid]["completed"] = True
                self._save_json(BANGUMI_MEMORY_FILE, mem)
                logger.info(f"[BiliBot] 📌 标记《{season_info['title']}》为已看完")
            return

        watch_log = self._load_json(BANGUMI_WATCH_LOG_FILE, [])
        watched_count = 0

        for ep in unwatched:
            if watched_count >= max_episodes:
                break
            bangumi_context = await self._get_bangumi_context_with_summary(season_id, season_info["title"])
            score, evaluation = await self._watch_bangumi_episode(season_info, ep, bangumi_context)
            watched_count += 1

            watch_log.append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "season_id": season_id, "title": season_info["title"],
                "ep_index": ep.get("ep_index", "?"),
                "ep_title": ep.get("long_title", "") or ep.get("title", ""),
                "score": score, "mood": (evaluation or {}).get("mood", "未知"),
                "review": (evaluation or {}).get("review", ""),
                "comment": (evaluation or {}).get("comment", ""),
            })
            self._save_json(BANGUMI_WATCH_LOG_FILE, watch_log[-300:])

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

    # ══════════════════════════════════════
    #  追番 & 更新检测
    # ══════════════════════════════════════

    async def _follow_bangumi(self, season_id):
        """追番（点追番按钮）。"""
        try:
            d, _ = await self._http_post(
                "https://api.bilibili.com/pgc/web/follow/add",
                data={"season_id": season_id, "csrf": self.config.get("BILI_JCT", "")},
            )
            if isinstance(d, dict) and d.get("code") == 0:
                logger.info(f"[BiliBot] 📌 追番成功: sid={season_id}")
                return True
            logger.debug(f"[BiliBot] 追番失败: {d}")
            return False
        except Exception as e:
            logger.warning(f"[BiliBot] 追番异常: {e}")
            return False

    async def _get_followed_bangumi(self, follow_status=0, page=1, page_size=30):
        """获取已追番列表。follow_status: 0=全部 1=想看 2=在看 3=看过。"""
        try:
            vmid = self.config.get("DEDE_USER_ID", "")
            if not vmid:
                logger.warning("[BiliBot] 追番列表需要 DEDE_USER_ID")
                return []
            d, _ = await self._http_get(
                "https://api.bilibili.com/x/space/bangumi/follow/list",
                params={"type": 1, "follow_status": follow_status, "pn": page, "ps": page_size, "vmid": vmid},
            )
            if not isinstance(d, dict) or d.get("code") != 0:
                logger.debug(f"[BiliBot] 追番列表失败: {str(d)[:200]}")
                return []
            data = d.get("data") or {}
            if not isinstance(data, dict):
                return []
            items = data.get("list") or []
            followed = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                new_ep = item.get("new_ep") or {}
                if not isinstance(new_ep, dict):
                    new_ep = {}
                followed.append({
                    "season_id": item.get("season_id", 0),
                    "media_id": item.get("media_id", 0),
                    "title": item.get("title", ""),
                    "new_ep_index": new_ep.get("index_show", ""),
                    "new_ep_id": new_ep.get("id", 0),
                    "total_count": item.get("total_count", 0),
                    "progress": (item.get("progress") or {}).get("last_ep_index", "") if isinstance(item.get("progress"), dict) else "",
                })
            logger.info(f"[BiliBot] 📋 追番列表: {len(followed)}部")
            return followed
        except Exception as e:
            logger.error(f"[BiliBot] 获取追番列表异常: {e}")
            return []

    async def _check_bangumi_updates(self):
        """检查已追番剧是否有更新，有就触发看番。每天调用一次。"""
        try:
            if not self.config.get("ENABLE_BANGUMI", False):
                return
            if not self._has_cookie():
                return

            followed = await self._get_followed_bangumi(follow_status=2)
            if not followed:
                logger.info("[BiliBot] 📋 没有在追的番")
                return

            mem = self._load_bangumi_memory()
            to_watch = []

            for f in followed:
                sid = str(f["season_id"])
                record = mem.get(sid)
                if not record or not record.get("episodes"):
                    # 追了但没看过，加入待看
                    to_watch.append(f)
                    continue
                # 检查有没有新集
                watched_ids = {e.get("ep_id") for e in record.get("episodes", []) if e.get("ep_id")}
                detail = await self.get_bangumi_detail(season_id=f["season_id"])
                if not detail or not detail.get("episodes"):
                    continue
                new_eps = [ep for ep in detail["episodes"] if ep.get("ep_id") and ep["ep_id"] not in watched_ids]
                if new_eps:
                    to_watch.append(f)
                    logger.info(f"[BiliBot] 📺 追番更新：《{f['title']}》有 {len(new_eps)} 集新内容")
                await asyncio.sleep(1)  # 避免请求过快

            if not to_watch:
                logger.info("[BiliBot] 📺 追番都是最新的，没有更新")
                return

            # 看一部更新的番（随机选，一天一部）
            target = random.choice(to_watch)
            logger.info(f"[BiliBot] 📺 触发追番更新：《{target['title']}》")
            max_ep = self.config.get("BANGUMI_EPISODE_COUNT", 3)
            await self._run_bangumi(season_id=target["season_id"], max_episodes=max_ep)

        except Exception as e:
            logger.error(f"[BiliBot] 检查追番更新异常: {e}\n{traceback.format_exc()}")

    # ══════════════════════════════════════
    #  工具 & 触发
    # ══════════════════════════════════════

    async def _tool_bili_watch_bangumi_result(self, season_id=None, ep_id=None):
        """QQ 端工具调用入口。"""
        if not self.config.get("ENABLE_BANGUMI", False):
            return "番剧功能未开启（ENABLE_BANGUMI）。"
        if not self._has_cookie():
            return "未登录B站，无法看番。"
        if self._bangumi_task is not None and not self._bangumi_task.done():
            return "已经在看番了，等这轮看完吧。"
        # 如果只传了 ep_id 没传 season_id，先查出 season_id
        if ep_id and not season_id:
            try:
                detail = await self.get_bangumi_detail(ep_id=ep_id)
                if detail:
                    season_id = detail.get("season_id")
            except Exception:
                pass
        self._bangumi_task = asyncio.create_task(self._run_bangumi(season_id=season_id, start_ep_id=ep_id, max_episodes=1))
        return "好的，开始看番了！看完会记录感想。"
