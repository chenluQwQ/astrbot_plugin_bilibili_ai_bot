"""数据目录和可丢的缓存：建目录，统计、清理临时图片视频、搜索缓存和登录二维码。

登录状态、记忆、好感、日程这些从不碰。
"""
from __future__ import annotations

import os
import shutil
import time

from astrbot.api import logger

from ..config import DATA_DIR, TEMP_IMAGE_DIR, TEMP_VIDEO_DIR
from .files import path_size
from .host_view import HostView
try:
    from ..config import WEB_SEARCH_CACHE_FILE
except ImportError:  # 兼容最小化测试配置与旧版配置模块
    WEB_SEARCH_CACHE_FILE = os.path.join(DATA_DIR, "web_search_cache.json")


class CacheFiles(HostView):
    HOST_API = frozenset({"json_store"})

    # ── 数据目录 ──
    def _ensure_data_dir(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
        os.makedirs(TEMP_VIDEO_DIR, exist_ok=True)
        self._cleanup_temp_files()

    # ── 临时文件清理 ──
    def _cache_stats(self):
        """Describe disposable cache usage. Persistent login/memory data is excluded."""
        qr_path = os.path.join(DATA_DIR, "login_qr.png")
        buckets = {
            "images": {"label": "临时图片", "bytes": path_size(TEMP_IMAGE_DIR)},
            "videos": {"label": "临时视频", "bytes": path_size(TEMP_VIDEO_DIR)},
            "search": {"label": "联网搜索缓存", "bytes": path_size(WEB_SEARCH_CACHE_FILE)},
            "qr": {"label": "登录二维码", "bytes": path_size(qr_path)},
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
                    if temp_dir == TEMP_VIDEO_DIR and name.startswith(("share_send_", "inspection_")) and os.path.getmtime(fp) > share_cutoff:
                        continue
                    size = path_size(fp)
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
                cache = self.json_store._load_json(WEB_SEARCH_CACHE_FILE, {})
                if str(mode).lower() == "deep":
                    kept = {}
                else:
                    cutoff = time.time() - 86400
                    kept = {k: v for k, v in (cache.items() if isinstance(cache, dict) else []) if float(v.get("ts", 0) or 0) >= cutoff}
                old_size = os.path.getsize(WEB_SEARCH_CACHE_FILE)
                self.json_store._save_json(WEB_SEARCH_CACHE_FILE, kept)
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
