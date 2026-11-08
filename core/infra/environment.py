"""运行环境：外部命令和各类模型配没配，启动时把缺的打成警告。"""
from __future__ import annotations

from astrbot.api import logger

from .host_view import HostView


class Environment(HostView):
    HOST_API = frozenset({"processes", "settings"})

    def _get_environment_status(self):
        video_provider_id = self.settings.get("VIDEO_VISION_PROVIDER_ID")
        image_provider_id = self.settings.get("IMAGE_VISION_PROVIDER_ID")
        video_api_ready = bool(self.settings.get("VIDEO_VISION_API_KEY") and self.settings.get("VIDEO_VISION_MODEL"))
        image_api_ready = bool(self.settings.get("IMAGE_VISION_API_KEY") and self.settings.get("IMAGE_VISION_MODEL"))
        image_gen_backend = str(self.settings.get("IMAGE_GEN_BACKEND") or "openai").lower().strip()
        if image_gen_backend in ("novelai", "nai"):
            image_gen_ready = bool(self.settings.get("IMAGE_GEN_API_KEY"))
        else:
            image_gen_ready = bool(self.settings.get("IMAGE_GEN_API_KEY") or self.settings.get("VIDEO_VISION_API_KEY"))
        deps = {
            "yt-dlp": bool(self.processes._find_command("yt-dlp")),
            "ffmpeg": bool(self.processes._find_command("ffmpeg")),
            "ffprobe": bool(self.processes._find_command("ffprobe")),
        }
        proactive_media_ready = deps["yt-dlp"] and deps["ffmpeg"] and deps["ffprobe"]
        return {
            "python": {"aiohttp": True, "cryptography": True, "lunardate": True, "openai": True, "Pillow": True, "qrcode": True, "yt-dlp": True},
            "external_commands": deps,
            "llm": {
                "chat_provider": bool(self.settings.get("LLM_PROVIDER_ID")),
                "video_provider": bool(video_provider_id),
                "video_api": video_api_ready,
                "image_provider": bool(image_provider_id),
                "image_api": image_api_ready,
                "image_gen_api": image_gen_ready,
            },
            "features": {
                "video_cover_analysis": bool(video_provider_id or video_api_ready or self.settings.get("LLM_PROVIDER_ID")),
                "image_recognition": bool(image_provider_id or image_api_ready),
                "proactive_video_media": proactive_media_ready and bool(video_provider_id or video_api_ready),
                "proactive_video_fallback_text": bool(self.settings.get("LLM_PROVIDER_ID")),
                "dynamic_image_generation": image_gen_ready,
                "image_gen_backend": "novelai" if image_gen_backend in ("novelai", "nai") else "openai",
                "web_search": bool(self.settings.get("ENABLE_WEB_SEARCH") and self.settings.get("WEB_SEARCH_API_KEY")),
                "web_search_backend": (self.settings.get("WEB_SEARCH_BACKEND") or "tavily").lower().strip() if self.settings.get("ENABLE_WEB_SEARCH") else "",
                "web_search_judge": bool(self.settings.get("WEB_SEARCH_JUDGE_PROVIDER_ID")),
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
        if self.settings.get("ENABLE_WEB_SEARCH"):
            ws_backend = (self.settings.get("WEB_SEARCH_BACKEND") or "tavily").lower()
            ws_key = self.settings.get("WEB_SEARCH_API_KEY")
            if not ws_key:
                logger.warning("[BiliBot] 联网搜索已启用但未配置 WEB_SEARCH_API_KEY。")
            else:
                logger.info(f"[BiliBot] 🔍 联网搜索已启用，后端: {ws_backend}")
