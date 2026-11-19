"""向量和多模态模型：建客户端、发请求、识图。不懂业务，也不读写 ``data/``。

``host`` 提供 ``config``、``settings``、``context``，客户端也缓存在它身上
（``core/network.py`` 的 ``close_plugin_clients`` 从那里关）。它就是插件对象本身。
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import logging
import math
import re

import aiohttp
from astrbot.api import logger

from ..network import openai_proxy_kwargs, plugin_http_session, redact_proxy_error
from ..video_diagnostics import (
    exception_detail, exception_status_code, get_call_diagnostic,
    merge_call_diagnostics, safe_detail, set_call_diagnostic,
)
from .text import normalize_openai_base_url


# 服务端明确拒绝「这种 content 不吃」的信号。命中即视为模型能力问题，
# 不值得对同一段视频反复重试，直接跳过视频直读阶段。
_UNSUPPORTED_HINTS = (
    "does not support", "don't support", "not support", "unsupported",
    "unknown variant", "unrecognized content", "unknown content",
    "invalid content type", "list of dicts", "not a valid content",
)
# AstrBot 的 provider 链路上没有「视频 part」，把 mp4 塞进任何形状都会被它内部拒掉，
# 而且这两种失败都不带 HTTP 状态码，只能靠文本识别：
#   * 原生适配器：image_url 里的 mp4 会被严格图片解析拒绝，
#     抛 ValueError: Invalid image file: data URI mime='video/mp4' payload_len=…
#   * 原生适配器：video_url / file part 会落进它的音频分支，
#     抛 KeyError: 'audio_url'
# 两者都是确定性的「这个报文形状它不吃」，重试同一段视频没有意义。
_ASTRBOT_PART_REJECTIONS = (
    "invalid image file",
    "data uri mime=",
    "'audio_url'",
)
# 只有「内容/消息结构被拒」才算能力问题，否则普通参数报错会把整个阶段误拉黑。
_CONTENT_WORDS = ("content", "message", "part", "modal", "video", "image")
_REJECT_WORDS = ("invalid", "unsupported", "not support", "unknown", "unexpected", "not a valid", "expected")
# 鉴权/模型不存在：连截帧也没戏，整段视觉分析都该停。
_UNAVAILABLE_HINTS = (
    "unauthorized", "invalid api key", "invalid_api_key", "invalid api-key",
    "api key not valid", "authentication", "forbidden", "permission",
    "permission_denied", "access denied", "model not found", "does not exist",
    "no such model", "model_not_found", "requested entity was not found",
    "entity was not found", "insufficient", "quota", "余额",
)
_TOO_LARGE_HINTS = ("too large", "payload", "413", "exceeds", "limit exceeded", "过长", "过大")
_UNAMBIGUOUS_TOO_LARGE_HINTS = (
    "too large", "payload too large", "request entity too large",
    "file too large", "413", "exceeds", "limit exceeded", "过长", "过大",
)
_VISION_CALL_TIMEOUT = 90
_THINK_TAG_RE = re.compile(r"<(think|thinking)>.*?</\1>", re.DOTALL)
_TRAILING_THINK_RE = re.compile(r"</(?:think|thinking)>\s*$")
_DATA_URI_RE = re.compile(r"^data:([^;,]+);base64,(.+)$", re.IGNORECASE | re.DOTALL)


# AstrBot 的通用接口收到「附件不对 / 模型不是多模态 / 图片被审核拦了」这类报错时，会把图片和
# image_url 形状的视频删掉，只用文字重试，日志里只有一条：
#   检测到图片请求失败（invalid_attachment），已移除图片并重试（保留文本内容）。
# 返回值看不出来，模型拿到的只剩文字，会照着标题和简介编。插件只能靠这条日志知道，
# 所以调用时在当前任务上挂一个标记，AstrBot 记这条日志时把标记打上。
_MEDIA_DROP_MARKERS = ("已移除图片并重试",)
_media_drop_watch: contextvars.ContextVar = contextvars.ContextVar("bilibot_media_drop_watch", default=None)
_MEDIA_DROP_HANDLER_NAME = "bilibot_media_drop"


class _MediaDropHandler(logging.Handler):
    def emit(self, record):
        watch = _media_drop_watch.get()
        if watch is None:
            return
        try:
            message = record.getMessage()
        except Exception:
            return
        if any(marker in message for marker in _MEDIA_DROP_MARKERS):
            watch["dropped"] = True
            args = record.args if isinstance(record.args, tuple) else ()
            watch["why"] = str(args[0]) if args else ""


def _watch_media_drops():
    """在 AstrBot 的日志上挂一次；插件重载时先摘掉旧的那个。"""
    root = logging.getLogger("astrbot")
    for handler in list(root.handlers):
        if getattr(handler, "name", "") == _MEDIA_DROP_HANDLER_NAME:
            if isinstance(handler, _MediaDropHandler):
                return
            root.removeHandler(handler)
    handler = _MediaDropHandler(level=logging.WARNING)
    handler.name = _MEDIA_DROP_HANDLER_NAME
    root.addHandler(handler)


def _image_mime(b64):
    """按文件头认图片格式；评论图常有 png、webp、gif，一律写 jpeg 会被严格的接口拒掉。"""
    return _detect_image_mime_from_b64(b64)


def _has_media(content_parts):
    return isinstance(content_parts, list) and any(
        isinstance(part, dict) and part.get("type") not in (None, "text") for part in content_parts
    )


def classify_vision_rejection(exc):
    """把视觉调用的失败归成 unsupported / unavailable / too_large / other。

    只做字符串与状态码判断，不发探针请求；目的是让调用方决定「跳过阶段」还是「换个格式再试」。
    """
    status = exception_status_code(exc)
    text = str(exc or "").lower()
    if isinstance(status, int):
        if status in (401, 403, 404):
            return "unavailable"
        if status == 413:
            return "too_large"
        if status in (400, 415, 422):
            if any(hint in text for hint in _UNAVAILABLE_HINTS):
                return "unavailable"
            # 排在 too_large 之前：AstrBot 的拒收文本里带 payload_len=…，否则会被误判成体积超限。
            if any(hint in text for hint in _ASTRBOT_PART_REJECTIONS):
                return "unsupported"
            if any(hint in text for hint in _TOO_LARGE_HINTS):
                return "too_large"
            if any(hint in text for hint in _UNSUPPORTED_HINTS):
                return "unsupported"
            if any(word in text for word in _CONTENT_WORDS) and any(word in text for word in _REJECT_WORDS):
                return "unsupported"
            return "other"
    if any(hint in text for hint in _UNAVAILABLE_HINTS):
        return "unavailable"
    if any(hint in text for hint in _ASTRBOT_PART_REJECTIONS):
        return "unsupported"
    if any(hint in text for hint in _UNAMBIGUOUS_TOO_LARGE_HINTS):
        return "too_large"
    if any(hint in text for hint in _UNSUPPORTED_HINTS):
        return "unsupported"
    return "other"


def detect_image_mime(data: bytes, default: str = "image/jpeg") -> str:
    """根据图片魔数识别常见图片 MIME，未知时回退默认值。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return default


def _detect_image_mime_from_b64(b64: str) -> str:
    """只解码 base64 前缀几个字节识别图片 MIME，避免重复解码整张图片。"""
    prefix = "".join(str(b64 or "")[:32].split())
    usable = prefix[: (len(prefix) // 4) * 4]
    if not usable:
        return "image/jpeg"
    try:
        head = base64.b64decode(usable, validate=False)
    except Exception:
        return "image/jpeg"
    return detect_image_mime(head, default="image/jpeg")


def _parse_data_uri(url: str) -> tuple[str, str] | None:
    match = _DATA_URI_RE.match(str(url or "").strip())
    if not match:
        return None
    mime = match.group(1).strip().lower()
    b64 = "".join(match.group(2).split())
    return (mime, b64) if mime and b64 else None


def _extract_video_part_data(part: dict) -> tuple[str, str] | None:
    """从 OpenAI/兼容风格的内容块提取视频 (mime_type, base64)；不是视频块返回 None。"""
    if not isinstance(part, dict):
        return None
    ptype = part.get("type")
    if ptype == "video_url":
        url = (part.get("video_url") or {}).get("url")
        return _parse_data_uri(url)
    if ptype == "file":
        url = (part.get("file") or {}).get("file_data")
        return _parse_data_uri(url)
    if ptype == "image_url":
        url = (part.get("image_url") or {}).get("url")
        parsed = _parse_data_uri(url)
        if parsed and parsed[0].startswith("video/"):
            return parsed
    return None


def _has_video_parts(content_parts) -> bool:
    return isinstance(content_parts, list) and any(
        _extract_video_part_data(part) is not None for part in content_parts if isinstance(part, dict)
    )


def _extract_chat_completion_text(resp) -> str | None:
    """兼容字符串与多模态列表形式的 completion content，空值安全。"""
    choices = getattr(resp, "choices", None)
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if content is None:
        return None
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict):
                item_type = str(item.get("type") or "").lower()
                if item_type in {"think", "thinking"}:
                    continue
                if item_type == "text" or (not item_type and "text" in item):
                    val = item.get("text")
                    if val is not None:
                        chunks.append(str(val))
            elif isinstance(item, str):
                chunks.append(item)
            else:
                val = getattr(item, "text", None)
                if val is not None and not getattr(item, "thought", False):
                    chunks.append(str(val))
        text = "".join(chunks)
    else:
        text = str(content)
    text = _THINK_TAG_RE.sub("", text)
    text = _TRAILING_THINK_RE.sub("", text).strip()
    return text or None


def _client_fingerprint(*parts) -> str:
    payload = "\0".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _schedule_close_client(client) -> None:
    close_fn = getattr(client, "close", None)
    if client is None or not callable(close_fn):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _close():
        try:
            await close_fn()
        except Exception:
            pass

    loop.create_task(_close())


def _provider_adapter_type(provider) -> str:
    meta_fn = getattr(provider, "meta", None)
    if callable(meta_fn):
        try:
            meta = meta_fn()
            ptype = getattr(meta, "type", None)
            if ptype:
                return str(ptype).lower()
        except Exception:
            pass
    cfg = getattr(provider, "provider_config", None)
    if isinstance(cfg, dict) and cfg.get("type"):
        return str(cfg["type"]).lower()
    return ""


def _is_gemini_native_provider(provider, ptype: str | None = None) -> bool:
    ptype = _provider_adapter_type(provider) if ptype is None else ptype
    if ptype == "googlegenai_chat_completion":
        return True
    client = getattr(provider, "client", None)
    models = getattr(client, "models", None)
    return callable(getattr(models, "generate_content", None))


def _is_openai_chat_provider(provider, ptype: str | None = None) -> bool:
    ptype = _provider_adapter_type(provider) if ptype is None else ptype
    if ptype == "openai_responses" or "anthropic" in ptype:
        return False
    client = getattr(provider, "client", None)
    completions = getattr(getattr(client, "chat", None), "completions", None)
    return callable(getattr(completions, "create", None))


def _decode_b64_bytes(b64: str) -> bytes:
    return base64.b64decode(b64)


def _encode_b64_str(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _on_host(name):
    return property(lambda self: getattr(self._host, name), lambda self, value: setattr(self._host, name, value))


class Vision:
    _embed_client = _on_host("_embed_client")
    _video_vision_client = _on_host("_video_vision_client")
    _image_vision_client = _on_host("_image_vision_client")

    def __init__(self, host):
        self._host = host

    @property
    def config(self):
        return self._host.config

    @property
    def settings(self):
        return self._host.settings

    @property
    def context(self):
        return self._host.context

    def _get_astrbot_provider(self, provider_id):
        pid = str(provider_id or "").strip()
        if not pid:
            return None
        ctx = getattr(self._host, "context", None)
        getter = getattr(ctx, "get_provider_by_id", None) if ctx is not None else None
        if not callable(getter):
            return None
        try:
            return getter(pid)
        except Exception:
            return None

    def provider_fingerprint(self, provider_id) -> str:
        """返回 AstrBot provider 当前配置的安全指纹（不含明文密钥），用于探测缓存失效。"""
        provider = self._get_astrbot_provider(provider_id)
        if provider is None:
            return ""
        ptype = _provider_adapter_type(provider)
        model = provider.get_model() if callable(getattr(provider, "get_model", None)) else ""
        cfg = getattr(provider, "provider_config", None) or {}
        api_base = getattr(provider, "api_base", None) or cfg.get("api_base") or ""
        key = provider.get_current_key() if callable(getattr(provider, "get_current_key", None)) else ""
        proxy = cfg.get("proxy") or ""
        return _client_fingerprint(ptype, model, api_base, key, proxy)[:12]

    def supports_native_video_only(self, provider_id) -> bool:
        """判断指定 AstrBot provider 是否走原生多模态协议（与 OpenAI 形态探测无关）。"""
        provider = self._get_astrbot_provider(provider_id)
        return provider is not None and _is_gemini_native_provider(provider)

    def _vision_timeout(self, provider=None) -> int:
        raw = getattr(provider, "timeout", None) if provider is not None else None
        try:
            if raw is not None:
                return max(_VISION_CALL_TIMEOUT, min(300, int(raw)))
        except (TypeError, ValueError):
            pass
        return _VISION_CALL_TIMEOUT

    def _get_or_rebuild_openai_client(self, attr_name, fp_attr, api_key, base_url, *, timeout):
        key = str(api_key or "").strip()
        existing = getattr(self._host, attr_name, None)
        if not key:
            if existing is not None:
                _schedule_close_client(existing)
                setattr(self._host, attr_name, None)
            setattr(self._host, fp_attr, None)
            return None
        normalized_base = normalize_openai_base_url(base_url)
        proxy = str(self.config.get("PROXY_URL") or "").strip()
        fp = _client_fingerprint(key, normalized_base, proxy, timeout)
        existing_fp = getattr(self._host, fp_attr, None)
        if existing is not None and existing_fp == fp:
            return existing
        if existing is not None:
            _schedule_close_client(existing)
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=key,
            base_url=normalized_base,
            timeout=timeout,
            max_retries=0,
            **openai_proxy_kwargs(self.config),
        )
        setattr(self._host, attr_name, client)
        setattr(self._host, fp_attr, fp)
        return client

    def _get_embed_client(self):
        return self._get_or_rebuild_openai_client(
            "_embed_client",
            "_embed_client_fingerprint",
            self.settings.get("EMBED_API_KEY"),
            self.settings.get("EMBED_API_BASE"),
            timeout=self._embed_timeout(),
        )

    def _embed_timeout(self):
        try:
            return max(3, min(60, int(self.settings.get("EMBED_TIMEOUT_SECONDS") or 10)))
        except (TypeError, ValueError):
            return 10

    async def _host_embedding(self, provider_id, text):
        """用 AstrBot 里配好的向量模型。找不到、出错、维度不对都返回 None，这次就不做向量检索。"""
        try:
            provider = self.context.get_provider_by_id(provider_id)
            if not callable(getattr(provider, "get_embedding", None)):
                logger.warning(f"[BiliBot] 没找到向量模型 {provider_id}，本次跳过向量检索")
                return None
            vector = await asyncio.wait_for(provider.get_embedding(text), self._embed_timeout())
            if not vector:
                return None
            # 没配维度的向量模型 get_dim() 是 0，这时不按维度拦。
            get_dim = getattr(provider, "get_dim", None)
            expected = get_dim() if callable(get_dim) else 0
            if expected and len(vector) != expected:
                logger.warning(f"[BiliBot] 向量维度 {len(vector)} 和模型配置的 {expected} 不一致，本次跳过向量检索")
                return None
            return list(vector)
        except Exception as e:
            logger.warning(f"[BiliBot] Embedding 暂不可用，本次跳过向量检索: {redact_proxy_error(e, self.config)}")
            return None

    async def _get_embedding(self, text):
        provider_id = str(self.settings.get("EMBED_PROVIDER_ID") or "").strip()
        if provider_id:
            return await self._host_embedding(provider_id, text)
        client = self._get_embed_client()
        if not client:
            return None
        try:
            embed_model = self.settings.get("EMBED_MODEL")
            resp = await client.embeddings.create(model=embed_model, input=text)
            return resp.data[0].embedding
        except Exception as e:
            logger.warning(f"[BiliBot] Embedding 暂不可用，本次跳过向量检索: {redact_proxy_error(e, self.config)}")
            return None

    @staticmethod
    def _cosine_similarity(a, b):
        if len(a) != len(b):
            # 维度不同说明来自不同 embedding 模型，相似度无意义
            return 0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0

    def _get_video_vision_client(self):
        return self._get_or_rebuild_openai_client(
            "_video_vision_client",
            "_video_vision_client_fingerprint",
            self.settings.get("VIDEO_VISION_API_KEY"),
            self.settings.get("VIDEO_VISION_API_BASE"),
            timeout=_VISION_CALL_TIMEOUT,
        )

    def _get_image_vision_client(self):
        return self._get_or_rebuild_openai_client(
            "_image_vision_client",
            "_image_vision_client_fingerprint",
            self.settings.get("IMAGE_VISION_API_KEY"),
            self.settings.get("IMAGE_VISION_API_BASE"),
            timeout=_VISION_CALL_TIMEOUT,
        )

    async def _vision_call(self, client, model, content_parts, max_tokens=250, system_prompt=""):
        """通用视觉模型调用。``system_prompt``：看完直接写评论时带上人设。"""
        set_call_diagnostic(reason="视觉模型返回空内容", model=safe_detail(model, self.config, 100))
        try:
            messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
            messages.append({"role": "user", "content": content_parts})
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                ),
                timeout=self._vision_timeout(),
            )
            return _extract_chat_completion_text(resp)
        except Exception as e:
            reject = classify_vision_rejection(e)
            reason = exception_detail(e, self.config)
            set_call_diagnostic(reason=reason, model=safe_detail(model, self.config, 100), rejection=reject)
            logger.error(f"[BiliBot] 视觉模型调用失败: {reason}")
            return None

    async def _fetch_image_base64(self, url):
        """下载图片并转 base64"""
        try:
            if not url.startswith("http"):
                url = "https:" + url
            async with plugin_http_session(self.config) as s:
                async with s.get(
                    url,
                    headers={"Referer": "https://www.bilibili.com"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        data = await r.read()
                        return await asyncio.to_thread(_encode_b64_str, data)
        except Exception as e:
            logger.error(f"[BiliBot] 图片下载失败: {e}")
        return None

    async def _recognize_images(self, image_urls):
        """用视觉模型识别评论与动态中的图片"""
        if not image_urls:
            return ""
        client = self._get_image_vision_client()
        provider_id = str(self.settings.get("IMAGE_VISION_PROVIDER_ID") or "").strip()
        model = str(self.settings.get("IMAGE_VISION_MODEL") or "").strip()
        if not provider_id and (not client or not model):
            return ""
        try:
            content = []
            for url in image_urls[:4]:
                b64 = await self._fetch_image_base64(url)
                if b64:
                    content.append({"type": "image_url", "image_url": {"url": f"data:{_image_mime(b64)};base64,{b64}"}})
            if not content:
                return ""
            content.append({"type": "text", "text": "请用50字以内描述这些图片的内容。"})
            result = None
            first_diag = {}
            if provider_id or not (client and model):
                result = await self._astrbot_multimodal_generate(provider_id, content, max_tokens=100)
                if not result and provider_id:
                    first_diag = get_call_diagnostic()
            if not result and client and model:
                result = await self._vision_call(client, model, content, max_tokens=100)
                if not result and first_diag:
                    set_call_diagnostic(**merge_call_diagnostics(first_diag, get_call_diagnostic()))
            # 视觉模型偶尔会忽略“50字以内”的提示；只保留当前图片的短摘要，
            # 避免图像描述在后续回复上下文中无限放大 token 占用。
            result = str(result or "").strip()
            return result if len(result) <= 500 else result[:500].rstrip() + "…"
        except Exception as e:
            logger.error(f"[BiliBot] 图片识别失败: {e}")
            return ""

    async def _call_gemini_native_provider(self, provider, content_parts, max_tokens, system_prompt, timeout):
        """通过原生多模态 SDK 客户端直接发送视频/图片字节，绕开 AstrBot 严格图片拦截。"""
        from google.genai import types

        genai_parts = []
        for item in content_parts:
            if not isinstance(item, dict):
                continue
            ptype = item.get("type")
            if ptype == "text":
                genai_parts.append(types.Part.from_text(text=str(item.get("text") or " ")))
                continue
            video_data = _extract_video_part_data(item)
            if video_data is not None:
                mime_type, b64 = video_data
                raw_bytes = await asyncio.to_thread(_decode_b64_bytes, b64)
                genai_parts.append(types.Part.from_bytes(data=raw_bytes, mime_type=mime_type))
                continue
            if ptype == "image_url":
                parsed = _parse_data_uri((item.get("image_url") or {}).get("url"))
                if parsed is not None:
                    mime_type, b64 = parsed
                    raw_bytes = await asyncio.to_thread(_decode_b64_bytes, b64)
                    genai_parts.append(types.Part.from_bytes(data=raw_bytes, mime_type=mime_type))

        model_name = (
            provider.get_model()
            if callable(getattr(provider, "get_model", None))
            else str((getattr(provider, "provider_config", {}) or {}).get("model") or "")
        )
        prepare_cfg = getattr(provider, "_prepare_query_config", None)

        async def _build_config(sys_inst):
            if callable(prepare_cfg):
                return await prepare_cfg(
                    {"model": model_name, "max_tokens": max_tokens},
                    system_instruction=sys_inst or None,
                )
            return types.GenerateContentConfig(
                system_instruction=sys_inst or None,
                max_output_tokens=max_tokens,
            )

        sys_inst = system_prompt or None
        contents = [types.UserContent(parts=genai_parts)]
        for attempt in range(2):
            config = await _build_config(sys_inst)
            try:
                resp = await asyncio.wait_for(
                    provider.client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=config,
                    ),
                    timeout=timeout,
                )
                break
            except Exception as exc:
                if attempt == 0 and sys_inst and "Developer instruction is not enabled" in str(exc):
                    contents = [types.UserContent(parts=[types.Part.from_text(text=sys_inst), *genai_parts])]
                    sys_inst = None
                    continue
                raise

        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            content = getattr(candidates[0], "content", None)
            parts = getattr(content, "parts", None) or []
            if parts:
                text = "".join(
                    p.text
                    for p in parts
                    if isinstance(getattr(p, "text", None), str) and not getattr(p, "thought", False)
                ).strip()
                return text or None
        fallback_text = getattr(resp, "text", None)
        return str(fallback_text).strip() or None if fallback_text else None

    async def _call_openai_provider_client(self, provider, content_parts, max_tokens, system_prompt, timeout):
        """通过 OpenAI 兼容提供商的底层客户端直发多模态内容，避免错误时被静默去图。"""
        messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
        messages.append({"role": "user", "content": content_parts})
        model_name = (
            provider.get_model()
            if callable(getattr(provider, "get_model", None))
            else str((getattr(provider, "provider_config", {}) or {}).get("model") or "")
        )
        create_kwargs = {
            "model": model_name,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        custom_extra_body = (getattr(provider, "provider_config", {}) or {}).get("custom_extra_body")
        if isinstance(custom_extra_body, dict) and custom_extra_body:
            create_kwargs["extra_body"] = custom_extra_body
        resp = await asyncio.wait_for(
            provider.client.chat.completions.create(**create_kwargs),
            timeout=timeout,
        )
        return _extract_chat_completion_text(resp)

    async def _astrbot_multimodal_generate(self, provider_id, content_parts, max_tokens=250, system_prompt=""):
        """通过 AstrBot provider 进行多模态调用。``system_prompt``：看完直接写评论时带上人设。"""
        pid = str(provider_id or "").strip()
        set_call_diagnostic(reason="视觉提供商未配置" if not pid else "视觉模型返回空内容", provider=safe_detail(pid, self.config, 100))
        if not pid:
            return None
        watch = None
        token = None
        if _has_media(content_parts):
            _watch_media_drops()
            watch = {}
            token = _media_drop_watch.set(watch)
        try:
            provider = self._get_astrbot_provider(pid)
            timeout = self._vision_timeout(provider)
            if provider is not None and _has_video_parts(content_parts):
                ptype = _provider_adapter_type(provider)
                if _is_gemini_native_provider(provider, ptype):
                    return await self._call_gemini_native_provider(
                        provider, content_parts, max_tokens, system_prompt, timeout
                    )
                if ptype == "openai_responses" or "anthropic" in ptype:
                    set_call_diagnostic(
                        reason="当前 AstrBot 提供商适配器不支持直传视频片段",
                        provider=safe_detail(pid, self.config, 100),
                        rejection="unsupported",
                    )
                    return None
                if _is_openai_chat_provider(provider, ptype):
                    return await self._call_openai_provider_client(
                        provider, content_parts, max_tokens, system_prompt, timeout
                    )
            kwargs = {"chat_provider_id": pid, "prompt": content_parts, "max_tokens": max_tokens}
            if system_prompt:
                kwargs["system_prompt"] = system_prompt
            resp = await asyncio.wait_for(self.context.llm_generate(**kwargs), timeout=timeout)
            if watch and watch.get("dropped"):
                why = watch.get("why") or "附件被拒"
                reason = f"AstrBot 把图片/视频删了、只用文字重试（{why}），模型没看到画面，这次的回答不用"
                # 被审核拦是这一段内容的事，换一段可能就过；其余是这个模型或报文形状收不了。
                reject = "other" if "moderat" in why else "unsupported"
                set_call_diagnostic(reason=reason, provider=safe_detail(pid, self.config, 100), rejection=reject)
                logger.warning(f"[BiliBot] {reason}")
                return None
            text = getattr(resp, "completion_text", None) if resp else None
            if isinstance(text, str) and text.strip():
                return text.strip()
            if text is not None and not isinstance(text, str):
                cleaned = str(text).strip()
                return cleaned or None
        except Exception as e:
            reject = classify_vision_rejection(e)
            reason = exception_detail(e, self.config)
            set_call_diagnostic(reason=reason, provider=safe_detail(pid, self.config, 100), rejection=reject)
            logger.warning(f"[BiliBot] AstrBot 多模态 provider 调用失败：{reason}")
        finally:
            if token is not None:
                _media_drop_watch.reset(token)
        return None
