"""向量和多模态模型：建客户端、发请求、识图。不懂业务，也不读写 ``data/``。

``host`` 提供 ``config``、``settings``、``context``，客户端也缓存在它身上
（``core/network.py`` 的 ``close_plugin_clients`` 从那里关）。它就是插件对象本身。
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import logging
import math

import aiohttp
from astrbot.api import logger

from ..network import openai_proxy_kwargs, plugin_http_session, redact_proxy_error
from ..video_diagnostics import exception_detail, safe_detail, set_call_diagnostic
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
    "unauthorized", "invalid api key", "invalid_api_key", "authentication",
    "forbidden", "permission", "model not found", "does not exist",
    "no such model", "model_not_found", "insufficient", "quota", "余额",
)
_TOO_LARGE_HINTS = ("too large", "payload", "413", "exceeds", "limit exceeded", "过长", "过大")


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
    try:
        head = base64.b64decode(str(b64 or "")[:32])
    except Exception:
        return "image/jpeg"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"GIF8":
        return "image/gif"
    return "image/jpeg"


def _has_media(content_parts):
    return isinstance(content_parts, list) and any(
        isinstance(part, dict) and part.get("type") not in (None, "text") for part in content_parts
    )


def classify_vision_rejection(exc):
    """把视觉调用的失败归成 unsupported / unavailable / too_large / other。

    只做字符串与状态码判断，不发探针请求；目的是让调用方决定「跳过阶段」还是「换个格式再试」。
    """
    status = getattr(exc, "status_code", None)
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
    if any(hint in text for hint in _UNSUPPORTED_HINTS):
        return "unsupported"
    return "other"


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

    def _get_embed_client(self):
        if self._embed_client is None:
            api_key = self.settings.get("EMBED_API_KEY")
            if not api_key:
                return None
            from openai import AsyncOpenAI
            base_url = normalize_openai_base_url(self.settings.get("EMBED_API_BASE"))
            self._embed_client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=self._embed_timeout(),
                max_retries=0,
                **openai_proxy_kwargs(self.config),
            )
        return self._embed_client

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
        if self._video_vision_client is None:
            api_key = self.settings.get("VIDEO_VISION_API_KEY")
            if not api_key:
                return None
            from openai import AsyncOpenAI
            base_url = normalize_openai_base_url(self.settings.get("VIDEO_VISION_API_BASE"))
            self._video_vision_client = AsyncOpenAI(api_key=api_key, base_url=base_url, **openai_proxy_kwargs(self.config))
        return self._video_vision_client

    def _get_image_vision_client(self):
        if self._image_vision_client is None:
            api_key = self.settings.get("IMAGE_VISION_API_KEY")
            if not api_key:
                return None
            from openai import AsyncOpenAI
            base_url = normalize_openai_base_url(self.settings.get("IMAGE_VISION_API_BASE"))
            self._image_vision_client = AsyncOpenAI(api_key=api_key, base_url=base_url, **openai_proxy_kwargs(self.config))
        return self._image_vision_client

    async def _vision_call(self, client, model, content_parts, max_tokens=250, system_prompt=""):
        """通用视觉模型调用。``system_prompt``：看完直接写评论时带上人设。"""
        set_call_diagnostic(reason="视觉模型返回空内容", model=safe_detail(model, self.config, 100))
        try:
            messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
            messages.append({"role": "user", "content": content_parts})
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip() if resp.choices else None
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
                        return base64.b64encode(data).decode()
        except Exception as e:
            logger.error(f"[BiliBot] 图片下载失败: {e}")
        return None

    async def _recognize_images(self, image_urls):
        """用视觉模型识别评论中的图片"""
        if not image_urls:
            return ""
        client = self._get_image_vision_client()
        provider_id = self.settings.get("IMAGE_VISION_PROVIDER_ID")
        model = self.settings.get("IMAGE_VISION_MODEL")
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
            result = await self._astrbot_multimodal_generate(provider_id, content, max_tokens=100)
            if not result and client and model:
                result = await self._vision_call(client, model, content, max_tokens=100)
            # 视觉模型偶尔会忽略“50字以内”的提示；只保留当前图片的短摘要，
            # 避免图像描述在后续回复上下文中无限放大 token 占用。
            result = str(result or "").strip()
            return result if len(result) <= 500 else result[:500].rstrip() + "…"
        except Exception as e:
            logger.error(f"[BiliBot] 图片识别失败: {e}")
            return ""

    async def _astrbot_multimodal_generate(self, provider_id, content_parts, max_tokens=250, system_prompt=""):
        """通过 AstrBot provider 进行多模态调用。``system_prompt``：看完直接写评论时带上人设。"""
        set_call_diagnostic(reason="视觉提供商未配置" if not provider_id else "视觉模型返回空内容", provider=safe_detail(provider_id, self.config, 100))
        if not provider_id:
            return None
        watch = None
        token = None
        if _has_media(content_parts):
            _watch_media_drops()
            watch = {}
            token = _media_drop_watch.set(watch)
        try:
            kwargs = {"chat_provider_id": provider_id, "prompt": content_parts, "max_tokens": max_tokens}
            if system_prompt:
                kwargs["system_prompt"] = system_prompt
            resp = await self.context.llm_generate(**kwargs)
            if watch and watch.get("dropped"):
                why = watch.get("why") or "附件被拒"
                reason = f"AstrBot 把图片/视频删了、只用文字重试（{why}），模型没看到画面，这次的回答不用"
                # 被审核拦是这一段内容的事，换一段可能就过；其余是这个模型或报文形状收不了。
                reject = "other" if "moderat" in why else "unsupported"
                set_call_diagnostic(reason=reason, provider=safe_detail(provider_id, self.config, 100), rejection=reject)
                logger.warning(f"[BiliBot] {reason}")
                return None
            if resp and resp.completion_text:
                return resp.completion_text.strip()
        except Exception as e:
            reject = classify_vision_rejection(e)
            reason = exception_detail(e, self.config)
            set_call_diagnostic(reason=reason, provider=safe_detail(provider_id, self.config, 100), rejection=reject)
            logger.warning(f"[BiliBot] AstrBot 多模态 provider 调用失败：{reason}")
        finally:
            if token is not None:
                _media_drop_watch.reset(token)
        return None
