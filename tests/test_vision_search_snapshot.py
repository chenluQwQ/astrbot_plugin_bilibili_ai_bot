"""识图、向量和联网搜索的快照：每个方法在各种响应下发了什么请求、返回了什么。

搬进 ``core/infra/vision.py``、``core/infra/search.py`` 时用它确认没搬错。

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_vision_search_snapshot.py
"""
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.features.search_judge import SearchJudge
from core.settings import Settings
from core.storage.json_file import JsonFile
from base_parts import BaseParts
from core.video_diagnostics import get_call_diagnostic, set_call_diagnostic
from core.infra.bili.comments import BiliComments
from core.infra.search import WebSearch
from core.infra.vision import Vision
from itself import itself

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "vision_search.json"
NOW = 1_700_000_000.0


class Config(dict):
    def save_config(self):
        pass


class Rejected(Exception):
    status_code = 400


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def json(self, content_type=None):
        return self.payload

    async def text(self):
        return json.dumps(self.payload, ensure_ascii=False)

    async def read(self):
        return b"image-bytes"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, probe):
        self.probe = probe

    def _request(self, method, url, kwargs):
        timeout = kwargs.get("timeout")
        self.probe.calls.append({
            "http": method, "url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers"),
            "timeout": getattr(timeout, "total", None),
        })
        if isinstance(self.probe.http, Exception):
            raise self.probe.http
        return FakeResponse(*self.probe.http)

    def get(self, url, **kwargs):
        return self._request("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeOpenAI:
    """顶替 ``openai.AsyncOpenAI``，记下怎么建的客户端、发了什么。"""

    def __init__(self, probe, **kwargs):
        self.probe = probe
        probe.calls.append({"openai_client": kwargs})
        self.embeddings = types.SimpleNamespace(create=self._embed)
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._chat))

    async def _embed(self, **kwargs):
        self.probe.calls.append({"embed": kwargs})
        if isinstance(self.probe.openai, Exception):
            raise self.probe.openai
        return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=[0.5, 0.5])])

    async def _chat(self, **kwargs):
        self.probe.calls.append({"chat": kwargs})
        if isinstance(self.probe.openai, Exception):
            raise self.probe.openai
        if self.probe.openai is None:
            return types.SimpleNamespace(choices=[])
        message = types.SimpleNamespace(content=self.probe.openai)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class Probe(BaseParts):
    models = itself

    def __init__(self, config, http=(200, {}), openai="模型回答", provider=None, llm=None, api=None):
        self.config = Config(config)
        self.settings = Settings(self.config)
        self.http = http
        self.openai = openai
        self.provider = provider
        self.llm = llm
        self.api = api
        self.calls = []
        self._embed_client = None
        self._video_vision_client = None
        self._image_vision_client = None
        self._web_search_client = None
        self.context = types.SimpleNamespace(llm_generate=self._llm_generate)
        self.vision = Vision(self)
        self.search_judge = SearchJudge(self)

    async def _llm_generate(self, **kwargs):
        self.calls.append({"provider": kwargs})
        if isinstance(self.provider, Exception):
            raise self.provider
        return types.SimpleNamespace(completion_text=self.provider) if self.provider is not None else None

    async def _llm_call(self, prompt, **kwargs):
        self.calls.append({"llm": prompt, **kwargs})
        if isinstance(self.llm, Exception):
            raise self.llm
        return self.llm

    async def _http_get(self, url, headers=None, params=None, **kwargs):
        self.calls.append({"api": url, "params": params})
        if isinstance(self.api, Exception):
            raise self.api
        return self.api, None


def _owner(probe, method):
    """场景里的方法现在住在哪：识图和向量在 ``Vision``，评论图片在B站评论接口那层，
    搜索在 ``WebSearch``，要不要搜在 ``SearchJudge``。"""
    if method == "_get_comment_images":
        return BiliComments(probe)
    if hasattr(SearchJudge, method):
        return probe.search_judge
    if hasattr(WebSearch, method):
        return probe.web_search
    return probe.vision if hasattr(Vision, method) else probe


SEARCH = {"ENABLE_WEB_SEARCH": True, "WEB_SEARCH_API_KEY": "key", "WEB_SEARCH_MAX_RESULTS": 2}


def search(backend, **extra):
    return {**SEARCH, "WEB_SEARCH_BACKEND": backend, **extra}


IMAGE = {"IMAGE_VISION_API_KEY": "key", "IMAGE_VISION_MODEL": "m", "IMAGE_VISION_API_BASE": "https://img.example"}
PARTS = [{"type": "text", "text": "看图"}]
LONG = "长" * 600
TAVILY = {"answer": "答案", "results": [{"title": "t1", "content": "c1"}, {"title": "t2", "content": "c2"}, {"title": "t3", "content": "c3"}]}
FIRECRAWL = {"data": {"web": [
    {"title": "t1", "url": "https://a", "description": "  d  1 "}, {"title": "t2", "url": "https://b"}, {"title": "t3"},
]}}
GROK = {"output_text": "回答", "output": [{"type": "message", "content": [{
    "type": "output_text", "text": "回答二", "annotations": [{"url_citation": {"url": "https://a", "title": "来源"}}],
}]}], "citations": ["https://b"]}
BOCHA = {"data": {"summary": "总结", "webPages": {"value": [{"name": "n1", "summary": "s1"}, {"name": "n2", "snippet": "s2"}]}}}
JUDGE_YES = '```json\n{"need_search": true, "query": "查这个"}\n```'
JUDGE_NO = '{"need_search": false, "query": ""}'
VIDEO = {"title": "标题", "desc": "简介", "tname": "分区", "owner_name": "UP"}

# 场景名 → (方法, 参数, Probe 的设置, 缓存文件原有内容)
SCENES = {
    "embed/no_key": ("_get_embedding", ("文本",), {"config": {}}),
    "embed/ok": ("_get_embedding", ("文本",), {"config": {"EMBED_API_KEY": "k", "EMBED_MODEL": "e", "EMBED_API_BASE": "https://e.example", "EMBED_TIMEOUT_SECONDS": 500}}),
    "embed/bad_timeout": ("_get_embedding", ("文本",), {"config": {"EMBED_API_KEY": "k", "EMBED_MODEL": "e", "EMBED_TIMEOUT_SECONDS": "坏"}}),
    "embed/error": ("_get_embedding", ("文本",), {"config": {"EMBED_API_KEY": "k", "EMBED_MODEL": "e"}, "openai": RuntimeError("断了")}),
    "video_client/no_key": ("_get_video_vision_client", (), {"config": {}}),
    "video_client/ok": ("_get_video_vision_client", (), {"config": {"VIDEO_VISION_API_KEY": "k", "VIDEO_VISION_API_BASE": "https://v.example"}}),
    "image_client/no_key": ("_get_image_vision_client", (), {"config": {}}),
    "image_client/ok": ("_get_image_vision_client", (), {"config": IMAGE}),
    "fetch/ok": ("_fetch_image_base64", ("//i0.example/a.png",), {"config": {}}),
    "fetch/https": ("_fetch_image_base64", ("https://i0.example/a.png",), {"config": {}}),
    "fetch/404": ("_fetch_image_base64", ("https://i0.example/a.png",), {"config": {}, "http": (404, {})}),
    "fetch/network": ("_fetch_image_base64", ("https://i0.example/a.png",), {"config": {}, "http": ConnectionError("断网")}),
    "comment_images/ok": ("_get_comment_images", (1, 2, 1), {"config": {}, "api": {"code": 0, "data": {"root": {"content": {"pictures": [{"img_src": "a"}, {"x": 1}, {"img_src": "b"}]}}}}}),
    "comment_images/fail": ("_get_comment_images", (1, 2, 1), {"config": {}, "api": {"code": -1}}),
    "comment_images/network": ("_get_comment_images", (1, 2, 1), {"config": {}, "api": ConnectionError("断网")}),
    "recognize/empty": ("_recognize_images", ([],), {"config": IMAGE}),
    "recognize/not_configured": ("_recognize_images", (["https://a"],), {"config": {}}),
    "recognize/provider": ("_recognize_images", (["https://a", "https://b", "https://c", "https://d"],), {"config": {"IMAGE_VISION_PROVIDER_ID": "p"}, "provider": "图里有猫"}),
    "recognize/fallback_client": ("_recognize_images", (["https://a"],), {"config": {**IMAGE, "IMAGE_VISION_PROVIDER_ID": "p"}, "provider": ""}),
    "recognize/client_only": ("_recognize_images", (["https://a"],), {"config": IMAGE}),
    "recognize/long": ("_recognize_images", (["https://a"],), {"config": IMAGE, "openai": LONG}),
    "recognize/download_failed": ("_recognize_images", (["https://a"],), {"config": IMAGE, "http": (404, {})}),
    "multimodal/no_provider": ("_astrbot_multimodal_generate", ("", PARTS), {"config": {}}),
    "multimodal/ok": ("_astrbot_multimodal_generate", ("p", PARTS, 50), {"config": {}, "provider": " 回答 "}),
    "multimodal/empty": ("_astrbot_multimodal_generate", ("p", PARTS), {"config": {}, "provider": ""}),
    "multimodal/rejected": ("_astrbot_multimodal_generate", ("p", PARTS), {"config": {}, "provider": Rejected("video content not support")}),
    "search/disabled": ("_web_search", ("问题",), {"config": {"ENABLE_WEB_SEARCH": False, "WEB_SEARCH_API_KEY": "key"}}),
    "search/no_key": ("_web_search", ("问题",), {"config": {"ENABLE_WEB_SEARCH": True}}),
    "search/unknown_backend": ("_web_search", ("问题",), {"config": search("nope")}),
    "search/bad_max_results": ("_web_search", ("问题",), {"config": search("tavily", WEB_SEARCH_MAX_RESULTS="坏"), "http": (200, TAVILY)}),
    "search/network": ("_web_search", ("问题",), {"config": search("tavily"), "http": ConnectionError("断网")}),
    "search/empty_not_cached": ("_web_search", ("问题",), {"config": search("tavily"), "http": (200, {})}),
    "search/cache_hit": ("_web_search", ("问题",), {"config": search("tavily"), "http": (200, TAVILY)}, {"tavily:问题": {"ts": NOW - 100, "result": "缓存的"}}),
    "search/cache_expired": ("_web_search", ("问题",), {"config": search("tavily"), "http": (200, TAVILY)}, {"tavily:问题": {"ts": NOW - 90000, "result": "旧的"}}),
    "search/cache_evicts": ("_web_search", ("问题",), {"config": search("tavily"), "http": (200, TAVILY)}, {f"tavily:{i}": {"ts": NOW - 200 + i, "result": str(i)} for i in range(200)}),
    "tavily/ok": ("_web_search", ("问题",), {"config": search("tavily"), "http": (200, TAVILY)}),
    "tavily/answer_only": ("_web_search", ("问题",), {"config": search("tavily"), "http": (200, {"answer": "答案"})}),
    "tavily/http_error": ("_web_search", ("问题",), {"config": search("tavily"), "http": (500, {"e": 1})}),
    "firecrawl/ok": ("_web_search", ("问题",), {"config": search("firecrawl"), "http": (200, FIRECRAWL)}),
    "firecrawl/base_v2": ("_web_search", ("问题",), {"config": search("firecrawl", WEB_SEARCH_API_BASE="https://f.example/v2/"), "http": (200, {"data": FIRECRAWL["data"]["web"]})}),
    "firecrawl/base_search": ("_web_search", ("问题",), {"config": search("firecrawl", WEB_SEARCH_API_BASE="https://f.example/v2/search"), "http": (200, {"data": {"results": FIRECRAWL["data"]["web"]}})}),
    "firecrawl/http_error": ("_web_search", ("问题",), {"config": search("firecrawl"), "http": (500, {})}),
    "grok/ok": ("_web_search", ("问题",), {"config": search("grok"), "http": (200, GROK)}),
    "grok/xai_base": ("_web_search", ("问题",), {"config": search("xai", WEB_SEARCH_API_BASE="https://g.example", WEB_SEARCH_MODEL="m"), "http": (200, GROK)}),
    "grok/responses_base": ("_web_search", ("问题",), {"config": search("grok", WEB_SEARCH_API_BASE="https://g.example/v1/responses"), "http": (200, {"citations": ["https://b"]})}),
    "grok/http_error": ("_web_search", ("问题",), {"config": search("grok"), "http": (500, {})}),
    "perplexity/ok": ("_web_search", ("问题",), {"config": search("perplexity")}),
    "perplexity/empty": ("_web_search", ("问题",), {"config": search("perplexity"), "openai": None}),
    "perplexity/error": ("_web_search", ("问题",), {"config": search("perplexity"), "openai": RuntimeError("断了")}),
    "bocha/ok": ("_web_search", ("问题",), {"config": search("bocha"), "http": (200, BOCHA)}),
    "bocha/results": ("_web_search", ("问题",), {"config": search("bocha"), "http": (200, {"results": [{"title": "t", "content": "c"}]})}),
    "bocha/http_error": ("_web_search", ("问题",), {"config": search("bocha"), "http": (500, {})}),
    "custom/ok": ("_web_search", ("问题",), {"config": search("custom", WEB_SEARCH_API_BASE="https://c.example", WEB_SEARCH_MODEL="m")}),
    "custom/no_base": ("_web_search", ("问题",), {"config": search("custom", WEB_SEARCH_MODEL="m")}),
    "custom/no_model": ("_web_search", ("问题",), {"config": search("custom", WEB_SEARCH_API_BASE="https://c.example")}),
    "custom/error": ("_web_search", ("问题",), {"config": search("custom", WEB_SEARCH_API_BASE="https://c.example", WEB_SEARCH_MODEL="m"), "openai": RuntimeError("断了")}),
    "judge_video/yes": ("_should_search_for_video", (VIDEO, "上下文"), {"config": {}, "llm": JUDGE_YES}),
    "judge_video/yes_no_query": ("_should_search_for_video", (VIDEO, ""), {"config": {}, "llm": '{"need_search": true}'}),
    "judge_video/no": ("_should_search_for_video", (VIDEO, ""), {"config": {}, "llm": JUDGE_NO}),
    "judge_video/empty": ("_should_search_for_video", (VIDEO, ""), {"config": {}, "llm": ""}),
    "judge_video/error": ("_should_search_for_video", (VIDEO, ""), {"config": {}, "llm": RuntimeError("断了")}),
    "judge_reply/disabled": ("_should_search_for_reply", ("这是什么东西呀",), {"config": {"ENABLE_WEB_SEARCH": False}}),
    "judge_reply/no_key": ("_should_search_for_reply", ("这是什么东西呀",), {"config": {"ENABLE_WEB_SEARCH": True}}),
    "judge_reply/short": ("_should_search_for_reply", ("[表情]好", ), {"config": SEARCH}),
    "judge_reply/skip_word": ("_should_search_for_reply", ("火钳刘明",), {"config": SEARCH}),
    "judge_reply/only_laugh": ("_should_search_for_reply", ("哈哈哈哈哈",), {"config": SEARCH}),
    "judge_reply/yes": ("_should_search_for_reply", ("这是什么东西呀", "前文"), {"config": {**SEARCH, "WEB_SEARCH_JUDGE_PROVIDER_ID": "j"}, "llm": JUDGE_YES}),
    "judge_reply/yes_no_query": ("_should_search_for_reply", ("这是什么东西呀",), {"config": SEARCH, "llm": '{"need_search": true}'}),
    "judge_reply/no": ("_should_search_for_reply", ("这是什么东西呀",), {"config": SEARCH, "llm": JUDGE_NO}),
    "judge_reply/error": ("_should_search_for_reply", ("这是什么东西呀",), {"config": SEARCH, "llm": RuntimeError("断了")}),
}


def _plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=lambda o: f"<{type(o).__name__}>"))


class VisionSearchSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def _scene(self, name, method, args, probe_args, cache=None):
        with tempfile.TemporaryDirectory() as folder:
            cache_file = os.path.join(folder, "web_search_cache.json")
            if cache is not None:
                Path(cache_file).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            probe = Probe(**probe_args)
            probe.web_search = WebSearch(probe, JsonFile(cache_file))
            set_call_diagnostic(reason="")
            with patch("time.time", return_value=NOW), patch(
                "core.network.aiohttp.ClientSession", side_effect=lambda **_: FakeSession(probe)
            ), patch("openai.AsyncOpenAI", side_effect=lambda **kwargs: FakeOpenAI(probe, **kwargs)):
                try:
                    result = getattr(_owner(probe, method), method)(*args)
                    outcome = {"result": await result if hasattr(result, "__await__") else result}
                except Exception as exc:
                    outcome = {"raises": type(exc).__name__}
            saved = json.loads(Path(cache_file).read_text(encoding="utf-8")) if os.path.exists(cache_file) else None
            if saved is not None and len(saved) > 5:
                saved = {"count": len(saved), "first": next(iter(saved)), "last": list(saved)[-1]}
            return _plain({"calls": probe.calls, "diagnostic": get_call_diagnostic(), "cache": saved, **outcome})

    async def test_matches_snapshot(self):
        results = {name: await self._scene(name, *scene) for name, scene in SCENES.items()}
        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS") or not SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(results, json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")))

    async def test_embedding_client_is_reused(self):
        probe = Probe(config={"EMBED_API_KEY": "k", "EMBED_MODEL": "e"})
        with patch("openai.AsyncOpenAI", side_effect=lambda **kwargs: FakeOpenAI(probe, **kwargs)):
            first = probe.vision._get_embed_client()
            self.assertIs(probe.vision._get_embed_client(), first)
        self.assertIs(probe._embed_client, first)

    def test_cosine_similarity(self):
        self.assertAlmostEqual(Vision._cosine_similarity([1, 0], [1, 0]), 1)
        self.assertEqual(Vision._cosine_similarity([1], [1, 0]), 0)
        self.assertEqual(Vision._cosine_similarity([0, 0], [1, 0]), 0)

    def test_vision_call_failure_is_classified(self):
        async def go():
            probe = Probe(config={}, openai=Rejected("video content not support"))
            client = FakeOpenAI(probe)
            self.assertIsNone(await probe.vision._vision_call(client, "m", PARTS))
            self.assertEqual(get_call_diagnostic().get("rejection"), "unsupported")
        import asyncio
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
