"""Embedding、视觉模型调用、图片识别。"""
import math
import base64
import aiohttp
from .network import plugin_http_session, openai_proxy_kwargs, redact_proxy_error
from astrbot.api import logger
from .video_diagnostics import set_call_diagnostic, exception_detail, safe_detail


# 服务端明确拒绝「这种 content 不吃」的信号。命中即视为模型能力问题，
# 不值得对同一段视频反复重试，直接跳过视频直读阶段。
_UNSUPPORTED_HINTS = (
    "does not support", "don't support", "not support", "unsupported",
    "unknown variant", "unrecognized content", "unknown content",
    "invalid content type", "list of dicts", "not a valid content",
)
# AstrBot 的 provider 链路上没有「视频 part」，把 mp4 塞进任何形状都会被它内部拒掉，
# 而且这两种失败都不带 HTTP 状态码，只能靠文本识别：
#   * 原生 Gemini 适配器：image_url 里的 mp4 会被严格图片解析拒绝，
#     抛 ValueError: Invalid image file: data URI mime='video/mp4' payload_len=…
#   * 原生 Gemini 适配器：video_url / file part 会落进它的音频分支，
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


class VisionMixin:
    """Embedding 和多模态视觉模型。"""

    # ── Embedding ──
    def _get_embed_client(self):
        if self._embed_client is None:
            api_key = self.config.get("EMBED_API_KEY", "")
            if not api_key:
                return None
            from openai import AsyncOpenAI
            base_url = self._normalize_openai_base_url(self.config.get("EMBED_API_BASE", "https://api.siliconflow.cn/v1"))
            try:
                timeout = max(
                    3,
                    min(60, int(self.config.get("EMBED_TIMEOUT_SECONDS", 10) or 10)),
                )
            except (TypeError, ValueError):
                timeout = 10
            self._embed_client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                max_retries=0,
                **openai_proxy_kwargs(self.config),
            )
        return self._embed_client

    async def _get_embedding(self, text):
        client = self._get_embed_client()
        if not client:
            return None
        try:
            embed_model = self.config.get("EMBED_MODEL", "BAAI/bge-m3")
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

    # ── 视觉模型客户端 ──
    def _get_video_vision_client(self):
        if self._video_vision_client is None:
            api_key = self.config.get("VIDEO_VISION_API_KEY", "")
            if not api_key:
                return None
            from openai import AsyncOpenAI
            base_url = self._normalize_openai_base_url(self.config.get("VIDEO_VISION_API_BASE", "https://api.siliconflow.cn/v1"))
            self._video_vision_client = AsyncOpenAI(api_key=api_key, base_url=base_url, **openai_proxy_kwargs(self.config))
        return self._video_vision_client

    def _get_image_vision_client(self):
        if self._image_vision_client is None:
            api_key = self.config.get("IMAGE_VISION_API_KEY", "")
            if not api_key:
                return None
            from openai import AsyncOpenAI
            base_url = self._normalize_openai_base_url(self.config.get("IMAGE_VISION_API_BASE", "https://api.siliconflow.cn/v1"))
            self._image_vision_client = AsyncOpenAI(api_key=api_key, base_url=base_url, **openai_proxy_kwargs(self.config))
        return self._image_vision_client

    async def _vision_call(self, client, model, content_parts, max_tokens=250):
        """通用视觉模型调用"""
        set_call_diagnostic(reason="视觉模型返回空内容", model=safe_detail(model, self.config, 100))
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content_parts}],
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

    async def _get_comment_images(self, oid, rpid, comment_type):
        """获取评论中的图片 URL 列表"""
        try:
            d, _ = await self._http_get(
                "https://api.bilibili.com/x/v2/reply/detail",
                params={"oid": oid, "type": comment_type, "root": rpid},
            )
            if d["code"] != 0:
                return []
            content = d.get("data", {}).get("root", {}).get("content", {})
            pictures = content.get("pictures", [])
            return [p["img_src"] for p in pictures if "img_src" in p]
        except Exception:
            return []

    async def _recognize_images(self, image_urls):
        """用视觉模型识别评论中的图片"""
        if not image_urls:
            return ""
        client = self._get_image_vision_client()
        provider_id = self.config.get("IMAGE_VISION_PROVIDER_ID", "")
        model = self.config.get("IMAGE_VISION_MODEL", "")
        if not provider_id and (not client or not model):
            return ""
        try:
            content = []
            for url in image_urls[:3]:
                b64 = await self._fetch_image_base64(url)
                if b64:
                    content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
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

    async def _astrbot_multimodal_generate(self, provider_id, content_parts, max_tokens=250):
        """通过 AstrBot provider 进行多模态调用"""
        set_call_diagnostic(reason="视觉提供商未配置" if not provider_id else "视觉模型返回空内容", provider=safe_detail(provider_id, self.config, 100))
        if not provider_id:
            return None
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=content_parts,
                max_tokens=max_tokens,
            )
            if resp and resp.completion_text:
                return resp.completion_text.strip()
        except Exception as e:
            reject = classify_vision_rejection(e)
            reason = exception_detail(e, self.config)
            set_call_diagnostic(reason=reason, provider=safe_detail(provider_id, self.config, 100), rejection=reject)
            logger.warning(f"[BiliBot] AstrBot 多模态 provider 调用失败：{reason}")
        return None
