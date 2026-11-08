"""视频文件：下载、压缩、切片、截帧、清理。只管文件和外部命令，不懂业务，不调模型。

``host`` 提供 ``config``、``settings`` 和 ``_run_process``。它就是插件对象本身。
"""
from __future__ import annotations

import os
import re
import shutil

from astrbot.api import logger

from ..config import TEMP_VIDEO_DIR
from ..network import download_proxy_args, redact_proxy_error
from ..video_diagnostics import exception_detail, safe_detail, set_call_diagnostic


def _yt_dlp_error(stderr):
    """从 yt-dlp 的输出里挑真正的错误。

    开头常是「版本超过 90 天」之类的 WARNING，只截前面就看不到错误；错误后面还跟着一句
    固定的「Confirm you are on the latest version」，不代表真是版本问题，也去掉。
    """
    errors = [line.strip() for line in str(stderr).splitlines() if line.startswith("ERROR:")]
    text = errors[-1] if errors else str(stderr).strip()
    return re.split(r"[;,.]? *please report this issue", text, flags=re.IGNORECASE)[0]


class VideoFiles:
    def __init__(self, host):
        self._host = host

    @property
    def config(self):
        return self._host.config

    @property
    def settings(self):
        return self._host.settings

    async def _run_process(self, *args, **kwargs):
        return await self._host.processes._run_process(*args, **kwargs)

    # 分辨率回退链：先尝试无需合并的 MP4，避免无 ffmpeg 时只留下 m4a；再尝试高质量音视频合并。
    def _format_fallbacks(self, max_height=480):
        try:
            cap = max(144, int(max_height or 480))
        except Exception:
            cap = 480
        heights = [h for h in (360, 480, 720) if h <= cap]
        if not heights:
            heights = [cap]
        elif heights[-1] != cap and cap not in (360, 480, 720):
            heights.append(cap)

        formats = []
        for h in heights:
            formats.extend([
                # 横屏视频的短边通常是 height，竖屏视频的短边则是 width。
                # 两种方向都纳入同一档回退，避免 360x640 这类竖屏视频
                # 因 height>360 被误判为“没有 360p 格式”。
                f"best[ext=mp4][vcodec!=none][acodec!=none][height<={h}]/"
                f"best[ext=mp4][vcodec!=none][acodec!=none][width<={h}]/"
                f"best[height<={h}][vcodec!=none][acodec!=none]/"
                f"best[width<={h}][vcodec!=none][acodec!=none]",
                f"bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]/"
                f"bestvideo[ext=mp4][width<={h}]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={h}]+bestaudio/"
                f"bestvideo[width<={h}]+bestaudio/"
                f"best[height<={h}]/best[width<={h}]",
            ])
        formats.extend([
            "bestvideo+bestaudio/best",
            "worst[ext=mp4][vcodec!=none]/worst[vcodec!=none]",
        ])
        return formats
    _VIDEO_FILE_EXTS = {".mp4", ".mkv", ".webm", ".mov"}
    _AUDIO_FILE_EXTS = {".m4a", ".mp3", ".aac", ".opus", ".flac", ".wav"}
    _VIDEO_DOWNLOAD_FORMAT_TIMEOUT = 120
    _VIDEO_DOWNLOAD_MAX_TIMEOUT = 3600

    def _video_download_timeout(self, duration):
        """单个格式的下载等待秒数：至少 120 秒，长视频按时长的一半放宽。"""
        try:
            seconds = float(duration or 0)
        except (TypeError, ValueError):
            seconds = 0
        return int(min(self._VIDEO_DOWNLOAD_MAX_TIMEOUT, max(self._VIDEO_DOWNLOAD_FORMAT_TIMEOUT, seconds / 2)))

    def _pick_downloaded_video_file(self, bvid, output_dir=None):
        output_dir = output_dir or TEMP_VIDEO_DIR
        if not os.path.isdir(output_dir):
            return None
        candidates = []
        for name in os.listdir(output_dir):
            if not name.startswith(bvid) or name.endswith("_cookies.txt"):
                continue
            fp = os.path.join(output_dir, name)
            if not os.path.isfile(fp) or name.endswith((".part", ".ytdl")):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in self._AUDIO_FILE_EXTS:
                continue
            if ext not in self._VIDEO_FILE_EXTS:
                continue
            size = os.path.getsize(fp)
            if size > 0:
                candidates.append((ext == ".mp4", size, fp))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][2]

    async def _download_video(self, bvid, max_height=480, *, output_dir=None, duration=None):
        set_call_diagnostic()
        timeout = self._video_download_timeout(duration)
        download_dir = output_dir or TEMP_VIDEO_DIR
        proxy_args = download_proxy_args(self.config)
        output_template = os.path.join(download_dir, f"{bvid}.%(ext)s")
        # 生成 Netscape 格式 cookie 文件，兼容新版 yt-dlp
        cookie_file = os.path.join(download_dir, f"{bvid}_cookies.txt")
        sessdata = self.settings.get("SESSDATA")
        bili_jct = self.settings.get("BILI_JCT")
        dede_uid = self.settings.get("DEDE_USER_ID")
        buvid3 = self.settings.get("BUVID3")
        cookie_content = (
            "# Netscape HTTP Cookie File\n"
            f".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\t{sessdata}\n"
            f".bilibili.com\tTRUE\t/\tFALSE\t0\tbili_jct\t{bili_jct}\n"
            f".bilibili.com\tTRUE\t/\tFALSE\t0\tDedeUserID\t{dede_uid}\n"
        )
        if buvid3:
            cookie_content += f".bilibili.com\tTRUE\t/\tFALSE\t0\tbuvid3\t{buvid3}\n"
        try:
            fd = os.open(cookie_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(cookie_content)
        except Exception as e:
            set_call_diagnostic(reason=exception_detail(e, self.config))
            logger.warning(f"[BiliBot] Cookie文件写入失败: {exception_detail(e, self.config)}")
            return None

        last_err = ""
        try:
            formats = self._format_fallbacks(max_height)
            total_formats = len(formats)
            for format_index, fmt in enumerate(formats, start=1):
                # 清理上一轮可能残留的部分文件
                self._cleanup_partial_downloads(bvid, output_dir=output_dir) if output_dir else self._cleanup_partial_downloads(bvid)
                logger.info(
                    f"[BiliBot] 开始下载({bvid})，格式 {format_index}/{total_formats}，"
                    f"单次最多等待 {timeout} 秒"
                )
                code, _, stderr = await self._run_process(
                    "yt-dlp", "-o", output_template,
                    "--format", fmt,
                    "--no-playlist", "--merge-output-format", "mp4",
                    "--recode-video", "mp4",
                    "--cookies", cookie_file,
                    "--add-header", "Referer: https://www.bilibili.com",
                    "--limit-rate", "2M",
                    *proxy_args,
                    f"https://www.bilibili.com/video/{bvid}",
                    timeout=timeout,
                )
                if code == 0:
                    fp = self._pick_downloaded_video_file(bvid, output_dir=output_dir) if output_dir else self._pick_downloaded_video_file(bvid)
                    if fp:
                        logger.info(f"[BiliBot] 视频下载成功({bvid})，格式: {fmt}，文件: {os.path.basename(fp)}")
                        return fp
                    last_err = "yt-dlp 成功退出，但没有产出可发送的视频文件（可能只下载到音频）"
                    next_step = "尝试下一个格式" if format_index < total_formats else "已无后续格式"
                    logger.info(f"[BiliBot] {last_err}({bvid})，{next_step}")
                    continue
                if code == 124:
                    last_err = f"下载超时（等了 {timeout} 秒）"
                    if timeout > self._VIDEO_DOWNLOAD_FORMAT_TIMEOUT:
                        # 长视频已经等够久了，换格式只会从头再下一遍。
                        logger.info(f"[BiliBot] {last_err}({bvid})，不再尝试其他格式")
                        break
                    continue
                last_err = _yt_dlp_error(redact_proxy_error(stderr, self.config))[:200] if stderr else "unknown error"
                next_step = "尝试下一个格式" if format_index < total_formats else "已无后续格式"
                logger.info(
                    f"[BiliBot] 格式 {format_index}/{total_formats} 下载失败({bvid})，"
                    f"{next_step}: {last_err[:80]}"
                )
        finally:
            try:
                os.remove(cookie_file)
            except OSError:
                pass

        set_call_diagnostic(reason=safe_detail(last_err, self.config))
        logger.warning(f"[BiliBot] 视频下载全部失败({bvid}): {safe_detail(last_err, self.config)}")
        return None

    def _cleanup_partial_downloads(self, bvid, output_dir=None):
        """清理某个 bvid 的残留下载文件（不删 cookie）"""
        output_dir = output_dir or TEMP_VIDEO_DIR
        if not os.path.isdir(output_dir):
            return
        for name in os.listdir(output_dir):
            if name.startswith(bvid) and not name.endswith("_cookies.txt"):
                try:
                    fp = os.path.join(output_dir, name)
                    if os.path.isfile(fp):
                        os.remove(fp)
                except OSError:
                    pass

    async def _get_video_duration(self, video_path):
        """获取视频实际时长（秒）"""
        code, stdout, _ = await self._run_process(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path, timeout=60,
        )
        try:
            return float(stdout.strip()) if code == 0 and stdout.strip() else 0.0
        except ValueError:
            return 0.0

    async def _compress_video(self, input_path, timeout=300):
        """压缩单段视频，AI分析用，极速编码优先。"""
        output_path = input_path.rsplit(".", 1)[0] + "_compressed.mp4"
        code, _, stderr = await self._run_process(
            "ffmpeg", "-y", "-i", input_path,
            "-vf", "scale=360:-2", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35",
            output_path, timeout=timeout,
        )
        if code != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.warning(f"[BiliBot] 视频压缩失败或输出为空，回退原视频: {stderr[:160] if stderr else ''}")
            return input_path
        try:
            os.remove(input_path)
        except OSError:
            pass
        return output_path

    async def _slice_video_segments(self, video_path, segment_sec=300, max_segments=10):
        """将长视频切成多个片段，每段 segment_sec 秒，最多 max_segments 段，返回片段路径列表。
        短视频（<=segment_sec）直接压缩返回单段。"""
        duration = await self._get_video_duration(video_path)
        if duration <= 0:
            duration = segment_sec  # 拿不到时长就当一段处理

        if duration <= segment_sec:
            # 短视频：直接压缩整段
            compressed = await self._compress_video(video_path, timeout=max(300, int(duration)))
            return [compressed]

        # 长视频：切片
        segments = []
        num_segments = min(int(duration // segment_sec) + (1 if duration % segment_sec > 10 else 0), max_segments)
        base = video_path.rsplit(".", 1)[0]
        for i in range(num_segments):
            start = i * segment_sec
            seg_path = f"{base}_seg{i}.mp4"
            code, _, stderr = await self._run_process(
                "ffmpeg", "-y", "-ss", str(start), "-i", video_path,
                "-t", str(segment_sec),
                "-vf", "scale=360:-2", "-an",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35",
                seg_path, timeout=max(300, int(segment_sec)),
            )
            if code == 0 and os.path.exists(seg_path):
                segments.append(seg_path)
            else:
                logger.warning(f"[BiliBot] 切片{i}失败: {stderr[:100] if stderr else 'unknown'}")
        # 清理原始文件
        try:
            os.remove(video_path)
        except OSError:
            pass
        if not segments:
            logger.warning("[BiliBot] 所有切片均失败")
        return segments

    async def _extract_video_frames(self, video_path, count=5):
        frame_dir = video_path.rsplit(".", 1)[0] + "_frames"
        os.makedirs(frame_dir, exist_ok=True)
        code, stdout, _ = await self._run_process(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path, timeout=60,
        )
        try:
            duration = float(stdout.strip()) if code == 0 and stdout.strip() else 30.0
        except ValueError:
            duration = 30.0
        frames = []
        for i in range(count):
            ts = duration * (i + 1) / (count + 1)
            frame_path = os.path.join(frame_dir, f"frame_{i}.jpg")
            code, _, _ = await self._run_process(
                "ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", video_path,
                "-vframes", "1", "-vf", "scale=360:-2", "-q:v", "8",
                frame_path, timeout=120,
            )
            if code == 0 and os.path.exists(frame_path):
                frames.append(frame_path)
        return frames

    def _cleanup_video_artifacts(self, video_path, frames=None):
        paths = list(frames or [])
        if video_path:
            paths.append(video_path)
            frame_dir = video_path.rsplit(".", 1)[0] + "_frames"
        else:
            frame_dir = ""
        for path in paths:
            try:
                if path and os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        if frame_dir and os.path.isdir(frame_dir):
            try:
                shutil.rmtree(frame_dir)
            except OSError:
                pass
