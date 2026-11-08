"""每周更新一次 yt-dlp。B站一改接口最先坏的就是它，旧版本下不了视频。

默认关闭：更新等于在插件的进程环境里装新代码，由主人自己决定要不要开。只更新这一个包。
"""
from __future__ import annotations

import asyncio
import sys
import time

from astrbot.api import logger

from ..storage.json_file import JsonFile
from .host_view import HostView

UPDATE_EVERY_SECONDS = 7 * 24 * 3600
RETRY_AFTER_FAILURE_SECONDS = 24 * 3600
PIP_TIMEOUT_SECONDS = 300


class YtDlpUpdater(HostView):
    """一个插件一个：后台任务记在这里，不挡主循环。"""

    HOST_API = frozenset({"processes", "settings"})

    def __init__(self, host, state_file: JsonFile):
        super().__init__(host)
        self._state = state_file
        self._task = None

    def maybe_start(self):
        if not self.settings.get("ENABLE_YTDLP_AUTO_UPDATE"):
            return None
        if self._task is not None and not self._task.done():
            return None
        if time.time() < float(self._state.read({}).get("next_check", 0) or 0):
            return None
        self._task = asyncio.create_task(self._update())
        return self._task

    def cancel(self):
        if self._task is not None:
            self._task.cancel()

    async def _version(self):
        code, stdout, _ = await self.processes._run_process("yt-dlp", "--version", timeout=30)
        return stdout.strip() if code == 0 else ""

    async def _update(self):
        # 先记下一次的时间：更新途中重启也不会连着再来一遍。
        self._remember(RETRY_AFTER_FAILURE_SECONDS)
        before = await self._version()
        code, _, stderr = await self.processes._run_process(
            sys.executable, "-m", "pip", "install", "--upgrade", "--no-input", "yt-dlp",
            timeout=PIP_TIMEOUT_SECONDS,
        )
        if code != 0:
            logger.warning(f"[BiliBot] yt-dlp 更新失败，明天再试: {stderr.strip()[-200:]}")
            return
        self._remember(UPDATE_EVERY_SECONDS)
        after = await self._version()
        if after and after != before:
            logger.info(f"[BiliBot] yt-dlp 已更新: {before or '未知'} → {after}")
        else:
            logger.info(f"[BiliBot] yt-dlp 没有变化，插件实际用的版本是 {after or '未知'}")

    def _remember(self, wait_seconds):
        self._state.write({"next_check": time.time() + wait_seconds, "last_attempt": int(time.time())})
