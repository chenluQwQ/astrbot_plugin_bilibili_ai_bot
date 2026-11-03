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

    async def _fetch_opus_post(self, opus_id):
        """拉取图文帖子（opus）的标题、正文与全部图片 URL。

        返回 {"title": str, "text": str, "images": [url, ...]}；失败返回 None。
        opus_id 来自 @ 通知 item 的 uri（如 https://www.bilibili.com/opus/<id>）。
        """
        if not opus_id:
            return None
        try:
            d, _ = await self._http_get(
                "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail",
                params={"id": opus_id},
            )
            if d.get("code") != 0:
                return None
            item = d.get("data", {}).get("item") or {}
            title = ""
            text_parts = []
            images = []
            for m in item.get("modules", []) or []:
                mt = m.get("module_type", "")
                if mt == "MODULE_TYPE_TITLE":
                    title = (m.get("module_title") or {}).get("text", "") or ""
                elif mt == "MODULE_TYPE_CONTENT":
                    for p in (m.get("module_content") or {}).get("paragraphs", []) or []:
                        txt = p.get("text")
                        if txt:
                            text_parts.append(str(txt).strip())
                        pic = p.get("pic") or {}
                        for pp in pic.get("pics", []) or []:
                            u = pp.get("url")
                            if u:
                                images.append(u)
            if not title and not text_parts and not images:
                return None
            return {
                "title": str(title).strip(),
                "text": "\n".join(t for t in text_parts if t).strip(),
                "images": images,
            }
        except Exception as e:
            logger.warning(f"[BiliBot] 帖子详情拉取失败(opus={opus_id}): {e}")
            return None

    async def _recognize_images(self, image_urls):
        """用视觉模型识别评论/帖子中的图片。

        多张图会先拼成一张宫格图再整体识别一次，减少调用次数与 token 占用。
        优先使用图片视觉模型（IMAGE_VISION_*）；未配置时回退复用视频视觉
        模型（VIDEO_VISION_*），两者底层都是多模态 OpenAI 客户端，图片输入通用。
        """
        if not image_urls:
            return ""
        client = self._get_image_vision_client()
        provider_id = self.config.get("IMAGE_VISION_PROVIDER_ID", "")
        model = self.config.get("IMAGE_VISION_MODEL", "")
        if not provider_id and (not client or not model):
            # 回退：复用视频视觉模型
            provider_id = self.config.get("VIDEO_VISION_PROVIDER_ID", "")
            client = self._get_video_vision_client()
            model = self.config.get("VIDEO_VISION_MODEL", "")
        if not provider_id and (not client or not model):
            return ""
        try:
            b64 = await self._compose_image_grid_b64(image_urls)
            if not b64:
                return ""
            content = [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": "这是同一篇内容里的多张图片（已拼成一张）。请用50字以内概括它们整体表达了什么。"},
            ]
            # 直连视觉客户端可用时优先直连（如 IMAGE_VISION_API_KEY 直连百炼 qwen3-vl-plus），
            # 避免先空跑一次 provider 通道；否则走 AstrBot provider（provider_id）。
            result = None
            if client and model:
                result = await self._vision_call(client, model, content, max_tokens=100)
            if not result and provider_id:
                result = await self._astrbot_multimodal_generate(provider_id, content, max_tokens=100)
            # 视觉模型偶尔会忽略“50字以内”的提示；只保留当前图片的短摘要，
            # 避免图像描述在后续回复上下文中无限放大 token 占用。
            result = str(result or "").strip()
            return result if len(result) <= 500 else result[:500].rstrip() + "…"
        except Exception as e:
            logger.error(f"[BiliBot] 图片识别失败: {e}")
            return ""

    async def _compose_image_grid_b64(self, image_urls, max_cells=12):
        """下载多张图片，拼成一张宫格图，返回 base64。

        单图直接返回其 base64；多图按接近方阵排列（列数=ceil(sqrt(n))），
        每格等比缩放到不超过 512×512 并居中贴到灰底格子上，最后整张转 base64。
        图片下载失败会被跳过，不影响其余图片。
        """
        if not image_urls:
            return ""
        urls = [u for u in image_urls if u][:max_cells]
        if not urls:
            return ""

        # 单图直接走原图 base64，不额外拼
        if len(urls) == 1:
            return await self._fetch_image_base64(urls[0])

        # 下载全部图片到内存
        import io
        imgs = []
        for u in urls:
            b64 = await self._fetch_image_base64(u)
            if not b64:
                continue
            try:
                from PIL import Image as PILImage
                raw = base64.b64decode(b64)
                imgs.append(PILImage.open(io.BytesIO(raw)).convert("RGB"))
            except Exception as e:
                logger.warning(f"[BiliBot] 拼图：单张图片解码失败，跳过: {e}")
        if not imgs:
            return ""
        if len(imgs) == 1:
            buf = io.BytesIO()
            imgs[0].save(buf, "JPEG", quality=85)
            return base64.b64encode(buf.getvalue()).decode()

        from PIL import Image as PILImage
        import math
        cols = int(math.ceil(math.sqrt(len(imgs))))
        rows = int(math.ceil(len(imgs) / cols))
        cell_w = cell_h = 512
        cells = []
        for im in imgs:
            im.thumbnail((cell_w, cell_h), PILImage.LANCZOS)
            cell = PILImage.new("RGB", (cell_w, cell_h), (18, 18, 22))
            cell.paste(im, ((cell_w - im.width) // 2, (cell_h - im.height) // 2))
            cells.append(cell)
        while len(cells) < cols * rows:
            cells.append(PILImage.new("RGB", (cell_w, cell_h), (18, 18, 22)))
        canvas = PILImage.new("RGB", (cols * cell_w, rows * cell_h), (18, 18, 22))
        for idx, cell in enumerate(cells):
            r, c = divmod(idx, cols)
            canvas.paste(cell, (c * cell_w, r * cell_h))
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()

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
