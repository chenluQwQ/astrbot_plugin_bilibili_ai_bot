"""配图：调生图服务、下载视频封面，结果存成临时文件交给调用方。

只管和外面打交道：要不要配图、配完发到哪是发动态自己的事。
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import zipfile

import aiohttp
from astrbot.api import logger

from ..config import TEMP_IMAGE_DIR
from ..network import plugin_http_session
from .host_view import HostView
from .text import normalize_openai_base_url


class ImageGen(HostView):
    HOST_API = frozenset({"settings", "config"})

    def config_tuple(self):
        backend = str(self.settings.get("IMAGE_GEN_BACKEND") or "openai").lower().strip()
        if backend in ("novelai", "nai"):
            api_key = self.settings.get("IMAGE_GEN_API_KEY")
            base_url = str(self.settings.get("IMAGE_GEN_API_BASE") or "https://image.novelai.net").strip().rstrip("/")
            model = str(self.settings.get("IMAGE_GEN_MODEL") or "nai-diffusion-4-5-full").strip()
            return "novelai", api_key, base_url, model
        api_key = self.settings.get("IMAGE_GEN_API_KEY")
        configured_base = str(self.settings.get("IMAGE_GEN_API_BASE") or "").strip()
        base_url = normalize_openai_base_url(configured_base) if configured_base else ""
        model = str(self.settings.get("IMAGE_GEN_MODEL") or "").strip()
        return "openai", api_key, base_url, model

    @staticmethod
    def _decode_novelai_image(body: bytes, content_type: str) -> bytes:
        """Decode NovelAI's JSON/base64 or ZIP image response."""
        content_type = str(content_type or "").lower()
        if "json" in content_type:
            data = json.loads(body.decode("utf-8"))
            images = data.get("images", []) if isinstance(data, dict) else []
            if not images:
                return b""
            encoded = images[0].get("image", "") if isinstance(images[0], dict) else images[0]
            return base64.b64decode(encoded) if encoded else b""
        if body.startswith(b"PK") or "zip" in content_type:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                names = [
                    name for name in archive.namelist()
                    if not name.endswith("/") and name.lower().endswith((".png", ".webp", ".jpg", ".jpeg"))
                ]
                return archive.read(names[0]) if names else b""
        if content_type.startswith("image/"):
            return body
        return b""

    async def _generate_novelai_image(self, prompt, api_key, base_url, model):
        if base_url.lower().endswith("/ai/generate-image"):
            url = base_url
        else:
            url = f"{base_url}/ai/generate-image"
        try:
            width = max(64, min(2048, int(self.settings.get("IMAGE_GEN_WIDTH") or 1024)))
            height = max(64, min(2048, int(self.settings.get("IMAGE_GEN_HEIGHT") or 1024)))
            steps = max(1, min(50, int(self.settings.get("IMAGE_GEN_STEPS") or 28)))
            scale = max(0.0, min(10.0, float(self.settings.get("IMAGE_GEN_SCALE") or 5.0)))
        except (TypeError, ValueError):
            width, height, steps, scale = 1024, 1024, 28, 5.0
        negative_prompt = str(
            self.settings.get("IMAGE_GEN_NEGATIVE_PROMPT")
            or "lowres, blurry, bad anatomy, bad hands, text, watermark"
        ).strip()
        parameters = {
            "params_version": 3,
            "width": width,
            "height": height,
            "scale": scale,
            "sampler": str(self.settings.get("IMAGE_GEN_SAMPLER") or "k_euler_ancestral"),
            "steps": steps,
            "n_samples": 1,
            "ucPreset": 0,
            "qualityToggle": True,
            "dynamic_thresholding": False,
            "cfg_rescale": 0,
            "noise_schedule": "karras",
            "negative_prompt": negative_prompt,
            "image_format": "png",
        }
        if "diffusion-4" in model:
            parameters.update({
                "v4_prompt": {
                    "caption": {"base_caption": prompt, "char_captions": []},
                    "use_coords": False,
                    "use_order": True,
                },
                "v4_negative_prompt": {
                    "caption": {"base_caption": negative_prompt, "char_captions": []},
                    "legacy_uc": False,
                },
            })
        token = str(api_key).strip()
        auth_value = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        payload = {"input": prompt, "model": model, "action": "generate", "parameters": parameters}
        headers = {
            "Authorization": auth_value,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        async with plugin_http_session(self.config) as s:
            async with s.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as r:
                body = await r.read()
                if r.status not in (200, 201):
                    message = body.decode("utf-8", errors="ignore")
                    logger.error(f"[BiliBot] NovelAI 生图 HTTP {r.status}: {message[:300]}")
                    return None
                img_data = self._decode_novelai_image(body, r.headers.get("Content-Type", ""))
        if not img_data:
            logger.warning("[BiliBot] NovelAI 生图返回中没有可用图片")
            return None
        save_path = os.path.join(TEMP_IMAGE_DIR, f"dynamic_{int(time.time())}.png")
        with open(save_path, "wb") as f:
            f.write(img_data)
        logger.info(f"[BiliBot] 🖼️ NovelAI 图片生成成功（{len(img_data) // 1024}KB）")
        return save_path

    async def generate(self, prompt, human_initiated=False):
        backend, api_key, base_url, model = self.config_tuple()
        if not api_key or not base_url or not model:
            logger.warning("[BiliBot] 图片生成配置不完整，请填写独立密钥、接口地址和模型名称；本次跳过配图")
            return None
        styled_prompt = f"anime style illustration, not photorealistic, soft lighting, beautiful colors: {prompt}"
        if backend == "novelai":
            if not human_initiated:
                logger.warning("[BiliBot] NovelAI 官方要求生图由真人操作触发；本次定时动态跳过配图并降级为纯文字")
                return None
            try:
                return await self._generate_novelai_image(styled_prompt, api_key, base_url, model)
            except Exception as e:
                logger.error(f"[BiliBot] NovelAI 图片生成异常: {e}")
                return None
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": [{"role": "user", "content": styled_prompt}], "modalities": ["image"]}
        try:
            async with plugin_http_session(self.config) as s:
                async with s.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120)) as r:
                    if r.status != 200:
                        logger.error(f"[BiliBot] 图片生成HTTP错误: {r.status}")
                        return None
                    data = await r.json()
            if "error" in data:
                logger.error(f"[BiliBot] 图片生成API错误: {data['error']}")
                return None
            message = data.get("choices", [{}])[0].get("message", {})
            images = message.get("images", [])
            if images:
                img_item = images[0]
                if isinstance(img_item, dict):
                    img_url = img_item.get("url", "") or img_item.get("b64_json", "") or (img_item.get("image_url", {}) or {}).get("url", "")
                else:
                    img_url = str(img_item)
                if img_url.startswith("data:image"):
                    img_b64 = img_url.split(",", 1)[1]
                    img_data = base64.b64decode(img_b64)
                    save_path = os.path.join(TEMP_IMAGE_DIR, f"dynamic_{int(time.time())}.png")
                    with open(save_path, "wb") as f:
                        f.write(img_data)
                    logger.info(f"[BiliBot] 🖼️ 图片生成成功（{len(img_data) // 1024}KB）")
                    return save_path
            content = message.get("content", "")
            if isinstance(content, str) and "data:image" in content:
                match = re.search(r'data:image/\w+;base64,([A-Za-z0-9+/=]+)', content)
                if match:
                    img_data = base64.b64decode(match.group(1))
                    save_path = os.path.join(TEMP_IMAGE_DIR, f"dynamic_{int(time.time())}.png")
                    with open(save_path, "wb") as f:
                        f.write(img_data)
                    logger.info(f"[BiliBot] 🖼️ 图片生成成功（{len(img_data) // 1024}KB）")
                    return save_path
            logger.warning("[BiliBot] 图片生成返回无图片")
            return None
        except Exception as e:
            logger.error(f"[BiliBot] 图片生成异常: {e}")
            return None

    async def download_cover(self, pic_url):
        url = str(pic_url or "").strip()
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        if not re.match(r"^https://[a-z0-9.-]+\.hdslb\.com/", url):
            logger.warning("[BiliBot] 视频封面地址不是B站图床，跳过封面配图")
            return None
        try:
            async with plugin_http_session(self.config) as s:
                async with s.get(url, headers={"Referer": "https://www.bilibili.com"},
                                 timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status != 200:
                        logger.warning(f"[BiliBot] 视频封面下载失败 HTTP {r.status}")
                        return None
                    body = await r.read()
        except Exception as e:
            logger.warning(f"[BiliBot] 视频封面下载异常: {e}")
            return None
        if not body or len(body) > 20 * 1024 * 1024:
            return None
        save_path = os.path.join(TEMP_IMAGE_DIR, f"dynamic_cover_{int(time.time())}.jpg")
        with open(save_path, "wb") as f:
            f.write(body)
        return save_path
