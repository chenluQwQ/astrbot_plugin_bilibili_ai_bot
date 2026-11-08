"""找外部命令（ffmpeg、yt-dlp）、跑子进程。"""
from __future__ import annotations

import asyncio
import os
import shutil

from astrbot.api import logger


def find_command(command_name):
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


async def run_process(*args, timeout=300, find=find_command):
    if args:
        resolved = find(args[0]) or args[0]
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
    except asyncio.CancelledError:
        # 取消时不能让 ffmpeg / yt-dlp 继续往已经清理掉的临时目录里写。
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
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


class Processes:
    """插件上用的那一份：找命令走自己的 ``_find_command``，替换它也会影响 ``_run_process``。"""

    def _find_command(self, command_name):
        return find_command(command_name)

    async def _run_process(self, *args, timeout=300):
        return await run_process(*args, timeout=timeout, find=self._find_command)
