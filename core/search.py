"""联网搜索：Tavily / Perplexity / 博查 / 自定义后端。"""
import re
import json
import time
from collections import OrderedDict
import aiohttp
from astrbot.api import logger
from .config import WEB_SEARCH_CACHE_FILE


class WebSearchMixin:
    """联网搜索。"""

    def _get_web_search_client(self):
        if self._web_search_client is None:
            backend = (self.config.get("WEB_SEARCH_BACKEND", "") or "tavily").lower().strip()
            api_key = self.config.get("WEB_SEARCH_API_KEY", "")
            if not api_key:
                return None
            if backend == "perplexity":
                from openai import AsyncOpenAI
                self._web_search_client = AsyncOpenAI(api_key=api_key, base_url="https://api.perplexity.ai")
            elif backend == "custom":
                base_url = self.config.get("WEB_SEARCH_API_BASE", "")
                if not base_url:
                    return None
                from openai import AsyncOpenAI
                self._web_search_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        return self._web_search_client

    async def _web_search(self, query: str) -> str:
        if not self.config.get("ENABLE_WEB_SEARCH", False):
            return ""
        api_key = self.config.get("WEB_SEARCH_API_KEY", "")
        if not api_key:
            return ""
        backend = (self.config.get("WEB_SEARCH_BACKEND", "") or "tavily").lower().strip()
        max_results = self.config.get("WEB_SEARCH_MAX_RESULTS", 5)
        cache = OrderedDict(self._load_json(WEB_SEARCH_CACHE_FILE, {}))
        cache_key = f"{backend}:{query}"
        if cache_key in cache:
            cached = cache[cache_key]
            if time.time() - cached.get("ts", 0) < 86400:
                logger.debug(f"[BiliBot] 🔍 搜索命中缓存: {query[:40]}")
                cache.move_to_end(cache_key)
                return cached.get("result", "")
            else:
                del cache[cache_key]
        logger.info(f"[BiliBot] 🔍 联网搜索({backend}): {query[:60]}")
        result = ""
        try:
            if backend == "tavily":
                result = await self._search_tavily(query, api_key, max_results)
            elif backend == "perplexity":
                result = await self._search_perplexity(query, max_results)
            elif backend == "bocha":
                result = await self._search_bocha(query, api_key, max_results)
            elif backend == "custom":
                result = await self._search_custom(query, max_results)
            else:
                logger.warning(f"[BiliBot] 未知搜索后端: {backend}")
                return ""
        except Exception as e:
            logger.error(f"[BiliBot] 联网搜索失败({backend}): {e}")
            return ""
        if result:
            cache[cache_key] = {"ts": time.time(), "result": result}
            cache.move_to_end(cache_key)
            while len(cache) > 200:
                cache.popitem(last=False)
            self._save_json(WEB_SEARCH_CACHE_FILE, dict(cache))
        return result

    async def _search_tavily(self, query: str, api_key: str, max_results: int) -> str:
        payload = {"query": query, "max_results": max_results, "search_depth": "basic", "include_answer": True}
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.tavily.com/search",
                json=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.warning(f"[BiliBot] Tavily HTTP {r.status}: {body[:200]}")
                    return ""
                data = await r.json(content_type=None)
        answer = (data.get("answer") or "").strip()
        results = data.get("results", [])
        snippets = []
        for item in results[:max_results]:
            title = item.get("title", "")
            content = item.get("content", "")[:300]
            if title or content:
                snippets.append(f"- {title}: {content}")
        combined = "\n".join(snippets)
        if answer:
            return f"{answer}\n\n相关来源：\n{combined}" if combined else answer
        return combined

    async def _search_perplexity(self, query: str, max_results: int) -> str:
        client = self._get_web_search_client()
        if not client:
            return ""
        model = self.config.get("WEB_SEARCH_MODEL", "") or "sonar"
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一个搜索助手。请根据用户的问题，简洁地汇总相关信息，300字以内，用中文回答。"},
                    {"role": "user", "content": query},
                ],
                max_tokens=400,
            )
            return resp.choices[0].message.content.strip() if resp.choices else ""
        except Exception as e:
            logger.error(f"[BiliBot] Perplexity 调用失败: {e}")
            return ""

    async def _search_bocha(self, query: str, api_key: str, max_results: int) -> str:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.bochaai.com/v1/web-search",
                json={"query": query, "count": max_results, "summary": True},
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.warning(f"[BiliBot] 博查 HTTP {r.status}: {body[:200]}")
                    return ""
                data = await r.json(content_type=None)
        pages = data.get("data", {}).get("webPages", {}).get("value", [])
        if not pages:
            pages = data.get("results", [])
        summary = data.get("data", {}).get("summary", "")
        snippets = []
        for item in pages[:max_results]:
            name = item.get("name") or item.get("title", "")
            snippet = (item.get("summary") or item.get("snippet") or item.get("content", ""))[:300]
            if name or snippet:
                snippets.append(f"- {name}: {snippet}")
        combined = "\n".join(snippets)
        if summary:
            return f"{summary}\n\n相关来源：\n{combined}" if combined else summary
        return combined

    async def _search_custom(self, query: str, max_results: int) -> str:
        client = self._get_web_search_client()
        if not client:
            return ""
        model = self.config.get("WEB_SEARCH_MODEL", "")
        if not model:
            logger.warning("[BiliBot] custom 搜索后端需要配置 WEB_SEARCH_MODEL")
            return ""
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一个搜索助手。请根据用户的问题，简洁地汇总相关信息，300字以内，用中文回答。"},
                    {"role": "user", "content": query},
                ],
                max_tokens=400,
            )
            return resp.choices[0].message.content.strip() if resp.choices else ""
        except Exception as e:
            logger.error(f"[BiliBot] 自定义搜索接口调用失败: {e}")
            return ""

    # ── 搜索判断 ──
    async def _should_search_for_video(self, video_info: dict, extra_context: str) -> str:
        title = video_info.get("title", "")
        desc = video_info.get("desc", "")[:200]
        tname = video_info.get("tname", "")
        owner = video_info.get("owner_name") or video_info.get("up_name", "")
        prompt = f"""判断以下B站视频是否需要联网搜索来补充背景知识，以便更好地理解视频内容。

视频标题：{title}
UP主：{owner}
分区：{tname}
简介：{desc}
{extra_context[:300] if extra_context else ''}

以下情况需要搜索：涉及时事新闻、专业领域知识、特定人物/事件/产品、最新科技动态、争议性话题等。
以下情况不需要搜索：日常vlog、搞笑视频、纯娱乐内容、游戏实况、个人分享等。

请用JSON回复：{{"need_search": true或false, "query": "搜索关键词(不需要搜索则留空)"}}
直接输出JSON。"""
        try:
            text = await self._llm_call(prompt, max_tokens=100)
            if not text:
                return ""
            text = text.replace("```json", "").replace("```", "").strip()
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                obj = json.loads(m.group())
                if obj.get("need_search"):
                    return (obj.get("query") or title).strip()
            return ""
        except Exception as e:
            logger.debug(f"[BiliBot] 搜索判断失败: {e}")
            return ""

    async def _should_search_for_reply(self, user_comment: str, context: str = "") -> str:
        if not self.config.get("ENABLE_WEB_SEARCH", False):
            return ""
        if not self.config.get("WEB_SEARCH_API_KEY", ""):
            return ""
        stripped = re.sub(r'\[.*?\]', '', user_comment).strip()
        if len(stripped) < 4:
            return ""
        SKIP_PATTERNS = (
            "哈哈", "hh", "笑死", "666", "好的", "谢谢", "感谢", "ok", "嗯嗯",
            "确实", "真的", "是的", "对的", "可以", "不错", "厉害", "牛", "绝了",
            "啊这", "草", "乐", "蚌", "典", "急了", "麻了", "顶", "dd", "催更",
            "前排", "火钳刘明", "来了", "打卡", "支持", "加油", "冲", "爱了",
        )
        if stripped.lower() in SKIP_PATTERNS or all(c in "。，！？~…、哈呵嘿嗯啊哦呀w～" for c in stripped):
            return ""
        judge_provider = self.config.get("WEB_SEARCH_JUDGE_PROVIDER_ID", "")
        ctx_block = f"\n最近对话上下文：\n{context[:500]}\n" if context else ""
        prompt = f"""判断以下B站用户评论是否需要联网搜索才能准确回复。
{ctx_block}
用户最新评论：「{user_comment[:300]}」

需要搜索的情况：用户提问了某个事实性问题、问了近期新闻/事件、提到了你可能不了解的专业知识/人物/产品/梗、要求你查某些信息。
不需要搜索的情况：日常聊天、打招呼、表情、吐槽、纯情感表达、闲聊、你能凭自身知识回答的内容。

请用JSON回复：{{"need_search": true或false, "query": "搜索关键词(不需要搜索则留空)"}}
直接输出JSON，不要加任何其他内容。"""
        try:
            text = await self._llm_call(prompt, max_tokens=80, provider_id=judge_provider or None)
            if not text:
                return ""
            text = text.replace("```json", "").replace("```", "").strip()
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                obj = json.loads(m.group())
                if obj.get("need_search"):
                    query = (obj.get("query") or "").strip()
                    if query:
                        logger.info(f"[BiliBot] 🔍 评论触发联网搜索: {query[:60]}")
                        return query
            return ""
        except Exception as e:
            logger.debug(f"[BiliBot] 评论搜索判断失败: {e}")
            return ""
