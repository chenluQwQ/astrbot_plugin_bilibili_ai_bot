"""下载原视频、按配置切片、发到 QQ，过一阵清掉临时文件。"""
from __future__ import annotations

import asyncio
import os
import shutil
import time

from astrbot.api import logger
from ..config import TEMP_VIDEO_DIR
from ..infra.host_view import HostView


class ShareDelivery(HostView):
    """延时清理的任务挂在 ``_share_cleanup_tasks``。"""

    HOST_API = frozenset({
        "processes", "qq_chat", "settings", "video_files",
    })

    def __init__(self, host):
        super().__init__(host)
        self._share_cleanup_tasks = set()

    async def _split_share_video_for_chat(self, video_path):
        segment_sec = int(self.settings.get("BILI_SHARE_PARSE_SEGMENT_SECONDS"))
        max_segments = max(1, int(self.settings.get("BILI_SHARE_PARSE_MAX_SEGMENTS")))
        max_mb = max(1, int(self.settings.get("BILI_SHARE_PARSE_MAX_VIDEO_MB")))
        duration = await self.video_files._get_video_duration(video_path)
        size_mb = os.path.getsize(video_path) / 1024 / 1024 if os.path.exists(video_path) else 0
        if segment_sec <= 0:
            logger.info("[BiliBot] 群聊原视频分段已关闭，直接尝试发送整段视频")
            return [video_path], False
        segment_sec = max(30, segment_sec)
        if duration <= segment_sec and size_mb <= max_mb:
            return [video_path], False

        base = video_path.rsplit(".", 1)[0]
        pattern = f"{base}_share_%03d.mp4"
        code, _, stderr = await self.processes._run_process(
            "ffmpeg", "-y", "-i", video_path,
            "-map", "0", "-c", "copy", "-f", "segment",
            "-segment_time", str(segment_sec), "-reset_timestamps", "1",
            pattern, timeout=300,
        )
        if code != 0:
            logger.warning(f"[BiliBot] 群聊视频切片失败，回退整段: {stderr[:160] if stderr else ''}")
            return [video_path], False
        folder = os.path.dirname(video_path) or "."
        prefix = os.path.basename(base) + "_share_"
        segments = [
            os.path.join(folder, name)
            for name in sorted(os.listdir(folder))
            if name.startswith(prefix) and name.endswith(".mp4")
        ]
        if not segments:
            return [video_path], False
        return segments[:max_segments], len(segments) > max_segments

    def _prepare_share_send_files(self, paths, bvid):
        """复制一份专供适配器发送的文件，避免原下载/切片文件被后续清理影响。"""
        send_dir = os.path.join(TEMP_VIDEO_DIR, f"share_send_{bvid}_{int(time.time() * 1000)}")
        os.makedirs(send_dir, exist_ok=True)
        send_paths = []
        for idx, path in enumerate(paths or [], 1):
            if not path or not os.path.isfile(path):
                continue
            ext = os.path.splitext(path)[1].lower() or ".mp4"
            dest = os.path.join(send_dir, f"share_{idx:03d}{ext}")
            shutil.copy2(path, dest)
            send_paths.append(dest)
        return send_paths

    def _cleanup_share_video_files(self, paths):
        touched_dirs = []
        for path in dict.fromkeys(p for p in paths if p):
            try:
                if os.path.isfile(path):
                    touched_dirs.append(os.path.dirname(path))
                    os.remove(path)
            except OSError:
                pass
        for folder in dict.fromkeys(d for d in touched_dirs if d):
            try:
                base = os.path.basename(folder)
                if base.startswith("share_send_") and os.path.commonpath([os.path.abspath(folder), os.path.abspath(TEMP_VIDEO_DIR)]) == os.path.abspath(TEMP_VIDEO_DIR):
                    os.rmdir(folder)
            except OSError:
                pass

    async def _cleanup_share_video_files_later(self, paths, delay=600):
        await asyncio.sleep(delay)
        self._cleanup_share_video_files(paths)

    async def _send_shared_video(self, event, bvid, duration=None):
        """Download and send the original video; a dict result reports its message id."""
        if not self.settings.get("BILI_SHARE_PARSE_SEND_VIDEO"):
            return
        if not self.processes._find_command("yt-dlp"):
            yield event.plain_result("⚠️ 没找到 yt-dlp，暂时只能发解析卡和链接。")
            return
        video_path = None
        raw_send_paths = []
        send_paths = []
        skipped = False
        try:
            max_height = max(144, int(self.settings.get("BILI_SHARE_PARSE_VIDEO_MAX_HEIGHT")))
            video_path = await self.video_files._download_video(bvid, max_height=max_height, duration=duration)
            if not video_path:
                yield event.plain_result("⚠️ 原视频下载失败，先看解析卡和链接吧。")
                return
            raw_send_paths, skipped = await self._split_share_video_for_chat(video_path)
            send_paths = self._prepare_share_send_files(raw_send_paths, bvid)
            if not send_paths:
                yield event.plain_result("⚠️ 原视频切片准备失败，先看解析卡和链接吧。")
                return
            components = []
            for path in send_paths:
                comp = self.qq_chat._share_video_component(path)
                if not comp:
                    yield event.plain_result(f"当前 AstrBot 适配器没有可用的视频/文件组件，只能保留链接： https://www.bilibili.com/video/{bvid}")
                    return
                components.append(comp)
            # 单段直接发；多段用一个聊天记录合集，每个节点仅含视频，不能混入文字。
            if len(components) == 1:
                chain = components
            else:
                forward = self.qq_chat._share_forward_component(event, components)
                chain = [forward] if forward is not None else None
            if chain is None:
                yield event.plain_result("当前适配器不支持视频聊天记录合集，改为逐段发送。")
                for comp in components:
                    yield event.chain_result([comp])
            else:
                handled, message_id = await self.qq_chat._send_onebot_chain(event, chain[0])
                if handled:
                    if message_id is None:
                        yield event.plain_result(f"原视频没发出去，先看链接吧： https://www.bilibili.com/video/{bvid}")
                    elif message_id:
                        yield {"message_id": message_id}
                else:
                    yield event.chain_result(chain)
            if skipped:
                yield event.plain_result("后面还有内容，我先按配置发到这里；想多发可以调大 BILI_SHARE_PARSE_MAX_SEGMENTS。")
        finally:
            # 协议端可能在 yield 之后才 realpath/copy，发送缓存交给全局清理按 20 分钟窗口处理。
            # cleanup 必须同时包含发送副本 send_paths，否则 share_send_* 目录会永久残留。
            cleanup = list(dict.fromkeys(list(raw_send_paths) + list(send_paths)))
            if video_path and video_path not in cleanup:
                cleanup.append(video_path)
            if cleanup:
                task = asyncio.create_task(self._cleanup_share_video_files_later(cleanup, delay=1200))
                self._share_cleanup_tasks.add(task)
                task.add_done_callback(self._share_cleanup_tasks.discard)
