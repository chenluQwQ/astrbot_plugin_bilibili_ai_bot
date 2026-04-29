"""基础工具方法：HTTP请求、JSON读写、进程管理、环境检测、临时文件清理。"""
import os
import json
import shutil
import asyncio
import aiohttp
from astrbot.api import logger
from .config import DATA_DIR, TEMP_IMAGE_DIR, TEMP_VIDEO_DIR, USER_AGENT


class UtilsMixin:
    """提供所有模块共用的底层工具方法。"""

    # ── 数据目录 ──
    def _ensure_data_dir(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
        os.makedirs(TEMP_VIDEO_DIR, exist_ok=True)
        self._cleanup_temp_files()

    # ── Cookie / Header ──
    def _has_cookie(self):
        return bool(self.config.get("SESSDATA", ""))

    def _headers(self):
        cookie_parts = (
            f"SESSDATA={self.config.get('SESSDATA', '')}; "
            f"bili_jct={self.config.get('BILI_JCT', '')}; "
            f"DedeUserID={self.config.get('DEDE_USER_ID', '')}"
        )
        buvid3 = self.config.get("BUVID3", "")
        if buvid3:
            cookie_parts += f"; buvid3={buvid3}"
        buvid4 = self.config.get("BUVID4", "")
        if buvid4:
            cookie_parts += f"; buvid4={buvid4}"
        return {
            "Cookie": cookie_parts,
            "User-Agent": USER_AGENT,
            "Referer": "https://www.bilibili.com",
            "Accept-Encoding": "gzip, deflate",
        }

    # ── JSON 持久化 ──
    def _load_json(self, path, default=None):
        if default is None:
            default = {}
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return default

    def _save_json(self, path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 外部命令 ──
    def _find_command(self, command_name):
        return shutil.which(command_name)

    # ── HTTP 请求 ──
    async def _http_get(self, url, headers=None, params=None, timeout=10, retries=2):
        last_err = None
        for i in range(retries + 1):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        url,
                        headers=headers or self._headers(),
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as r:
                        return await r.json(content_type=None), r
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as e:
                last_err = e
                if i < retries:
                    await asyncio.sleep(0.5 * (i + 1))
                    continue
                raise
        raise last_err

    async def _http_post(self, url, headers=None, data=None, timeout=10, retries=2):
        last_err = None
        for i in range(retries + 1):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        url,
                        headers=headers or self._headers(),
                        data=data,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as r:
                        return await r.json(content_type=None), r
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as e:
                last_err = e
                if i < retries:
                    await asyncio.sleep(0.5 * (i + 1))
                    continue
                raise
        raise last_err

    async def _http_get_text(self, url, headers=None, params=None, timeout=10, retries=2):
        last_err = None
        for i in range(retries + 1):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        url,
                        headers=headers or self._headers(),
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as r:
                        return await r.text(), r
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError) as e:
                last_err = e
                if i < retries:
                    await asyncio.sleep(0.5 * (i + 1))
                    continue
                raise
        raise last_err

    async def _run_process(self, *args, timeout=300):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning(f"[BiliBot] 命令不存在：{args[0]}")
            return 127, "", f"{args[0]} not found"
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.warning(f"[BiliBot] 命令执行超时：{' '.join(args[:3])} ...")
            return 124, "", "timeout"
        return (
            proc.returncode,
            stdout.decode("utf-8", errors="ignore"),
            stderr.decode("utf-8", errors="ignore"),
        )

    # ── 环境状态检测 ──
    def _get_environment_status(self):
        video_provider_id = self.config.get("VIDEO_VISION_PROVIDER_ID", "")
        image_provider_id = self.config.get("IMAGE_VISION_PROVIDER_ID", "")
        video_api_ready = bool(self.config.get("VIDEO_VISION_API_KEY", "") and self.config.get("VIDEO_VISION_MODEL", ""))
        image_api_ready = bool(self.config.get("IMAGE_VISION_API_KEY", "") and self.config.get("IMAGE_VISION_MODEL", ""))
        image_gen_ready = bool(self.config.get("IMAGE_GEN_API_KEY", "") and self.config.get("IMAGE_GEN_MODEL", ""))
        deps = {
            "yt-dlp": bool(self._find_command("yt-dlp")),
            "ffmpeg": bool(self._find_command("ffmpeg")),
            "ffprobe": bool(self._find_command("ffprobe")),
        }
        proactive_media_ready = deps["yt-dlp"] and deps["ffmpeg"] and deps["ffprobe"]
        return {
            "python": {"aiohttp": True, "cryptography": True, "lunardate": True, "openai": True, "Pillow": True, "qrcode": True, "yt-dlp": True},
            "external_commands": deps,
            "llm": {
                "chat_provider": bool(self.config.get("LLM_PROVIDER_ID", "")),
                "video_provider": bool(video_provider_id),
                "video_api": video_api_ready,
                "image_provider": bool(image_provider_id),
                "image_api": image_api_ready,
                "image_gen_api": image_gen_ready,
            },
            "features": {
                "video_cover_analysis": bool(video_provider_id or video_api_ready or self.config.get("LLM_PROVIDER_ID", "")),
                "image_recognition": bool(image_provider_id or image_api_ready),
                "proactive_video_media": proactive_media_ready and bool(video_provider_id or video_api_ready),
                "proactive_video_fallback_text": bool(self.config.get("LLM_PROVIDER_ID", "")),
                "dynamic_image_generation": image_gen_ready or bool(self.config.get("VIDEO_VISION_API_KEY", "")),
                "web_search": bool(self.config.get("ENABLE_WEB_SEARCH", False) and self.config.get("WEB_SEARCH_API_KEY", "")),
                "web_search_backend": (self.config.get("WEB_SEARCH_BACKEND", "") or "tavily").lower().strip() if self.config.get("ENABLE_WEB_SEARCH", False) else "",
                "web_search_judge": bool(self.config.get("WEB_SEARCH_JUDGE_PROVIDER_ID", "")),
            },
        }

    def _log_environment_warnings(self):
        env = self._get_environment_status()
        missing_commands = [name for name, ok in env["external_commands"].items() if not ok]
        if missing_commands:
            logger.warning(
                "[BiliBot] 缺少外部命令: %s。主动看视频将无法执行视频直读/截帧分析，会回退为纯文本分析或直接跳过。",
                ", ".join(missing_commands),
            )
        if not env["llm"]["chat_provider"]:
            logger.warning("[BiliBot] 未配置 LLM_PROVIDER_ID。文本回复/评价/纯文本回退将依赖 AstrBot 当前默认聊天模型。")
        if not (env["llm"]["video_provider"] or env["llm"]["video_api"]):
            logger.warning("[BiliBot] 未配置视频视觉模型。视频相关分析将退回纯文本概括。")
        if not (env["llm"]["image_provider"] or env["llm"]["image_api"]):
            logger.warning("[BiliBot] 未配置图片识别模型。评论图片识别功能将不可用。")
        if self.config.get("ENABLE_WEB_SEARCH", False):
            ws_backend = (self.config.get("WEB_SEARCH_BACKEND", "") or "tavily").lower()
            ws_key = self.config.get("WEB_SEARCH_API_KEY", "")
            if not ws_key:
                logger.warning("[BiliBot] 联网搜索已启用但未配置 WEB_SEARCH_API_KEY。")
            else:
                logger.info(f"[BiliBot] 🔍 联网搜索已启用，后端: {ws_backend}")

    # ── 临时文件清理 ──
    def _cleanup_temp_files(self):
        cleaned = 0
        for temp_dir in (TEMP_IMAGE_DIR, TEMP_VIDEO_DIR):
            if not os.path.isdir(temp_dir):
                continue
            for name in os.listdir(temp_dir):
                fp = os.path.join(temp_dir, name)
                try:
                    if os.path.isfile(fp):
                        os.remove(fp)
                        cleaned += 1
                    elif os.path.isdir(fp):
                        shutil.rmtree(fp)
                        cleaned += 1
                except OSError:
                    pass
        qr_path = os.path.join(DATA_DIR, "login_qr.png")
        if os.path.exists(qr_path):
            try:
                os.remove(qr_path)
                cleaned += 1
            except OSError:
                pass
        if cleaned:
            logger.info(f"[BiliBot] 🗑️ 清理了 {cleaned} 个临时文件")

    @staticmethod
    def _repair_llm_json(text):
        """修复LLM返回的各种JSON格式问题"""
        import re
        # 去 markdown 包裹
        text = text.replace("```json", "").replace("```", "").strip()
        # 中文引号 → 安全字符
        text = text.replace('\u201c', "'").replace('\u201d', "'")
        text = text.replace('\u2018', "'").replace('\u2019', "'")
        text = text.replace('\uff02', "'")  # 全角双引号
        # 去掉尾逗号
        text = re.sub(r',\s*([}\]])', r'\1', text)
        # 提取JSON对象
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            text = m.group()
        return text
