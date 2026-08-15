"""基础工具方法：HTTP请求、JSON读写、进程管理、环境检测、临时文件清理。"""
import os
import json
import shutil
import tempfile
import threading
import time
import asyncio
import aiohttp
from astrbot.api import logger
from .config import DATA_DIR, TEMP_IMAGE_DIR, TEMP_VIDEO_DIR, USER_AGENT
try:
    from .config import WEB_SEARCH_CACHE_FILE
except ImportError:  # 兼容最小化测试配置与旧版配置模块
    WEB_SEARCH_CACHE_FILE = os.path.join(DATA_DIR, "web_search_cache.json")


_JSON_SAVE_LOCKS = {}
_JSON_SAVE_LOCKS_GUARD = threading.Lock()


class UtilsMixin:
    """提供所有模块共用的底层工具方法。"""

    # ── 数据目录 ──
    def _ensure_data_dir(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
        os.makedirs(TEMP_VIDEO_DIR, exist_ok=True)
        self._cleanup_temp_files()

    @staticmethod
    def _normalize_openai_base_url(value):
        """Accept a host/root URL and normalize it to an OpenAI-compatible /v1 base."""
        url = str(value or "").strip().rstrip("/")
        if not url:
            return ""
        return url if url.lower().endswith("/v1") else f"{url}/v1"

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
        # 每次写入使用同目录的唯一临时文件：既保留 os.replace 的原子性，
        # 也避免多个线程/任务争用固定的 ``path.tmp``（Windows 会报 WinError 32）。
        target = os.path.abspath(os.fspath(path))
        parent = os.path.dirname(target)
        os.makedirs(parent, exist_ok=True)
        with _JSON_SAVE_LOCKS_GUARD:
            target_lock = _JSON_SAVE_LOCKS.setdefault(target, threading.Lock())
        # Windows 也可能拒绝两个线程同时 replace 同一个目标，因此同路径串行提交；
        # 不同数据文件仍各自使用独立锁。
        with target_lock:
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(target)}.",
                suffix=".tmp",
                dir=parent,
                text=True,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, target)
            finally:
                # json.dump / os.replace 失败时不遗留临时文件；原目标文件保持不变。
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass

    def _append_json_list(self, path, entry, cap=None):
        """读最新数据→追加→写回（同步、无 await），避免并发任务用旧快照互相覆盖。"""
        data = self._load_json(path, [])
        if not isinstance(data, list):
            data = []
        data.append(entry)
        if cap:
            data = data[-cap:]
        self._save_json(path, data)
        return data

    # ── 外部命令 ──
    def _find_command(self, command_name):
        found = shutil.which(command_name)
        if found:
            return found
        names = [command_name]
        if os.name == "nt" and not command_name.lower().endswith(".exe"):
            names.append(command_name + ".exe")
        search_dirs = [
            "/usr/local/bin", "/usr/bin", "/bin", "/opt/homebrew/bin",
            r"C:\\ffmpeg\\bin", r"C:\\Program Files\\ffmpeg\\bin", r"C:\\ProgramData\\chocolatey\\bin",
        ]
        for folder in search_dirs:
            for name in names:
                path = os.path.join(folder, name)
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    return path
        return None

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
                        d = await r.json(content_type=None)
                        if isinstance(d, dict) and d.get("code") == -352 and getattr(self, "_wbi_keys_cache", None):
                            # -352 通常意味着 wbi key 已轮换或签名失效，清缓存让下次请求重新拉取
                            self._wbi_keys_cache = None
                            logger.warning("[BiliBot] 收到 -352 风控码，已清除 WBI key 缓存待下次重签")
                        return d, r
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
        if args:
            resolved = self._find_command(args[0]) or args[0]
            args = (resolved, *args[1:])
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
        image_gen_backend = str(self.config.get("IMAGE_GEN_BACKEND", "") or "openai").lower().strip()
        if image_gen_backend in ("novelai", "nai"):
            image_gen_ready = bool(self.config.get("IMAGE_GEN_API_KEY", ""))
        else:
            image_gen_ready = bool(self.config.get("IMAGE_GEN_API_KEY", "") or self.config.get("VIDEO_VISION_API_KEY", ""))
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
                "dynamic_image_generation": image_gen_ready,
                "image_gen_backend": "novelai" if image_gen_backend in ("novelai", "nai") else "openai",
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
    @staticmethod
    def _path_size(path):
        """Return the byte size of a file or directory without following links."""
        if not os.path.exists(path):
            return 0
        if os.path.isfile(path):
            try:
                return os.path.getsize(path)
            except OSError:
                return 0
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total

    def _cache_stats(self):
        """Describe disposable cache usage. Persistent login/memory data is excluded."""
        qr_path = os.path.join(DATA_DIR, "login_qr.png")
        buckets = {
            "images": {"label": "临时图片", "bytes": self._path_size(TEMP_IMAGE_DIR)},
            "videos": {"label": "临时视频", "bytes": self._path_size(TEMP_VIDEO_DIR)},
            "search": {"label": "联网搜索缓存", "bytes": self._path_size(WEB_SEARCH_CACHE_FILE)},
            "qr": {"label": "登录二维码", "bytes": self._path_size(qr_path)},
        }
        return {
            "total_bytes": sum(item["bytes"] for item in buckets.values()),
            "buckets": buckets,
            "protected": ["B站 Cookie 与扫码登录状态", "记忆与用户画像", "好感度", "日程和运行数据库"],
        }

    def _purge_cache(self, mode="normal"):
        """Clear derived media/search caches only; never touch credentials or memories."""
        before = self._cache_stats()
        deleted_files = 0
        released_bytes = 0
        share_cutoff = time.time() - 1200
        for temp_dir in (TEMP_IMAGE_DIR, TEMP_VIDEO_DIR):
            if not os.path.isdir(temp_dir):
                continue
            for name in os.listdir(temp_dir):
                fp = os.path.join(temp_dir, name)
                try:
                    if temp_dir == TEMP_VIDEO_DIR and name.startswith("share_send_") and os.path.getmtime(fp) > share_cutoff:
                        continue
                    size = self._path_size(fp)
                    if os.path.isfile(fp):
                        os.remove(fp)
                    elif os.path.isdir(fp):
                        shutil.rmtree(fp)
                    else:
                        continue
                    deleted_files += 1
                    released_bytes += size
                except OSError:
                    pass
        qr_path = os.path.join(DATA_DIR, "login_qr.png")
        if str(mode).lower() == "deep" and os.path.isfile(qr_path):
            try:
                released_bytes += os.path.getsize(qr_path)
                os.remove(qr_path)
                deleted_files += 1
            except OSError:
                pass
        if os.path.isfile(WEB_SEARCH_CACHE_FILE):
            try:
                cache = self._load_json(WEB_SEARCH_CACHE_FILE, {})
                if str(mode).lower() == "deep":
                    kept = {}
                else:
                    cutoff = time.time() - 86400
                    kept = {k: v for k, v in (cache.items() if isinstance(cache, dict) else []) if float(v.get("ts", 0) or 0) >= cutoff}
                old_size = os.path.getsize(WEB_SEARCH_CACHE_FILE)
                self._save_json(WEB_SEARCH_CACHE_FILE, kept)
                new_size = os.path.getsize(WEB_SEARCH_CACHE_FILE) if os.path.exists(WEB_SEARCH_CACHE_FILE) else 0
                released_bytes += max(0, old_size - new_size)
                if old_size != new_size:
                    deleted_files += 1
            except (OSError, TypeError, ValueError):
                pass
        after = self._cache_stats()
        result = {
            "mode": "deep" if str(mode).lower() == "deep" else "normal",
            "deleted_files": deleted_files,
            "released_bytes": max(released_bytes, before["total_bytes"] - after["total_bytes"]),
            "before_bytes": before["total_bytes"],
            "after_bytes": after["total_bytes"],
            "stats": after,
        }
        if deleted_files:
            logger.info(f"[BiliBot] 🗑️ 清理了 {deleted_files} 个缓存项，释放 {result['released_bytes']} 字节")
        return result

    def _cleanup_temp_files(self):
        return self._purge_cache("normal")

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
        # 从每个括号位置尝试完整解析，取第一个合法的 JSON 对象/数组；
        # 既支持顶层数组，也不会把正文里的「[说明]」误当 JSON（解析失败会继续向后找）
        decoder = json.JSONDecoder()
        for idx, ch in enumerate(text):
            if ch in "[{":
                try:
                    obj, end = decoder.raw_decode(text[idx:])
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    return text[idx:idx + end]
                # 数组只接受对象列表（如批量评估结果），跳过正文里的 [1,2]、[说明] 等
                if isinstance(obj, list) and all(isinstance(x, dict) for x in obj):
                    return text[idx:idx + end]
        return text

