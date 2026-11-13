"""视频文件：下载、压缩、切片、截帧、清理。只管文件和外部命令，不懂业务，不调模型。

``host`` 提供 ``config``、``settings`` 和 ``_run_process``。它就是插件对象本身。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from urllib.parse import urlsplit, urlunsplit

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


# 这些错误和具体格式无关：同一视频的所有格式都指向同一个 CDN，换格式只是把同样的失败再等一遍。
_NETWORK_ERROR_HINTS = (
    "timed out", "timeout", "connection", "unable to download", "unable to connect",
    "network is unreachable", "name resolution", "remote end closed", "ssl",
)


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

    # 分辨率回退链：先尝试音视频分离后合并（B 站现在几乎只提供 DASH 分离流），
    # 再退回无需合并的 MP4；没有 ffmpeg 时合并会立即失败并自动落到下一档。
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
                f"bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]/"
                f"bestvideo[ext=mp4][width<={h}]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={h}]+bestaudio/"
                f"bestvideo[width<={h}]+bestaudio/"
                f"best[height<={h}]/best[width<={h}]",
                # 横屏视频的短边通常是 height，竖屏视频的短边则是 width。
                # 两种方向都纳入同一档回退，避免 360x640 这类竖屏视频
                # 因 height>360 被误判为“没有 360p 格式”。
                f"best[ext=mp4][vcodec!=none][acodec!=none][height<={h}]/"
                f"best[ext=mp4][vcodec!=none][acodec!=none][width<={h}]/"
                f"best[height<={h}][vcodec!=none][acodec!=none]/"
                f"best[width<={h}][vcodec!=none][acodec!=none]",
            ])
        formats.extend([
            "bestvideo+bestaudio/best",
            # 原来的 worst[vcodec!=none] 永远选不到东西：worst 只挑“音视频合一”的格式，
            # DASH 视频没有这种格式，最后一档必然报 Requested format is not available。
            "worstvideo+worstaudio/worst",
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
        network_err = ""  # 网络类错误优先保留：它才是换格式也躲不掉的真实原因
        try:
            # 先探一次 CDN 线路：原线路在这台机器上不通时直接改走可达镜像，
            # 省掉在死线路上反复等超时。这一步失败不影响后面的直连回退链。
            try:
                mirrored = await self._download_via_reachable_cdn(
                    bvid, max_height, cookie_file, output_dir=output_dir, duration=duration
                )
            except Exception as exc:
                logger.info(f"[BiliBot] CDN 线路探测失败({bvid})，按原逻辑下载: {exception_detail(exc, self.config)[:120]}")
                mirrored = None
            if mirrored:
                return mirrored

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
                    # yt-dlp 默认单次连接等 20 秒、重试 10 次，CDN 不通时一档就要等好几分钟。
                    "--socket-timeout", "15",
                    "--retries", "2",
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
                    network_err = f"下载超时（等了 {timeout} 秒）"
                    last_err = network_err
                    logger.info(f"[BiliBot] {last_err}({bvid})，同一视频的其他格式也会撞上同样的网络问题，不再尝试")
                    break
                last_err = _yt_dlp_error(redact_proxy_error(stderr, self.config))[:200] if stderr else "unknown error"
                if any(hint in last_err.lower() for hint in _NETWORK_ERROR_HINTS):
                    network_err = last_err
                    logger.info(f"[BiliBot] 格式 {format_index}/{total_formats} 网络失败({bvid})，不再尝试其他格式: {last_err[:80]}")
                    break
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

        set_call_diagnostic(reason=safe_detail(network_err or last_err, self.config))
        logger.warning(f"[BiliBot] 视频下载全部失败({bvid}): {safe_detail(network_err or last_err, self.config)}")
        return None

    # B 站按访客所在运营商分配 CDN 域名：实测部分线路（cn-sh-cc-*，上海移动）在这台服务器上
    # 完全连不上，而深圳对象存储线路正常。所有线路共用同一套路径和签名，只换域名就能取到
    # 同一份数据，所以先探一次线路，不通就改走可达镜像，全程不依赖任何代理。
    _CDN_MIRROR_HOSTS = (
        "upos-sz-mirrorcos.bilivideo.com",
        "upos-sz-estgcos.bilivideo.com",
        "upos-sz-estgoss.bilivideo.com",
    )
    _CDN_PROBE_TIMEOUT = 3

    @staticmethod
    def _cdn_host(url):
        """取出 B 站视频 CDN 的域名；不是 CDN 地址就返回空串。"""
        try:
            host = urlsplit(str(url)).hostname or ""
        except ValueError:
            return ""
        return host if host.endswith(".bilivideo.com") else ""

    async def _reachable_cdn_host(self, hosts):
        """并发探 443，按传入顺序返回第一个连得上的域名。"""
        async def probe(host):
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, 443), timeout=self._CDN_PROBE_TIMEOUT
                )
                writer.close()
                return True
            except Exception:
                return False

        for host, ok in zip(hosts, await asyncio.gather(*(probe(host) for host in hosts))):
            if ok:
                return host
        return ""

    @staticmethod
    def _swap_cdn_host(url, host):
        parsed = urlsplit(str(url))
        return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))

    async def _download_via_reachable_cdn(self, bvid, max_height, cookie_file, *, output_dir=None, duration=None):
        """探一次 CDN 线路：原线路可达就返回 None 交给直连逻辑，不可达则换镜像域名后下载。

        换域名只在拿到视频信息后做一次，任何一步失败都返回 None，由原有回退链兜底。
        """
        download_dir = output_dir or TEMP_VIDEO_DIR
        # 不用 bvid 开头：_cleanup_partial_downloads 会删掉所有 bvid 前缀的文件
        info_file = os.path.join(download_dir, f"_cdn_{bvid}.json")
        try:
            code, stdout, _ = await self._run_process(
                "yt-dlp", "-J", "--no-playlist",
                "--cookies", cookie_file,
                "--add-header", "Referer: https://www.bilibili.com",
                *download_proxy_args(self.config),
                f"https://www.bilibili.com/video/{bvid}",
                timeout=60,
            )
            if code != 0 or not stdout.strip():
                return None
            try:
                info = json.loads(stdout)
            except ValueError:
                return None
            entries = [item for item in (info.get("formats") or []) if isinstance(item, dict)]
            hosts = []
            for item in entries:
                host = self._cdn_host(item.get("url"))
                if host and host not in hosts:
                    hosts.append(host)
            if not hosts:
                return None
            # 先看原线路，全不可达才看镜像：避免把本来通的线路也换掉。
            reachable = await self._reachable_cdn_host(hosts)
            if not reachable:
                reachable = await self._reachable_cdn_host(
                    [host for host in self._CDN_MIRROR_HOSTS if host not in hosts]
                )
            if not reachable or reachable in hosts:
                return None
            changed = 0
            for item in entries:
                for key in ("url", "manifest_url"):
                    value = item.get(key)
                    if isinstance(value, str) and self._cdn_host(value):
                        item[key] = self._swap_cdn_host(value, reachable)
                        changed += 1
                for fragment in item.get("fragments") or []:
                    value = fragment.get("url")
                    if isinstance(value, str) and self._cdn_host(value):
                        fragment["url"] = self._swap_cdn_host(value, reachable)
                        changed += 1
            if not changed:
                return None
            with open(info_file, "w", encoding="utf-8") as handle:
                json.dump(info, handle, ensure_ascii=False)
            logger.info(f"[BiliBot] CDN 线路不可达({bvid})，改走 {reachable}")
            output_template = os.path.join(download_dir, f"{bvid}.%(ext)s")
            timeout = self._video_download_timeout(duration)
            for fmt in self._format_fallbacks(max_height):
                self._cleanup_partial_downloads(bvid, output_dir=output_dir)
                code, _, _ = await self._run_process(
                    "yt-dlp", "--load-info-json", info_file,
                    "-o", output_template,
                    "--format", fmt,
                    "--no-playlist", "--merge-output-format", "mp4",
                    "--recode-video", "mp4",
                    "--limit-rate", "2M",
                    "--socket-timeout", "15",
                    "--retries", "2",
                    timeout=timeout,
                )
                if code == 0:
                    fp = self._pick_downloaded_video_file(bvid, output_dir=output_dir)
                    if fp:
                        logger.info(f"[BiliBot] 视频下载成功({bvid})，线路 {reachable}，格式: {fmt}")
                        return fp
            return None
        finally:
            try:
                os.remove(info_file)
            except OSError:
                pass

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
