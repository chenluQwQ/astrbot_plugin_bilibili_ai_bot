"""动态发布：文案生成、图片生成、发送。"""
import os
import re
import json
import time
import random
import base64
import asyncio
import traceback
import aiohttp
from datetime import datetime
from astrbot.api import logger
from .config import (
    DEFAULT_DYNAMIC_TOPICS, DYNAMIC_LOG_FILE,
    PERMANENT_MEMORY_FILE, TEMP_IMAGE_DIR,
)


class DynamicMixin:
    """B站动态发布。"""

    def _get_image_gen_config(self):
        api_key = self.config.get("IMAGE_GEN_API_KEY", "") or self.config.get("VIDEO_VISION_API_KEY", "")
        base_url = self.config.get("IMAGE_GEN_API_BASE", "https://openrouter.ai/api/v1")
        model = self.config.get("IMAGE_GEN_MODEL", "black-forest-labs/flux-schnell")
        return api_key, base_url, model

    async def _generate_image(self, prompt):
        api_key, base_url, model = self._get_image_gen_config()
        if not api_key:
            logger.warning("[BiliBot] 图片生成模型未配置")
            return None
        styled_prompt = f"anime style illustration, not photorealistic, soft lighting, beautiful colors: {prompt}"
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
        sp = self._get_system_prompt()
        prompt = f"""{sp}{perm_section}

{time_hint}，你想发一条B站动态。主题方向：{topic}{history_section}

风格要求：
- 说话自然有网感，像真人发的动态
- 结合当前时间（{time_hint}）写出真实感
- 不要和最近发过的动态内容重复或相似

请以JSON格式回复：
{{"text": "动态文案（50-150字，自然随意）", "need_image": true或false, "image_prompt": "如果need_image为true，写一段英文图片描述用于AI生图，否则留空"}}

注意：动态文案要有个性，不要像AI写的。不是每次都需要图片。"""
        try:
            text = await self._llm_call(prompt, max_tokens=500)
            if not text:
                return None
            text = text.replace("```json", "").replace("```", "").strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                match = re.search(r'\{.*\}', text, re.DOTALL)
                if match:
                    try:
                        return json.loads(match.group())
                    except Exception:
                        pass
            logger.warning(f"[BiliBot] 动态内容JSON解析失败: {text[:100]}")
            return None
        except Exception as e:
            logger.error(f"[BiliBot] 生成动态内容失败: {e}")
            return None

    async def _run_dynamic(self):
        try:
            await self._run_dynamic_inner()
        except asyncio.CancelledError:
            logger.info("[BiliBot] 动态发布任务被取消")
        except Exception as e:
            logger.error(f"[BiliBot] 动态发布任务异常退出: {e}\n{traceback.format_exc()}")

    async def _run_dynamic_inner(self):
        logger.info("[BiliBot] 📢 开始发布动态...")
        log = self._load_json(DYNAMIC_LOG_FILE, [])
        today = datetime.now().strftime("%Y-%m-%d")
        today_posts = [l for l in log if l.get("time", "").startswith(today)]
        max_daily = self.config.get("DYNAMIC_DAILY_COUNT", 1)
        if len(today_posts) >= max_daily:
            logger.info(f"[BiliBot] 今天已发 {len(today_posts)} 条动态，跳过")
            return
        logger.info("[BiliBot] 🤔 正在想要发什么...")
        content = await self._generate_dynamic_content()
        if not content:
            logger.error("[BiliBot] ❌ 生成动态内容失败")
            return
        text = content.get("text", "")
        need_image = content.get("need_image", False)
        image_prompt = content.get("image_prompt", "")
        logger.info(f"[BiliBot] 📝 文案：{text[:50]}...")
        logger.info(f"[BiliBot] 🖼️ 需要图片：{need_image}")
        success = False
        if need_image and image_prompt:
            logger.info(f"[BiliBot] 🎨 生图提示：{image_prompt[:50]}...")
            local_path = await self._generate_image(image_prompt)
            if local_path:
                img_info = await self._upload_image_to_bilibili(local_path)
                if img_info:
                    success = await self._post_dynamic_with_image(text, img_info)
                else:
                    success = await self._post_dynamic_text(text)
                try:
                    os.remove(local_path)
                except Exception:
                    pass
            else:
                success = await self._post_dynamic_text(text)
        else:
            success = await self._post_dynamic_text(text)
        if success:
            log.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": text, "has_image": need_image and bool(image_prompt), "image_prompt": image_prompt if need_image else ""})
            self._save_json(DYNAMIC_LOG_FILE, log[-100:])
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            short_text = text[:60] if len(text) > 60 else text
            await self._save_self_memory_record("dynamic", f"[{now_str}] Bot发了一条动态：{short_text}", source="bilibili", memory_type="dynamic")
            logger.info("[BiliBot] 🎉 动态发布完成！")
        else:
            logger.error("[BiliBot] ❌ 动态发布失败")
