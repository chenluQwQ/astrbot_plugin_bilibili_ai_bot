"""联网搜索：按配置选后端、发请求、把结果整理成文字，结果缓存一天。不懂业务。

``host`` 提供 ``config``、``settings``，客户端也缓存在它身上
（``core/network.py`` 的 ``close_plugin_clients`` 从那里关）。它就是插件对象本身。
要不要搜、搜什么由调用方决定。
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict

import aiohttp
from astrbot.api import logger

from ..network import (
    cached_client, client_fingerprint, configured_proxy, openai_proxy_kwargs,
    plugin_http_session, redact_proxy_error,
)
from ..storage.json_file import JsonFile
from .text import normalize_openai_base_url


# 不填 Key 也能搜的后端（走公开接口，对方会记录搜索词和服务器 IP，有频率上限）。
KEYLESS_BACKENDS = frozenset({"keenable"})


def web_search_backend(settings) -> str:
    return (settings.get("WEB_SEARCH_BACKEND") or "tavily").lower().strip()


def web_search_ready(settings) -> bool:
    """打开了联网搜索，并且填了 Key 或选的是免 Key 后端。"""
    if not settings.get("ENABLE_WEB_SEARCH"):
        return False
    return bool(settings.get("WEB_SEARCH_API_KEY")) or web_search_backend(settings) in KEYLESS_BACKENDS


class WebSearch:
    def __init__(self, host, cache: JsonFile):
        self._host = host
        self._cache = cache

    @property
    def config(self):
        return self._host.config

    @property
    def settings(self):
        return self._host.settings

    @property
    def _web_search_client(self):
        return self._host._web_search_client

    @_web_search_client.setter
    def _web_search_client(self, value):
        self._host._web_search_client = value

    def _get_web_search_client(self):
        backend = web_search_backend(self.settings)
        api_key = self.settings.get("WEB_SEARCH_API_KEY")
        if backend == "perplexity":
            base_url = "https://api.perplexity.ai"
        elif backend == "custom":
            base_url = normalize_openai_base_url(self.settings.get("WEB_SEARCH_API_BASE"))
        else:
            base_url = ""

        def build():
            from openai import AsyncOpenAI
            return AsyncOpenAI(api_key=api_key, base_url=base_url, **openai_proxy_kwargs(self.config))

        ready = api_key and base_url
        fingerprint = client_fingerprint(backend, api_key, base_url, configured_proxy(self.config)) if ready else None
        return cached_client(self._host, "_web_search_client", fingerprint, build)

    async def _web_search(self, query: str) -> str:
        if not web_search_ready(self.settings):
            return ""
        api_key = self.settings.get("WEB_SEARCH_API_KEY") or ""
        backend = web_search_backend(self.settings)
        try:
            max_results = max(1, min(10, int(self.settings.get("WEB_SEARCH_MAX_RESULTS") or 5)))
        except (TypeError, ValueError):
            max_results = 5
        raw_cache = self._cache.read({})
        # 按访问时间排序重建 OrderedDict，确保 LRU 淘汰正确
        cache = OrderedDict(
            sorted(raw_cache.items(), key=lambda x: x[1].get("ts", 0))
        )
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
            elif backend == "firecrawl":
                result = await self._search_firecrawl(query, api_key, max_results)
            elif backend in ("grok", "xai"):
                result = await self._search_grok(query, api_key)
            elif backend == "perplexity":
                result = await self._search_perplexity(query, max_results)
            elif backend == "bocha":
                result = await self._search_bocha(query, api_key, max_results)
            elif backend == "custom":
                result = await self._search_custom(query, max_results)
            elif backend == "exa":
                result = await self._search_exa(query, api_key, max_results)
            elif backend == "deepseek":
                result = await self._search_deepseek(query, api_key, max_results)
            elif backend == "keenable":
                result = await self._search_keenable(query, api_key, max_results)
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
            self._cache.write(dict(cache))
        return result

    @staticmethod
    def _format_firecrawl_results(data: dict, max_results: int) -> str:
        """Normalize Firecrawl v2 (and older flat) search responses for prompts."""
        payload = data.get("data", {}) if isinstance(data, dict) else {}
        if isinstance(payload, dict):
            results = payload.get("web", []) or payload.get("results", [])
        elif isinstance(payload, list):
            results = payload
        else:
            results = []
        snippets = []
        for item in results[:max_results]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or "网页结果").strip()
            url = str(item.get("url") or "").strip()
            description = str(
                item.get("description")
                or item.get("snippet")
                or item.get("markdown")
                or item.get("content")
                or ""
            )
            description = re.sub(r"\s+", " ", description).strip()[:500]
            label = f"[{title}]({url})" if url else title
            if description:
                snippets.append(f"- {label}: {description}")
            elif url or title:
                snippets.append(f"- {label}")
        return "\n".join(snippets)

    async def _search_firecrawl(self, query: str, api_key: str, max_results: int) -> str:
        base_url = str(self.settings.get("WEB_SEARCH_API_BASE") or "https://api.firecrawl.dev").strip().rstrip("/")
        if base_url.lower().endswith("/v2/search"):
            url = base_url
        elif base_url.lower().endswith("/v2"):
            url = f"{base_url}/search"
        else:
            url = f"{base_url}/v2/search"
        payload = {
            "query": query,
            "limit": max_results,
            "sources": ["web"],
            "safe": True,
        }
        async with plugin_http_session(self.config) as s:
            async with s.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.warning(f"[BiliBot] Firecrawl HTTP {r.status}: {body[:200]}")
                    return ""
                data = await r.json(content_type=None)
        return self._format_firecrawl_results(data, max_results)

    @staticmethod
    def _format_grok_response(data: dict) -> str:
        """Extract final text and URL citations from an xAI Responses payload."""
        if not isinstance(data, dict):
            return ""
        texts = []
        citations = []

        def add_citation(value, fallback_title=""):
            if isinstance(value, str):
                url, title = value.strip(), fallback_title or value.strip()
            elif isinstance(value, dict):
                url = str(value.get("url") or "").strip()
                title = str(value.get("title") or fallback_title or url).strip()
            else:
                return
            if url and url not in {item[0] for item in citations}:
                citations.append((url, title))

        direct_text = data.get("output_text")
        if isinstance(direct_text, str) and direct_text.strip():
            texts.append(direct_text.strip())
        for output in data.get("output", []) or []:
            if not isinstance(output, dict) or output.get("type") != "message":
                continue
            for block in output.get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if block.get("type") in ("output_text", "text") and isinstance(text, str) and text.strip():
                    texts.append(text.strip())
                for annotation in block.get("annotations", []) or []:
                    if not isinstance(annotation, dict):
                        continue
                    nested = annotation.get("url_citation")
                    add_citation(nested if isinstance(nested, dict) else annotation)
        for citation in data.get("citations", []) or []:
            add_citation(citation)
        text = "\n".join(dict.fromkeys(texts)).strip()
        uncited = [(url, title) for url, title in citations if url not in text]
        if uncited:
            sources = "\n".join(f"- [{title}]({url})" for url, title in uncited[:8])
            text = f"{text}\n\n相关来源：\n{sources}" if text else sources
        return text

    async def _search_grok(self, query: str, api_key: str) -> str:
        base_url = str(self.settings.get("WEB_SEARCH_API_BASE") or "https://api.x.ai/v1").strip().rstrip("/")
        if base_url.lower().endswith("/responses"):
            url = base_url
        else:
            url = f"{normalize_openai_base_url(base_url)}/responses"
        model = str(self.settings.get("WEB_SEARCH_MODEL") or "grok-4.6").strip()
        payload = {
            "model": model,
            "input": [{
                "role": "user",
                "content": f"请联网搜索并用中文简洁回答，保留关键来源；正文尽量控制在300字以内。\n\n问题：{query}",
            }],
            "tools": [{"type": "web_search"}],
            "max_output_tokens": 600,
        }
        async with plugin_http_session(self.config) as s:
            async with s.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.warning(f"[BiliBot] Grok 搜索 HTTP {r.status}: {body[:300]}")
                    return ""
                data = await r.json(content_type=None)
        return self._format_grok_response(data)

    async def _search_tavily(self, query: str, api_key: str, max_results: int) -> str:
        payload = {"query": query, "max_results": max_results, "search_depth": "basic", "include_answer": True}
        async with plugin_http_session(self.config) as s:
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
        model = self.settings.get("WEB_SEARCH_MODEL") or "sonar"
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一个搜索助手。请根据用户的问题，简洁地汇总相关信息，300字以内，用中文回答。"},
                    {"role": "user", "content": query},
                ],
                max_tokens=400,
            )
            return (resp.choices[0].message.content or "").strip() if resp.choices else ""
        except Exception as e:
            logger.error(f"[BiliBot] Perplexity 调用失败: {redact_proxy_error(e, self.config)}")
            return ""

    async def _search_bocha(self, query: str, api_key: str, max_results: int) -> str:
        async with plugin_http_session(self.config) as s:
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
        model = self.settings.get("WEB_SEARCH_MODEL")
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
            return (resp.choices[0].message.content or "").strip() if resp.choices else ""
        except Exception as e:
            logger.error(f"[BiliBot] 自定义搜索接口调用失败: {redact_proxy_error(e, self.config)}")
            return ""

    @staticmethod
    def _format_exa_results(data: dict, max_results: int) -> str:
        """Exa /search 的结果：每条标题、链接，加摘录（没有摘录就用正文或摘要）。"""
        results = data.get("results", []) if isinstance(data, dict) else []
        snippets = []
        for item in (results if isinstance(results, list) else [])[:max_results]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "网页结果").strip()
            url = str(item.get("url") or "").strip()
            highlights = item.get("highlights")
            if isinstance(highlights, list) and highlights:
                description = " … ".join(str(h) for h in highlights if h)
            else:
                description = str(item.get("summary") or item.get("text") or "")
            description = re.sub(r"\s+", " ", description).strip()[:500]
            label = f"[{title}]({url})" if url else title
            snippets.append(f"- {label}: {description}" if description else f"- {label}")
        return "\n".join(snippets)

    async def _search_exa(self, query: str, api_key: str, max_results: int) -> str:
        base_url = str(self.settings.get("WEB_SEARCH_API_BASE") or "https://api.exa.ai").strip().rstrip("/")
        url = base_url if base_url.lower().endswith("/search") else f"{base_url}/search"
        payload = {
            "query": query,
            "numResults": max_results,
            "type": "auto",
            "contents": {"highlights": {"maxCharacters": 500}},
        }
        async with plugin_http_session(self.config) as s:
            async with s.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "x-api-key": api_key},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.warning(f"[BiliBot] Exa HTTP {r.status}: {body[:200]}")
                    return ""
                data = await r.json(content_type=None)
        return self._format_exa_results(data, max_results)

    @staticmethod
    def _format_keenable_results(data: dict, max_results: int) -> str:
        """每条标题、链接、摘录；有发布时间的带上日期。"""
        results = data.get("results", []) if isinstance(data, dict) else []
        snippets = []
        for item in (results if isinstance(results, list) else [])[:max_results]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "网页结果").strip()
            url = str(item.get("url") or "").strip()
            description = str(item.get("snippet") or item.get("description") or "")
            description = re.sub(r"\s+", " ", description).strip()[:500]
            published = str(item.get("published_at") or "")[:10]
            label = f"[{title}]({url})" if url else title
            if published:
                label += f"（{published}）"
            snippets.append(f"- {label}: {description}" if description else f"- {label}")
        return "\n".join(snippets)

    async def _search_keenable(self, query: str, api_key: str, max_results: int) -> str:
        base_url = str(self.settings.get("WEB_SEARCH_API_BASE") or "https://api.keenable.ai").strip().rstrip("/")
        if base_url.lower().endswith(("/search", "/search/public")):
            base_url = base_url.rsplit("/search", 1)[0]
        if not base_url.lower().endswith("/v1"):
            base_url += "/v1"
        headers = {"Content-Type": "application/json"}
        if api_key:
            url = f"{base_url}/search"
            headers["X-API-Key"] = api_key
        else:
            # 免 Key 的公开接口要求带应用名
            url = f"{base_url}/search/public"
            headers["X-Keenable-Title"] = "astrbot_plugin_bilibili_ai_bot"
        payload = {"query": query, "max_results": max_results, "snippet_max_length": 500}
        async with plugin_http_session(self.config) as s:
            async with s.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.warning(f"[BiliBot] Keenable HTTP {r.status}: {redact_proxy_error(body[:200], self.config)}")
                    return ""
                data = await r.json(content_type=None)
        return self._format_keenable_results(data, max_results)

    @staticmethod
    def _format_deepseek_response(data: dict, max_results: int) -> str:
        """对话接口带联网工具的回复：正文，加上正文里没出现过的引用和搜索来源。"""
        choices = data.get("choices") if isinstance(data, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            return ""
        text = str(message.get("content") or "").strip()
        sources = []

        def add(url, title):
            url = str(url or "").strip()
            if url and url not in {item[0] for item in sources}:
                sources.append((url, str(title or url).strip()))

        for annotation in message.get("annotations") or []:
            cite = annotation.get("url_citation") if isinstance(annotation, dict) else None
            if isinstance(cite, dict):
                add(cite.get("url"), cite.get("title"))
        for tool_call in message.get("server_tool_calls") or []:
            for call in (tool_call.get("calls") or []) if isinstance(tool_call, dict) else []:
                for source in (call.get("sources") or []) if isinstance(call, dict) else []:
                    if isinstance(source, dict):
                        add(source.get("url"), source.get("title"))
        uncited = [(url, title) for url, title in sources if url not in text][:max_results]
        if uncited:
            listed = "\n".join(f"- [{title}]({url})" for url, title in uncited)
            text = f"{text}\n\n相关来源：\n{listed}" if text else listed
        return text

    async def _search_deepseek(self, query: str, api_key: str, max_results: int) -> str:
        base_url = str(self.settings.get("WEB_SEARCH_API_BASE") or "https://api.deepseek.com").strip().rstrip("/")
        url = base_url if base_url.lower().endswith("/chat/completions") else f"{base_url}/chat/completions"
        model = str(self.settings.get("WEB_SEARCH_MODEL") or "deepseek-flash").strip()
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": f"请联网搜索并用中文简洁回答，保留关键来源；正文尽量控制在300字以内。\n\n问题：{query}",
            }],
            "tools": [{"type": "deepseek_web_search"}],
            "thinking": {"type": "disabled"},
            "max_tokens": 600,
        }
        async with plugin_http_session(self.config) as s:
            async with s.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.warning(f"[BiliBot] DeepSeek 搜索 HTTP {r.status}: {body[:300]}")
                    return ""
                data = await r.json(content_type=None)
        return self._format_deepseek_response(data, max_results)
