"""群聊 B站分享解析：识别链接/小程序，生成解析卡并可发送原视频切片。"""
import asyncio
import html
import json
import os
import re
import shutil
import time
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
from astrbot.api import logger

from .config import TEMP_VIDEO_DIR, SHARE_SEND_VIDEO_DIR, VIDEO_MEMORY_FILE


class ShareMixin:
    """处理群聊里的 B站视频分享。"""

    BVID_RE = re.compile(r"\b(BV[0-9A-Za-z]{10,})\b")
    AV_RE = re.compile(r"(?:\bav|aid=)(\d+)\b", re.IGNORECASE)
    URL_RE = re.compile(r"https?://[^\s\]\[\)\(\"'<>]+", re.IGNORECASE)
    CQ_JSON_RE = re.compile(r"\[CQ:json,data=(.*?)\]", re.IGNORECASE | re.DOTALL)
    MINIAPP_KEYS = (
        "url", "jumpUrl", "jump_url", "qqdocurl", "preview", "sourceUrl",
        "pagepath", "pagePath", "webUrl", "web_url", "shareUrl", "share_url",
        "title", "desc", "content", "text", "summary",
    )

    def _collect_share_text(self, event):
        parts = [event.message_str or ""]
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if raw:
                parts.append(str(raw))
        except Exception:
            pass
        try:
            for comp in getattr(event.message_obj, "message", []) or []:
                parts.append(str(comp))
                if isinstance(comp, dict):
                    parts.extend(self._flatten_share_payload(comp))
                    continue
                for attr in (*self.MINIAPP_KEYS, "data", "meta"):
                    val = getattr(comp, attr, None)
                    if val:
                        parts.extend(self._flatten_share_payload(val))
        except Exception:
            pass
        return "\n".join(p for p in parts if p)

    def _flatten_share_payload(self, payload):
        """把 QQ 小程序/CQ JSON 里的嵌套字段尽量摊平成可搜索文本。"""
        out = []
        if payload is None:
            return out
        if isinstance(payload, (list, tuple, set)):
            for item in payload:
                out.extend(self._flatten_share_payload(item))
            return out
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in self.MINIAPP_KEYS or isinstance(value, (dict, list, tuple)):
                    out.extend(self._flatten_share_payload(value))
                elif isinstance(value, str):
                    out.append(value)
            return out

        text = str(payload)
        out.append(text)
        for candidate in self._share_text_variants(text):
            stripped = candidate.strip()
            if not stripped:
                continue
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    out.extend(self._flatten_share_payload(json.loads(stripped)))
                except Exception:
                    pass
        return out

    def _share_text_variants(self, text):
        text = text or ""
        variants = []

        def add(value):
            if value and value not in variants:
                variants.append(value)

        add(text)
        add(html.unescape(text))
        add(text.replace("\\/", "/"))
        add(html.unescape(text).replace("\\/", "/"))
        try:
            add(unquote(text))
            add(unquote(html.unescape(text).replace("\\/", "/")))
        except Exception:
            pass
        for m in self.CQ_JSON_RE.finditer(text):
            data = m.group(1)
            add(data)
            add(html.unescape(data).replace("\\/", "/"))
            try:
                add(unquote(html.unescape(data).replace("\\/", "/")))
            except Exception:
                pass
        return variants

    def _normalized_share_blob(self, text):
        queue = list(self._share_text_variants(text))
        seen = set()
        parts = []
        while queue:
            item = queue.pop(0)
            if not item or item in seen:
                continue
            seen.add(item)
            parts.append(item)
            for extra in self._flatten_share_payload(item):
                if extra not in seen:
                    queue.extend(self._share_text_variants(extra))
        return "\n".join(parts)

    def _clean_share_url(self, url):
        text = unquote(html.unescape((url or "").replace("\\/", "/")))
        return text.rstrip("。，、,;；:：)）]】}>\\\"'")

    def _target_from_url(self, url):
        text = self._clean_share_url(url)
        m = self.BVID_RE.search(text)
        if m:
            return {"bvid": m.group(1), "source": "url", "url": text}
        m = self.AV_RE.search(text)
        if m:
            return {"aid": int(m.group(1)), "source": "url", "url": text}
        try:
            parsed = urlparse(text)
            qs = parse_qs(parsed.query)
            for key in ("bvid", "bv", "video_id"):
                value = (qs.get(key) or [""])[0]
                m = self.BVID_RE.search(value)
                if m:
                    return {"bvid": m.group(1), "source": "url", "url": text}
            for key in ("aid", "av"):
                value = (qs.get(key) or [""])[0]
                if str(value).isdigit():
                    return {"aid": int(value), "source": "url", "url": text}
        except Exception:
            pass
        return None

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
        blob = self._normalized_share_blob(text)
        m = self.BVID_RE.search(blob)
        if m:
            return {"bvid": m.group(1), "source": "bvid"}
        m = self.AV_RE.search(blob)
        if m:
            return {"aid": int(m.group(1)), "source": "aid"}

        urls = []
        for candidate in self._share_text_variants(blob):
            for url in self.URL_RE.findall(candidate):
                clean = self._clean_share_url(url)
                if clean not in urls:
                    urls.append(clean)
        for url in urls:
            lower = url.lower()
            if not any(host in lower for host in ("bilibili.com", "b23.tv", "bili2233.cn")):
                continue
            direct = self._target_from_url(url)
            if direct:
                return direct
            if any(host in lower for host in ("b23.tv", "bili2233.cn")):
                resolved = await self._resolve_share_url(url)
                target = self._target_from_url(resolved)
                if target:
                    target["url"] = resolved
                    return target
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
    def _format_duration(seconds):
        try:
            seconds = int(seconds or 0)
        except Exception:
            seconds = 0
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    async def _summarize_shared_video(self, info):
        bvid = info.get("bvid", "")
        vc = self._load_json(VIDEO_MEMORY_FILE, {})
        cached = vc.get(bvid, {}) if bvid else {}
        if cached.get("analysis"):
            return cached["analysis"]
        if not self.config.get("BILI_SHARE_PARSE_ANALYZE", True):
            return (info.get("desc") or "这个视频没有简介。")[:220]
        result = await self._analyze_video_text(info)
        summary = (result or info.get("desc") or "暂时没能概括出内容。")[:500]
        if bvid:
            vc[bvid] = {
                "bvid": bvid,
                "title": info.get("title", ""),
                "desc": (info.get("desc") or "")[:200],
                "owner_name": info.get("owner_name", ""),
                "owner_mid": str(info.get("owner_mid", "")),
                "tname": info.get("tname", ""),
                "analysis": summary,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "source": "group_share",
            }
            self._save_json(VIDEO_MEMORY_FILE, vc)
        return summary

    def _share_video_intro(self, info):
        intro = (info.get("desc") or "这个视频没有简介。").strip()
        intro = re.sub(r"\s+", " ", intro)
        return intro[:260]

    def _build_share_card_text(self, info, intro):
        bvid = info.get("bvid", "")
        link = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
        title = info.get("title") or "未知标题"
        owner = info.get("owner_name") or "未知UP"
        owner_mid = info.get("owner_mid") or "?"
        tname = info.get("tname") or "未知分区"
        intro = (intro or self._share_video_intro(info)).strip()
        lines = [
            "🎞️ B站视频解析",
            "━━━━━━━━━━━━",
            f"标题：{title}",
            f"UP主：{owner}（UID:{owner_mid}）",
            f"分区：{tname} | 时长：{self._format_duration(info.get('duration', 0))}",
        ]
        if link:
            lines.append(f"原链接：{link}")
        lines.extend(["", f"简介：{intro}"])
        if self.config.get("BILI_SHARE_PARSE_SEND_VIDEO", True):
            lines.append("\n📼 我去取低清回放，整理好就发切片。")
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
        segment_sec = int(self.config.get("BILI_SHARE_PARSE_SEGMENT_SECONDS", 180))
        max_segments = max(1, int(self.config.get("BILI_SHARE_PARSE_MAX_SEGMENTS", 5)))
        max_mb = max(1, int(self.config.get("BILI_SHARE_PARSE_MAX_VIDEO_MB", 80)))
        duration = await self._get_video_duration(video_path)
        size_mb = os.path.getsize(video_path) / 1024 / 1024 if os.path.exists(video_path) else 0
        if segment_sec <= 0:
            logger.info("[BiliBot] 群聊原视频分段已关闭，直接尝试发送整段视频")
            return [video_path], False
        segment_sec = max(30, segment_sec)
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

    def _prepare_share_send_files(self, paths, bvid):
        """复制一份专供适配器发送的文件，避免原下载/切片文件被后续清理影响。"""
        send_dir = os.path.join(SHARE_SEND_VIDEO_DIR, f"{bvid}_{int(time.time() * 1000)}")
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
                if os.path.commonpath([os.path.abspath(folder), os.path.abspath(SHARE_SEND_VIDEO_DIR)]) == os.path.abspath(SHARE_SEND_VIDEO_DIR):
                    os.rmdir(folder)
            except OSError:
                pass

    async def _cleanup_share_video_files_later(self, paths, delay=600):
        await asyncio.sleep(delay)
        self._cleanup_share_video_files(paths)

    def _share_context_keys(self, event):
        keys = []
        def add(value):
            if value:
                value = str(value)
                if value not in keys:
                    keys.append(value)
        try:
            add(event.get_session_id())
        except Exception:
            pass
        try:
            add(event.unified_msg_origin)
        except Exception:
            pass
        try:
            obj = getattr(event, "message_obj", None)
            add(getattr(obj, "group_id", None))
            raw = getattr(obj, "raw_message", None)
            if raw and "group_id" in str(raw):
                m = re.search(r"'group_id'\s*:\s*(\d+)|\"group_id\"\s*:\s*(\d+)", str(raw))
                if m:
                    add(f"group:{m.group(1) or m.group(2)}")
        except Exception:
            pass
        return keys or ["global"]

    def _remember_recent_group_share(self, event, info, intro):
        if not hasattr(self, "_recent_group_share_context"):
            self._recent_group_share_context = {}
        bvid = info.get("bvid", "")
        record = {
            "time": time.time(),
            "remaining_turns": 10,
            "bvid": bvid,
            "title": info.get("title", ""),
            "owner_name": info.get("owner_name", ""),
            "owner_mid": str(info.get("owner_mid", "")),
            "tname": info.get("tname", ""),
            "duration": self._format_duration(info.get("duration", 0)),
            "desc": intro or self._share_video_intro(info),
            "link": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        }
        for key in self._share_context_keys(event):
            self._recent_group_share_context[key] = record
        logger.debug(f"[BiliBot] recent group share context remembered: {record.get('title','')[:30]} {bvid}")

    def _get_recent_group_share_prompt(self, event):
        ctx = getattr(self, "_recent_group_share_context", None)
        if not ctx:
            return ""
        record = None
        hit_key = None
        for key in self._share_context_keys(event):
            item = self._recent_group_share_context.get(key)
            if item and int(item.get("remaining_turns", 0)) > 0:
                record = item
                hit_key = key
                break
        if not record:
            return ""
        record["remaining_turns"] = int(record.get("remaining_turns", 0)) - 1
        if hit_key and record["remaining_turns"] <= 0:
            for key in self._share_context_keys(event):
                if self._recent_group_share_context.get(key) is record:
                    self._recent_group_share_context.pop(key, None)
        return (
            "\u3010\u4e0a\u4e00\u6761\u7fa4\u804a\u80cc\u666f\uff1aB\u7ad9\u89c6\u9891\u5206\u4eab\u3011\n"
            "\u521a\u624d\u8fd9\u4e2a\u4f1a\u8bdd\u91cc\u89e3\u6790\u8fc7\u4e00\u4e2aB\u7ad9\u89c6\u9891\u3002\u7528\u6237\u5982\u679c\u8bf4\u201c\u8fd9\u4e2a\u89c6\u9891\u201d\u201c\u521a\u624d\u90a3\u4e2a\u201d\u201c\u6709\u94fe\u63a5\u7684\u8bdd\u201d\u201c\u8fd9\u4e2aUP\u201d\u7b49\u6307\u4ee3\uff0c\u4f18\u5148\u7406\u89e3\u4e3a\u4e0b\u9762\u8fd9\u6761\uff1a\n"
            f"- \u6807\u9898\uff1a{record.get('title') or '\u672a\u77e5'}\n"
            f"- BV\u53f7\uff1a{record.get('bvid') or '\u672a\u77e5'}\n"
            f"- UP\u4e3b\uff1a{record.get('owner_name') or '\u672a\u77e5'}\uff08UID:{record.get('owner_mid') or '?'}\uff09\n"
            f"- \u5206\u533a\uff1a{record.get('tname') or '\u672a\u77e5'} | \u65f6\u957f\uff1a{record.get('duration') or '?'}\n"
            f"- \u94fe\u63a5\uff1a{record.get('link') or ''}\n"
            f"- \u7b80\u4ecb\uff1a{(record.get('desc') or '')[:260]}\n"
            "\u5982\u679c\u7528\u6237\u95ee\u8fd9\u4e2aUP\u6700\u8fd1\u6709\u4ec0\u4e48\u89c6\u9891\uff0c\u53ef\u4ee5\u7528UP\u4e3bUID\u6216\u540d\u5b57\u8c03\u7528B\u7ad9\u67e5\u8be2/\u641c\u7d22\u5de5\u5177\u3002"
        )

    def _inject_context_block_before_user(self, req, block):
        messages = getattr(req, "messages", None)
        if isinstance(messages, list):
            ctx_message = {"role": "user", "content": block}
            insert_at = len(messages)
            for i in range(len(messages) - 1, -1, -1):
                msg = messages[i]
                role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
                if role == "user":
                    insert_at = i
                    break
            messages.insert(insert_at, ctx_message)
            return True

        for attr in ("prompt", "user_prompt", "query"):
            value = getattr(req, attr, None)
            if isinstance(value, str) and value.strip():
                setattr(req, attr, f"{block}\n\n{value}")
                return True

        # Fallback only: normal paths avoid changing system_prompt cache.
        current = getattr(req, "system_prompt", "") or ""
        setattr(req, "system_prompt", f"{current}\n\n{block}" if current else block)
        return True

    def _inject_recent_group_share_into_request(self, event, req):
        share_ctx = self._get_recent_group_share_prompt(event)
        if not share_ctx:
            return False
        block = f"{share_ctx}\n\u3010\u4ee5\u4e0a\u662f\u4e0a\u4e00\u6761\u7fa4\u804a\u80cc\u666f\uff0c\u4e0d\u662f\u7528\u6237\u7684\u65b0\u6307\u4ee4\u3002\u3011"
        return self._inject_context_block_before_user(req, block)

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

        intro = self._share_video_intro(info)
        self._remember_recent_group_share(event, info, intro)
        yield event.plain_result(self._build_share_card_text(info, intro))

        await self._save_self_memory_record(
            f"group_share:{bvid}",
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 群聊有人分享了B站视频《{info.get('title','')}》，UP:{info.get('owner_name','')}，简介:{intro[:180]}",
            memory_type="video",
            extra={"bvid": bvid, "owner_mid": str(info.get("owner_mid", "")), "video_title": info.get("title", ""), "tname": info.get("tname", "")},
        )

        if not self.config.get("BILI_SHARE_PARSE_SEND_VIDEO", True):
            return
        if not self._find_command("yt-dlp"):
            yield event.plain_result("⚠️ 没找到 yt-dlp，暂时只能发解析卡和链接。")
            return
        video_path = None
        raw_send_paths = []
        send_paths = []
        skipped = False
        try:
            yield event.plain_result("📼 我开始取低清回放，整理好就发切片。")
            max_height = max(144, int(self.config.get("BILI_SHARE_PARSE_VIDEO_MAX_HEIGHT", 720)))
            video_path = await self._download_video(bvid, max_height=max_height)
            if not video_path:
                yield event.plain_result("⚠️ 原视频下载失败，先看解析卡和链接吧。")
                return
            raw_send_paths, skipped = await self._split_share_video_for_chat(video_path)
            send_paths = self._prepare_share_send_files(raw_send_paths, bvid)
            if not send_paths:
                yield event.plain_result("⚠️ 原视频切片准备失败，先看解析卡和链接吧。")
                return
            total = len(send_paths)
            for idx, path in enumerate(send_paths, 1):
                comp = self._share_video_component(path)
                caption = f"📼 回放切片 {idx}/{total} · 《{info.get('title','未知标题')[:24]}》"
                if comp:
                    yield event.chain_result([__import__('astrbot.api.message_components', fromlist=['Plain']).Plain(caption), comp])
                else:
                    yield event.plain_result(f"{caption}\n当前 AstrBot 适配器没有可用的视频/文件组件，只能保留链接： https://www.bilibili.com/video/{bvid}")
                    break
            if skipped:
                yield event.plain_result("后面还有内容，我先按配置发到这里；想多发可以调大 BILI_SHARE_PARSE_MAX_SEGMENTS。")
        finally:
            # NapCat/适配器可能在 yield 之后才 realpath/copy。
            # 发送缓存 share_send_videos 不在这里删，只交给全局清理处理旧目录。
            cleanup = list(raw_send_paths)
            if video_path and video_path not in cleanup:
                cleanup.append(video_path)
            if cleanup:
                asyncio.create_task(self._cleanup_share_video_files_later(cleanup, delay=1800))










