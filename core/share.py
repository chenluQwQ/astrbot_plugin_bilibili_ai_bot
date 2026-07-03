"""群聊 B站分享解析：识别链接/小程序，生成解析卡并可发送原视频切片。"""
import os
import asyncio
import re
import time
import json
import html
import aiohttp
from datetime import datetime
from astrbot.api import logger
from .config import TEMP_VIDEO_DIR, VIDEO_MEMORY_FILE


class ShareMixin:
    """处理群聊里的 B站视频分享。"""

    BVID_RE = re.compile(r"\b(BV[0-9A-Za-z]{10,})\b")
    AV_RE = re.compile(r"(?:\bav|aid=)(\d+)\b", re.IGNORECASE)
    URL_RE = re.compile(r"https?://[^\s\]\[\)\(\"'<>]+", re.IGNORECASE)

    @staticmethod
    def _normalize_share_text(value):
        text = html.unescape(str(value or ""))
        return text.replace("\\/", "/")

    def _append_share_text(self, parts, value):
        if value is None:
            return
        if isinstance(value, dict):
            for v in value.values():
                self._append_share_text(parts, v)
            return
        if isinstance(value, (list, tuple, set)):
            for v in value:
                self._append_share_text(parts, v)
            return

        text = self._normalize_share_text(value)
        if not text:
            return
        parts.append(text)

        # QQ 小程序一般是 CQ:json / arkElement，B站短链藏在 meta.detail_1.qqdocurl 里。
        # 这里顺手解析 JSON 字符串，把 qqdocurl/url/desc/prompt 等字段都纳入链接提取。
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                self._append_share_text(parts, json.loads(stripped))
            except Exception:
                pass

    def _collect_share_text(self, event):
        parts = []
        self._append_share_text(parts, event.message_str or "")
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            self._append_share_text(parts, raw)
        except Exception:
            pass
        try:
            for comp in getattr(event.message_obj, "message", []) or []:
                self._append_share_text(parts, comp)
                for attr in ("url", "title", "content", "desc", "text", "data"):
                    self._append_share_text(parts, getattr(comp, attr, None))
        except Exception:
            pass

        # 转发聊天记录里的预览文本通常藏在 NapCat raw.elements[].multiForwardMsgElement.xmlContent。
        # 默认不读，避免把聊天记录里的旧链接也自动解析；用户打开配置后才扫描。
        if self.config.get("BILI_SHARE_PARSE_FORWARD", False):
            for attr in ("raw", "raw_dict", "raw_event"):
                try:
                    self._append_share_text(parts, getattr(event.message_obj, attr, None))
                except Exception:
                    pass
        return "\n".join(p for p in parts if p)

    async def _resolve_share_url(self, url):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    url,
                    headers={**self._headers(), "Accept": "text/html,application/xhtml+xml"},
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    return str(r.url)
        except Exception as e:
            logger.debug(f"[BiliBot] B站短链展开失败: {url} {e}")
            return url

    async def _extract_bili_share_target(self, text):
        text = text or ""
        m = self.BVID_RE.search(text)
        if m:
            return {"bvid": m.group(1), "source": "bvid"}
        m = self.AV_RE.search(text)
        if m:
            return {"aid": int(m.group(1)), "source": "aid"}
        urls = self.URL_RE.findall(text)
        for url in urls:
            clean = url.rstrip("。，、)）]】>\"'")
            if not any(host in clean.lower() for host in ("bilibili.com", "b23.tv")):
                continue
            resolved = await self._resolve_share_url(clean) if "b23.tv" in clean.lower() else clean
            combined = f"{clean}\n{resolved}"
            m = self.BVID_RE.search(combined)
            if m:
                return {"bvid": m.group(1), "source": "url", "url": resolved}
            m = self.AV_RE.search(combined)
            if m:
                return {"aid": int(m.group(1)), "source": "url", "url": resolved}
        return None

    async def _get_video_info_by_share_target(self, target):
        if target.get("bvid"):
            oid = await self._get_video_oid(target["bvid"])
            if not oid:
                return None
            info = await self._get_video_info(oid)
            if info:
                info["oid"] = oid
            return info
        if target.get("aid"):
            info = await self._get_video_info(target["aid"])
            if info:
                info["oid"] = target["aid"]
            return info
        return None

    @staticmethod
    def _format_duration_minutes(seconds):
        try:
            seconds = int(seconds or 0)
        except Exception:
            seconds = 0
        if seconds <= 0:
            return "未知"
        minutes = seconds / 60
        text = f"{minutes:.1f}".rstrip("0").rstrip(".")
        return f"{text} 分钟"

    @staticmethod
    def _short_text(text, limit=140):
        text = " ".join(str(text or "").split())
        if not text:
            return "这个视频没有简介。"
        return text if len(text) <= limit else text[:limit].rstrip() + "…"

    async def _summarize_shared_video(self, info):
        return self._short_text(info.get("desc") or "这个视频没有简介。")

    def _build_share_card_text(self, info, summary):
        bvid = info.get("bvid", "")
        link = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
        lines = [
            f"标题：{info.get('title', '未知标题')}",
            f"UP：{info.get('owner_name', '未知')}",
            f"时长：{self._format_duration_minutes(info.get('duration', 0))}",
        ]
        if link:
            lines.append(f"链接：{link}")
        if summary:
            lines.append(f"内容：{self._short_text(summary)}")
        return "\n".join(lines)

    def _share_video_component(self, video_path):
        try:
            import astrbot.api.message_components as Comp
            for cls_name in ("Video", "File"):
                cls = getattr(Comp, cls_name, None)
                if not cls:
                    continue
                for meth in ("fromFileSystem", "from_file", "fromPath"):
                    fn = getattr(cls, meth, None)
                    if fn:
                        try:
                            return fn(video_path)
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"[BiliBot] 当前 AstrBot 视频组件不可用: {e}")
        return None

    async def _split_share_video_for_chat(self, video_path):
        segment_sec = max(30, int(self.config.get("BILI_SHARE_PARSE_SEGMENT_SECONDS", 180)))
        max_segments = max(1, int(self.config.get("BILI_SHARE_PARSE_MAX_SEGMENTS", 3)))
        max_mb = max(1, int(self.config.get("BILI_SHARE_PARSE_MAX_VIDEO_MB", 80)))
        duration = await self._get_video_duration(video_path)
        size_mb = os.path.getsize(video_path) / 1024 / 1024 if os.path.exists(video_path) else 0
        if duration <= segment_sec and size_mb <= max_mb:
            return [video_path], False

        base = video_path.rsplit(".", 1)[0]
        pattern = f"{base}_share_%03d.mp4"
        code, _, stderr = await self._run_process(
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

    def _cleanup_share_video_files(self, paths):
        for path in dict.fromkeys(p for p in paths if p):
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass

    async def _cleanup_share_video_files_later(self, paths, delay=600):
        await asyncio.sleep(delay)
        self._cleanup_share_video_files(paths)

    def _share_recent_hit(self, bvid):
        if not bvid:
            return False
        if not hasattr(self, "_bili_share_recent"):
            self._bili_share_recent = {}
        cooldown = max(0, int(self.config.get("BILI_SHARE_PARSE_COOLDOWN", 90)))
        now = time.time()
        self._bili_share_recent = {k: v for k, v in self._bili_share_recent.items() if now - v < cooldown * 3}
        last = self._bili_share_recent.get(bvid, 0)
        if cooldown and now - last < cooldown:
            return True
        self._bili_share_recent[bvid] = now
        return False

    async def _handle_group_bili_share(self, event):
        if not self.config.get("ENABLE_BILI_SHARE_PARSE", False):
            return
        msg = (event.message_str or "").strip()
        if msg.startswith("/"):
            return
        text = self._collect_share_text(event)
        if not text.strip():
            return
        target = await self._extract_bili_share_target(text)
        if not target:
            return
        info = await self._get_video_info_by_share_target(target)
        if not info or not info.get("bvid"):
            return
        bvid = info.get("bvid", "")
        if self._share_recent_hit(bvid):
            return

        summary = await self._summarize_shared_video(info)
        yield event.plain_result(self._build_share_card_text(info, summary))

        await self._save_self_memory_record(
            f"group_share:{bvid}",
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 群聊有人分享了B站视频《{info.get('title','')}》，UP:{info.get('owner_name','')}，内容概括:{self._short_text(summary, 120)}",
            memory_type="video",
            extra={"bvid": bvid, "owner_mid": str(info.get("owner_mid", "")), "video_title": info.get("title", ""), "tname": info.get("tname", "")},
        )

        if not self.config.get("BILI_SHARE_PARSE_SEND_VIDEO", True):
            return
        if not self._find_command("yt-dlp"):
            yield event.plain_result("⚠️ 没找到 yt-dlp，暂时只能发解析卡和链接。")
            return
        video_path = None
        send_paths = []
        skipped = False
        try:
            video_path = await self._download_video(bvid)
            if not video_path:
                yield event.plain_result("⚠️ 原视频下载失败，先看解析卡和链接吧。")
                return
            send_paths, skipped = await self._split_share_video_for_chat(video_path)
            total = len(send_paths)
            for idx, path in enumerate(send_paths, 1):
                caption = f"📼 回放切片 {idx}/{total} · 《{info.get('title','未知标题')[:24]}》"
                if not path or not os.path.isfile(path):
                    logger.warning(f"[BiliBot] 群聊视频发送前文件不存在: {path}")
                    yield event.plain_result(
                        f"{caption}\n⚠️ 视频文件不存在，可能是下载失败、临时文件被清理，或协议端无法访问本地路径。"
                    )
                    continue
                if os.path.getsize(path) <= 0:
                    logger.warning(f"[BiliBot] 群聊视频发送前文件为空: {path}")
                    yield event.plain_result(f"{caption}\n⚠️ 视频文件为空，可能下载失败。")
                    continue

                comp = self._share_video_component(path)
                if comp:
                    yield event.chain_result([__import__('astrbot.api.message_components', fromlist=['Plain']).Plain(caption), comp])
                else:
                    yield event.plain_result(f"{caption}\n当前 AstrBot 适配器没有可用的视频/文件组件，只能保留链接： https://www.bilibili.com/video/{bvid}")
                    break
            if skipped:
                yield event.plain_result("后面还有内容，我先按配置发到这里；想多发可以调大 BILI_SHARE_PARSE_MAX_SEGMENTS。")
        finally:
            cleanup = list(send_paths)
            if video_path and video_path not in cleanup:
                cleanup.append(video_path)
            if cleanup:
                asyncio.create_task(self._cleanup_share_video_files_later(cleanup, delay=600))


