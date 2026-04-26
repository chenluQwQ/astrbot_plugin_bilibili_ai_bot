"""Embedding、视觉模型调用、图片识别。"""
import math
import base64
import aiohttp
from astrbot.api import logger


class VisionMixin:
    """Embedding 和多模态视觉模型。"""

    # ── Embedding ──
    def _get_embed_client(self):
        if self._embed_client is None:
            api_key = self.config.get("EMBED_API_KEY", "")
            if not api_key:
                return None
            from openai import AsyncOpenAI
            base_url = self.config.get("EMBED_API_BASE", "https://api.siliconflow.cn/v1")
            self._embed_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
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
            logger.error(f"[BiliBot] Embedding 失败: {e}")
            return None

    @staticmethod
    def _cosine_similarity(a, b):
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
            base_url = self.config.get("VIDEO_VISION_API_BASE", "https://api.siliconflow.cn/v1")
            self._video_vision_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        return self._video_vision_client

    def _get_image_vision_client(self):
        if self._image_vision_client is None:
            api_key = self.config.get("IMAGE_VISION_API_KEY", "")
            if not api_key:
                return None
            from openai import AsyncOpenAI
            base_url = self.config.get("IMAGE_VISION_API_BASE", "https://api.siliconflow.cn/v1")
            self._image_vision_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        return self._image_vision_client

    async def _vision_call(self, client, model, content_parts, max_tokens=250):
        """通用视觉模型调用"""
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content_parts}],
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip() if resp.choices else None
        except Exception as e:
            logger.error(f"[BiliBot] 视觉模型调用失败: {e}")
            return None

    async def _fetch_image_base64(self, url):
        """下载图片并转 base64"""
        try:
            if not url.startswith("http"):
                url = "https:" + url
            async with aiohttp.ClientSession() as s:
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
            return result or ""
        except Exception as e:
            logger.error(f"[BiliBot] 图片识别失败: {e}")
            return ""

    async def _astrbot_multimodal_generate(self, provider_id, content_parts, max_tokens=250):
        """通过 AstrBot provider 进行多模态调用"""
        if not provider_id:
            return None
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=content_parts,
            )
            if resp and resp.completion_text:
                return resp.completion_text.strip()
        except Exception as e:
            logger.warning(f"[BiliBot] AstrBot 多模态 provider 调用失败({provider_id})：{e}")
        return None
