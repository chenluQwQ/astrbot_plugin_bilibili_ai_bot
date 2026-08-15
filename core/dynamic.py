"""动态发布：文案生成、图片生成、发送。"""
import os
import re
import json
import time
import random
import base64
import io
import zipfile
import asyncio
import traceback
import hashlib
import aiohttp
from datetime import datetime
from astrbot.api import logger
from .config import (
    DEFAULT_DYNAMIC_TOPICS, DYNAMIC_LOG_FILE,
    PERMANENT_MEMORY_FILE, TEMP_IMAGE_DIR,
)
from .runtime import ActionRequest, EventPriority


class DynamicMixin:
    """B站动态发布。"""

    async def _queue_dynamic_post(self, text, handler):
        digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:20]
        date_key = datetime.now().strftime("%Y-%m-%d")
        outcome = await self.event_runtime.execute(
            ActionRequest(
                key=f"post_dynamic:{date_key}:{digest}",
                kind="post_dynamic",
                event_key=f"bilibili:proactive:dynamic:{date_key}",
                target_id=str(self.config.get("DEDE_USER_ID", "") or "self"),
                priority=EventPriority.BACKGROUND,
                metadata={"proactive": True},
            ),
            handler,
        )
        if not outcome.success and str(outcome.reason).startswith("budget_exhausted:"):
            logger.info(f"[BiliBot] 📢 统一行为预算已满，跳过动态：{outcome.reason}")
        elif not outcome.success and outcome.state == "unknown":
            logger.warning("[BiliBot] 动态发布结果未知，不会自动重发")
        return outcome.success

    def _get_image_gen_config(self):
        backend = str(self.config.get("IMAGE_GEN_BACKEND", "") or "openai").lower().strip()
        if backend in ("novelai", "nai"):
            api_key = self.config.get("IMAGE_GEN_API_KEY", "")
            base_url = str(self.config.get("IMAGE_GEN_API_BASE", "") or "https://image.novelai.net").strip().rstrip("/")
            model = str(self.config.get("IMAGE_GEN_MODEL", "") or "nai-diffusion-4-5-full").strip()
            return "novelai", api_key, base_url, model
        api_key = self.config.get("IMAGE_GEN_API_KEY", "") or self.config.get("VIDEO_VISION_API_KEY", "")
        base_url = self._normalize_openai_base_url(
            self.config.get("IMAGE_GEN_API_BASE", "") or "https://openrouter.ai/api/v1"
        )
        model = str(self.config.get("IMAGE_GEN_MODEL", "") or "black-forest-labs/flux-schnell").strip()
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
            width = max(64, min(2048, int(self.config.get("IMAGE_GEN_WIDTH", 1024) or 1024)))
            height = max(64, min(2048, int(self.config.get("IMAGE_GEN_HEIGHT", 1024) or 1024)))
            steps = max(1, min(50, int(self.config.get("IMAGE_GEN_STEPS", 28) or 28)))
            scale = max(0.0, min(10.0, float(self.config.get("IMAGE_GEN_SCALE", 5.0) or 5.0)))
        except (TypeError, ValueError):
            width, height, steps, scale = 1024, 1024, 28, 5.0
        negative_prompt = str(
            self.config.get("IMAGE_GEN_NEGATIVE_PROMPT", "")
            or "lowres, blurry, bad anatomy, bad hands, text, watermark"
        ).strip()
        parameters = {
            "params_version": 3,
            "width": width,
            "height": height,
            "scale": scale,
            "sampler": str(self.config.get("IMAGE_GEN_SAMPLER", "") or "k_euler_ancestral"),
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
        async with aiohttp.ClientSession() as s:
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

    async def _generate_image(self, prompt, human_initiated=False):
        backend, api_key, base_url, model = self._get_image_gen_config()
        if not api_key:
            logger.warning("[BiliBot] 图片生成模型未配置")
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
            async with aiohttp.ClientSession() as s:
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

    async def _generate_dynamic_content(self):
        perm = self._load_json(PERMANENT_MEMORY_FILE, [])
        perm_section = ""
        if perm:
            perm_section = "\n【你的自我认知】\n" + "\n".join([p["text"] for p in perm[-20:]])
        history_log = self._load_json(DYNAMIC_LOG_FILE, [])
        history_section = ""
        if history_log:
            recent_dynamics = [h.get("text", "") for h in history_log[-10:] if h.get("text")]
            if recent_dynamics:
                history_section = "\n【最近发过的动态（不要重复类似内容）】\n" + "\n".join([f"- {d[:50]}..." if len(d) > 50 else f"- {d}" for d in recent_dynamics])
        now = datetime.now()
        hour = now.hour
        if hour < 6:
            time_hint = "现在是深夜/凌晨"
        elif hour < 12:
            time_hint = "现在是上午"
        elif hour < 18:
            time_hint = "现在是下午"
        else:
            time_hint = "现在是晚上"
        custom_topics = self.config.get("DYNAMIC_TOPICS", [])
        topics = custom_topics if custom_topics and isinstance(custom_topics, list) else DEFAULT_DYNAMIC_TOPICS
        topic = random.choice(topics)
        sp = await self._get_system_prompt()
        prompt = f"""{sp}{perm_section}

你准备发一条B站动态。当前时段是：{time_hint}。主题方向：{topic}{history_section}

B站动态的感觉：
- 像本人忽然想起一个具体小事、感受或吐槽后随手发出来，不像在完成“发动态”任务
- 一条只说一个中心，先让读者看得懂发生了什么或你在想什么；不要故作深沉、写成谜语或强行升华
- 当前时段只是背景，确实和内容有关时再自然带到；不要为了“真实感”凭空声称自己在摸鱼、追番、出门或经历了某件事
- 不写运营口吻、鸡汤结尾、每日打卡式开场，也不要习惯性向大家提问或讨互动
- 句式和长短可以变化，通常1到2句、15到80字；没有足够内容时宁可短，不凑到固定篇幅
- 不要和最近发过的动态内容重复或相似

请以JSON格式回复：
{{"text": "动态文案（通常15-80字）", "need_image": true或false, "image_prompt": "如果need_image为true，写一段英文图片描述用于AI生图，否则留空"}}

注意：默认不配图；只有内容里确实有一个适合呈现的具体画面、且图片能补充文字时才将need_image设为true。"""
        custom_dynamic_inst = self.config.get("CUSTOM_DYNAMIC_INSTRUCTION", "")
        if custom_dynamic_inst:
            prompt += f"\n\n【补充提示词】{custom_dynamic_inst}"
        try:
            text = await self._llm_call(prompt, max_tokens=500)
            if not text:
                return None
            text = self._repair_llm_json(text)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
            logger.warning(f"[BiliBot] 动态内容JSON解析失败: {text[:100]}")
            return None
        except Exception as e:
            logger.error(f"[BiliBot] 生成动态内容失败: {e}")
            return None

    async def _run_dynamic(self, human_initiated=False):
        try:
            await self._run_dynamic_inner(human_initiated=human_initiated)
        except asyncio.CancelledError:
            logger.info("[BiliBot] 动态发布任务被取消")
        except Exception as e:
            logger.error(f"[BiliBot] 动态发布任务异常退出: {e}\n{traceback.format_exc()}")

    async def _run_dynamic_inner(self, human_initiated=False):
        logger.info("[BiliBot] 📢 开始发布动态...")
        log = self._load_json(DYNAMIC_LOG_FILE, [])
        today = datetime.now().strftime("%Y-%m-%d")
        today_posts = [l for l in log if l.get("time", "").startswith(today)]
        max_daily = max(0, int(self.config.get("DYNAMIC_DAILY_COUNT", 1)))
        autonomous_limit = max(0, int(self.config.get("AUTONOMOUS_DYNAMIC_DAILY_LIMIT", max_daily) or 0))
        if autonomous_limit:
            max_daily = min(max_daily, autonomous_limit) if max_daily else autonomous_limit
        plan = self._autonomous_plan_for_today() if hasattr(self, "_autonomous_plan_for_today") else {}
        if plan:
            max_daily = min(max_daily, len(plan.get("dynamic_times", [])))
        if len(today_posts) >= max_daily:
            logger.info(f"[BiliBot] 今天已发 {len(today_posts)} 条动态，跳过")
            return
        logger.info("[BiliBot] 🤔 正在想要发什么...")
        content = await self._generate_dynamic_content()
        if not content:
            logger.error("[BiliBot] ❌ 生成动态内容失败")
            return
        text = str(content.get("text", "") or "").strip()
        if not text:
            logger.warning("[BiliBot] 动态文案为空，跳过本次发布")
            return
        need_image = content.get("need_image", False)
        image_prompt = content.get("image_prompt", "")
        logger.info(f"[BiliBot] 📝 文案：{text[:50]}...")
        logger.info(f"[BiliBot] 🖼️ 需要图片：{need_image}")
        success = False
        if need_image and image_prompt:
            logger.info(f"[BiliBot] 🎨 生图提示：{image_prompt[:50]}...")
            local_path = await self._generate_image(image_prompt, human_initiated=human_initiated)
            if local_path:
                img_info = await self._upload_image_to_bilibili(local_path)
                if img_info:
                    success = await self._queue_dynamic_post(
                        text, lambda: self._post_dynamic_with_image(text, img_info)
                    )
                else:
                    success = await self._queue_dynamic_post(
                        text, lambda: self._post_dynamic_text(text)
                    )
                try:
                    os.remove(local_path)
                except Exception:
                    pass
            else:
                success = await self._queue_dynamic_post(
                    text, lambda: self._post_dynamic_text(text)
                )
        else:
            success = await self._queue_dynamic_post(
                text, lambda: self._post_dynamic_text(text)
            )
        if success:
            log.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": text, "has_image": need_image and bool(image_prompt), "image_prompt": image_prompt if need_image else ""})
            self._save_json(DYNAMIC_LOG_FILE, log[-100:])
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            short_text = text[:60] if len(text) > 60 else text
            await self._save_self_memory_record("dynamic", f"[{now_str}] Bot发了一条动态：{short_text}", source="bilibili", memory_type="dynamic")
            logger.info("[BiliBot] 🎉 动态发布完成！")
        else:
            logger.error("[BiliBot] ❌ 动态发布失败")
