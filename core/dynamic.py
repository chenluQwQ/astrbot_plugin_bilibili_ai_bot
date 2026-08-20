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
    DAILY_SUMMARY_FILE, DEFAULT_DYNAMIC_TOPICS, DYNAMIC_LOG_FILE,
    PERMANENT_MEMORY_FILE, TEMP_IMAGE_DIR, WATCH_LOG_FILE, WEEKLY_SUMMARY_FILE,
)
from .runtime import ActionRequest, EventPriority
from .content_protocol import (
    ContentProtocolError, DYNAMIC_SCHEMA_PROMPT, parse_dynamic_content,
)


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

    def _dynamic_grounding_context(self):
        """Return concrete same-day reasons that may justify an automatic post."""
        today = datetime.now().strftime("%Y-%m-%d")
        reasons = []
        if hasattr(self, "_get_today_mood"):
            mood, mood_reason = self._get_today_mood()
            mood_reason = re.sub(r"\s+", " ", str(mood_reason or "")).strip()
            if mood_reason:
                reasons.append(f"当前情绪：{mood}；缘由：{mood_reason[:120]}")
        watched = [
            item for item in self._load_json(WATCH_LOG_FILE, [])
            if isinstance(item, dict) and str(item.get("time") or "").startswith(today)
        ]
        def score_of(value):
            try:
                return float(value.get("score", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                return 0.0

        for item in sorted(watched, key=score_of, reverse=True)[:3]:
            try:
                score = float(item.get("score", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                score = 0
            if score < 8:
                continue
            title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()[:60]
            review = re.sub(r"\s+", " ", str(item.get("review") or item.get("comment") or "")).strip()[:120]
            if title and review and review not in {"评价失败", "没什么感觉", "未知"}:
                reasons.append(f"今天很喜欢的视频《{title}》：{review}")
        daily_records = self._load_json(DAILY_SUMMARY_FILE, [])
        for record in reversed(daily_records if isinstance(daily_records, list) else []):
            if str(record.get("date") or "") == today:
                summary = re.sub(r"\s+", " ", str(record.get("summary") or "")).strip()
                if summary and not summary.startswith("（今日无"):
                    reasons.append(f"今日日记：{summary[:180]}")
                break
        week = datetime.now().strftime("%G-W%V")
        weekly_records = self._load_json(WEEKLY_SUMMARY_FILE, [])
        for record in reversed(weekly_records if isinstance(weekly_records, list) else []):
            if str(record.get("week") or "") == week and str(record.get("time") or "").startswith(today):
                summary = re.sub(r"\s+", " ", str(record.get("summary") or "")).strip()
                if summary and not summary.startswith("（本周无"):
                    reasons.append(f"今天的周回顾片段：{summary[:180]}")
                break
        return reasons[:6]

    async def _generate_dynamic_content(self, human_initiated=False):
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
        if not topics:
            topics = ["只在今天确实有具体事情想说时，挑一个片段随口记下"]
        topic = random.choice(topics)
        sp = await self._get_system_prompt()
        grounding = self._dynamic_grounding_context()
        grounding_text = "\n".join(f"- {item}" for item in grounding) or "- 今天没有足够具体的活动或情绪缘由"
        prompt = f"""你准备判断现在是否值得发一条B站动态。当前时段是：{time_hint}。管理员给的方向：{topic}{perm_section}{history_section}

今天可以作为依据的真实片段：
{grounding_text}
触发方式：{'管理员刚刚手动要求发动态' if human_initiated else '后台定时时刻到达'}

B站动态的感觉：
- 自动触发时，只有明显情绪、特别喜欢的视频、刚完成的周回顾或其他具体经历值得说，才选择post；定时时刻本身不是发布理由
- 没有真实内容就选择skip，不使用泛泛话题、当前时间或“今天也要努力”等句子凑一条
- 管理员手动触发时可以围绕给定方向创作，但仍不能伪造经历
- 像本人忽然想起一个具体小事、感受或吐槽后随手发出来，不像在完成“发动态”任务
- 一条只说一个中心，先让读者看得懂发生了什么或你在想什么；不要故作深沉、写成谜语或强行升华
- 当前时段只是背景，确实和内容有关时再自然带到；不要为了“真实感”凭空声称自己在摸鱼、追番、出门或经历了某件事
- 不写运营口吻、鸡汤结尾、每日打卡式开场，也不要习惯性向大家提问或讨互动
- 句式和长短可以变化，通常1到2句、15到80字；没有足够内容时宁可短，不凑到固定篇幅
- 不要和最近发过的动态内容重复或相似

注意：默认不配图；只有内容里确实有一个适合呈现的具体画面、且图片能补充文字时才将need_image设为true。
{DYNAMIC_SCHEMA_PROMPT}"""
        custom_dynamic_inst = self.config.get("CUSTOM_DYNAMIC_INSTRUCTION", "")
        if custom_dynamic_inst:
            prompt += f"\n\n【补充提示词】{custom_dynamic_inst}"
        try:
            text = await self._llm_call(prompt, system_prompt=sp, max_tokens=500)
            if not text:
                return None
            try:
                return parse_dynamic_content(text)
            except ContentProtocolError as exc:
                logger.warning(f"[BiliBot] 动态内容结构校验失败，本次不发: {exc}")
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
        autonomous_limit = self._autonomous_limit_max("dynamic") if hasattr(self, "_autonomous_limit_max") else max(0, int(self.config.get("AUTONOMOUS_DYNAMIC_DAILY_LIMIT", max_daily) or 0))
        if autonomous_limit:
            max_daily = min(max_daily, autonomous_limit) if max_daily else autonomous_limit
        plan = self._autonomous_plan_for_today() if hasattr(self, "_autonomous_plan_for_today") else {}
        if plan:
            max_daily = min(max_daily, len(plan.get("dynamic_times", [])))
        if len(today_posts) >= max_daily:
            logger.info(f"[BiliBot] 今天已发 {len(today_posts)} 条动态，跳过")
            return
        logger.info("[BiliBot] 🤔 正在想要发什么...")
        content = await self._generate_dynamic_content(human_initiated=human_initiated)
        if not content:
            logger.error("[BiliBot] ❌ 生成动态内容失败")
            return
        if content.get("decision") != "post":
            logger.info("[BiliBot] 今天没有足够具体的动态发布动机，本次跳过")
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
