"""B站分享解析：识别群聊/私聊链接与小程序，生成解析卡并可发送原视频切片。"""
import asyncio
import html
import json
import os
import re
import shutil
import time
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse, urljoin

import aiohttp
from .network import plugin_http_session
from astrbot.api import logger

from .config import TEMP_VIDEO_DIR, VIDEO_MEMORY_FILE
from .content_protocol import (
    ContentProtocolError, SHARE_REACTION_SCHEMA_PROMPT, SHARE_WATCH_SCHEMA_PROMPT,
    parse_share_reaction, parse_share_watch_decision,
    TOOL_WATCH_FOLLOWUP_SCHEMA_PROMPT,
)
from .security.redact import wrap_untrusted


class ShareMixin:
    """处理群聊和私聊里的 B站视频分享。"""

    BVID_RE = re.compile(r"\b(BV[0-9A-Za-z]{10,})\b")
    AV_RE = re.compile(r"(?:\bav|aid=)(\d+)\b", re.IGNORECASE)
    URL_RE = re.compile(r"https?://[^\s\]\[\)\(\"'<>]+", re.IGNORECASE)
    CQ_JSON_RE = re.compile(r"\[CQ:json,data=(.*?)\]", re.IGNORECASE | re.DOTALL)
    MINIAPP_KEYS = (
        "url", "jumpUrl", "jump_url", "qqdocurl", "preview", "sourceUrl",
        "pagepath", "pagePath", "webUrl", "web_url", "shareUrl", "share_url",
        "title", "desc", "content", "text", "summary",
    )

    def _collect_share_text(self, event, include_reply=False):
        parts = [event.message_str or ""]
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if raw:
                parts.append(str(raw))
        except Exception:
            pass
        try:
            for comp in getattr(event.message_obj, "message", []) or []:
                is_reply = (
                    comp.__class__.__name__.lower() == "reply"
                    or (
                        isinstance(comp, dict)
                        and str(comp.get("type", "")).lower() == "reply"
                    )
                )
                if is_reply and not include_reply:
                    continue
                parts.append(str(comp))
                if isinstance(comp, dict):
                    parts.extend(self._flatten_share_payload(comp))
                    continue
                attrs = (*self.MINIAPP_KEYS, "data", "meta")
                if include_reply:
                    attrs = (*attrs, "message_str")
                for attr in attrs:
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

    @staticmethod
    def _is_allowed_share_host(url):
        """严格主机校验：仅允许 B站官方域名，杜绝子串匹配绕过。

        旧实现用 ``host in url.lower()`` 判断，``http://127.0.0.1/b23.tv/x``
        或 ``b23.tv.evil.com`` 都会通过。这里改为对 ``urlparse`` 出的
        hostname 做精确等值/后缀比较。
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() not in {"http", "https"} or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
                return False
            hostname = (parsed.hostname or "").lower().rstrip(".")
        except Exception:
            return False
        if hostname in ("b23.tv", "bili2233.cn", "bilibili.com"):
            return True
        return hostname.endswith(".bilibili.com")

    async def _resolve_share_url(self, url):
        # Check every hop BEFORE making the next request; never send login cookies.
        original = url
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BiliBot",
            "Referer": "https://www.bilibili.com",
            "Accept": "text/html,application/xhtml+xml",
        }
        try:
            async with plugin_http_session(self.config) as session:
                for _ in range(5):
                    if not self._is_allowed_share_host(url):
                        return original
                    async with session.get(url, headers=headers, allow_redirects=False,
                                           timeout=aiohttp.ClientTimeout(total=10)) as response:
                        current = str(response.url)
                        if not self._is_allowed_share_host(current):
                            return original
                        if getattr(response, "status", 200) not in {301, 302, 303, 307, 308}:
                            return current
                        location = response.headers.get("Location", "")
                        target = urljoin(current, location)
                        if not location or not self._is_allowed_share_host(target):
                            return original
                        url = target
        except Exception as exc:
            logger.debug(f"[BiliBot] B站短链展开失败: {type(exc).__name__}")
        return original

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
            # 严格主机校验：伪造路径（如 127.0.0.1/b23.tv/x）或仿冒域名
            # （b23.tv.evil.com）一律不请求，避免 Cookie 外带与 SSRF。
            if not self._is_allowed_share_host(url):
                continue
            direct = self._target_from_url(url)
            if direct:
                return direct
            if any(host in url.lower() for host in ("b23.tv", "bili2233.cn")):
                resolved = await self._resolve_share_url(url)
                # 重定向后的最终地址也要过主机校验，防止短链跳到站外。
                if not self._is_allowed_share_host(resolved):
                    continue
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
        bvid = str(info.get("bvid", "") or "")
        cached = self._cached_video_memory(bvid) if bvid else None
        if cached:
            content = cached.get("analysis") or cached.get("summary", "")
            review = cached.get("review", "")
            rating = f"\n当时评分：{cached.get('score')}/10；理由：{cached.get('score_reason', '')}" if cached.get("score") is not None else ""
            faded = "（细节已淡忘，仅保留摘要）" if cached.get("memory_stage") == "faded" else ""
            return "【从观看记忆读取】" + faded + "\n" + content + rating + (f"\n当时的感想：{review}" if review else "")
        if not self.config.get("BILI_SHARE_PARSE_ANALYZE", True):
            return (info.get("desc") or "这个视频没有简介。")[:220]
        if bvid:
            result = await self._watch_video_and_save_memory(bvid, memory_source="qq_share")
            if result.get("ok"):
                return result.get("message", "")
            return "暂未完成视频分析：" + result.get("message", "内容获取失败")
        return "仅有视频简介，尚未确认完整内容：" + (info.get("desc") or "暂无简介")[:220]

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
            lines.append("\n请稍等...视频一会发出")
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

    def _share_forward_component(self, event, components):
        """One forward bundle, with exactly one video/file per node."""
        try:
            from astrbot.api.message_components import Node, Nodes

            try:
                self_id = str(event.get_self_id() or "0")
            except (AttributeError, TypeError):
                self_id = str(getattr(getattr(event, "message_obj", None), "self_id", "0") or "0")
            return Nodes([
                Node(uin=self_id, name="BiliBot", content=[component])
                for component in components
            ])
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            logger.warning(f"[BiliBot] 当前适配器无法构建视频合集，回退逐段发送: {type(exc).__name__}")
            return None

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

    def _remember_pending_bili_share(self, event, text):
        """记住本会话最近出现的B站视频，供稍后的手动/LLM解析使用。"""
        if not hasattr(self, "_pending_bili_shares"):
            self._pending_bili_shares = {}
        record = {"time": time.time(), "text": str(text or "")[:4000]}
        for key in self._share_context_keys(event):
            self._pending_bili_shares[key] = record

    def _get_pending_bili_share_text(self, event):
        pending = getattr(self, "_pending_bili_shares", {})
        if not pending:
            return ""
        max_age = max(60, int(self.config.get("BILI_SHARE_PENDING_MAX_AGE", 1800)))
        now = time.time()
        for key in self._share_context_keys(event):
            record = pending.get(key)
            if not record:
                continue
            if now - float(record.get("time", 0) or 0) <= max_age:
                return str(record.get("text", "") or "")
            pending.pop(key, None)
        return ""

    def _share_scene(self, event):
        """Return the storage source and user-facing scene for this share."""
        try:
            origin = str(event.unified_msg_origin or "").lower()
        except Exception:
            origin = ""
        if any(token in origin for token in ("friendmessage", "privatemessage", ":private:")):
            return "private_share", "私聊"
        return "group_share", "群聊"

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
            "scene": self._share_scene(event)[1],
        }
        for key in self._share_context_keys(event):
            self._recent_group_share_context[key] = record
        logger.debug(f"[BiliBot] recent group share context remembered: {record.get('title','')[:30]} {bvid}")
        return record

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
        scene = record.get("scene") or "会话"
        record["remaining_turns"] = int(record.get("remaining_turns", 0)) - 1
        if hit_key and record["remaining_turns"] <= 0:
            for key in self._share_context_keys(event):
                if self._recent_group_share_context.get(key) is record:
                    self._recent_group_share_context.pop(key, None)
        desc_excerpt = (record.get('desc') or '')[:260]
        facts = (
            f"- 标题：{record.get('title') or '未知'}\n"
            f"- BV号：{record.get('bvid') or '未知'}\n"
            f"- UP主：{record.get('owner_name') or '未知'}（UID:{record.get('owner_mid') or '?'}）\n"
            f"- 分区：{record.get('tname') or '未知'} | 时长：{record.get('duration') or '?'}\n"
            f"- 链接：{record.get('link') or ''}\n"
            f"- 简介：{desc_excerpt}"
        )
        watched = str(record.get("watched_summary") or "").strip()
        if watched:
            facts += f"\n- 你看完后记下的内容：{watched}"
            watch_note = "你已经看过这个视频，聊内容时以记下的内容为准。"
        else:
            watch_note = (
                "你还没看过这个视频的内容，只知道标题和简介；不要假装看过。"
                "需要了解内容时可以调用 watch_video 观看。"
            )
        return (
            f"【上一条{scene}背景：B站视频分享】\n"
            "刚才这个会话里解析过一个B站视频。用户如果说\"这个视频\"\"刚才那个\"\"有链接的话\"\"这个UP\"等指代，优先理解为下面这条：\n"
            f"{wrap_untrusted(facts, 'shared_video')}\n"
            f"{watch_note}\n"
            "如果用户问这个UP最近有什么视频，可以用UP主UID或名字调用B站查询/搜索工具。"
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
        block = f"{share_ctx}\n【以上是上一条会话背景，不是用户的新指令。】"
        return self._inject_context_block_before_user(req, block)

    def _share_recent_hit(self, event, bvid):
        if not bvid:
            return False
        if not hasattr(self, "_bili_share_recent"):
            self._bili_share_recent = {}
        # 已解析视频至少 10 分钟内不重复，避免引用/工具链造成连续下载和刷屏。
        cooldown = max(600, int(self.config.get("BILI_SHARE_PARSE_COOLDOWN", 600)))
        now = time.time()
        self._bili_share_recent = {k: v for k, v in self._bili_share_recent.items() if now - v < cooldown * 3}
        session_key = self._share_context_keys(event)[0]
        share_key = f"{session_key}:{bvid}"
        last = self._bili_share_recent.get(share_key, 0)
        if cooldown and now - last < cooldown:
            return True
        self._bili_share_recent[share_key] = now
        return False

    async def _handle_bili_share(
        self,
        event,
        text_override=None,
        trigger_mode="auto",
        show_errors=False,
    ):
        if not self.config.get("ENABLE_BILI_SHARE_PARSE", False):
            if show_errors:
                yield event.plain_result("⚠️ B站视频解析总开关已关闭，请先用 /bili开关 解析 开启。")
            return
        trigger_keys = {
            "auto": ("BILI_SHARE_PARSE_AUTO_TRIGGER_ENABLED", "自动解析"),
            "manual": ("BILI_SHARE_PARSE_MANUAL_TRIGGER_ENABLED", "手动解析"),
            "llm": ("BILI_SHARE_PARSE_LLM_TRIGGER_ENABLED", "LLM解析"),
        }
        trigger_key, trigger_label = trigger_keys.get(trigger_mode, trigger_keys["auto"])
        msg = (event.message_str or "").strip()
        if text_override is None and (
            msg.startswith("/") or re.match(r"^bili解析(?:\s|$)", msg, re.IGNORECASE)
        ):
            return
        text = str(text_override) if text_override is not None else self._collect_share_text(event)
        if not text.strip() and trigger_mode in ("manual", "llm"):
            text = self._get_pending_bili_share_text(event)
        if not text.strip():
            if show_errors:
                yield event.plain_result("⚠️ 没有找到需要解析的B站链接或BV号。")
            return
        target = await self._extract_bili_share_target(text)
        if trigger_mode == "auto" and target:
            # 自动解析关闭时也记录目标，之后可用 /bili解析 或 LLM 解析“上面那个”。
            self._remember_pending_bili_share(event, text)
        if not self.config.get(trigger_key, True):
            if show_errors:
                yield event.plain_result(
                    f"⚠️ {trigger_label}触发已关闭，请先用 /bili开关 {trigger_label} 开启。"
                )
            return
        if not target and trigger_mode in ("manual", "llm"):
            pending_text = self._get_pending_bili_share_text(event)
            if pending_text:
                text = pending_text
                target = await self._extract_bili_share_target(text)
        if not target:
            if show_errors:
                yield event.plain_result("⚠️ 没有识别到有效的B站视频链接、短链或BV号。")
            return
        info = await self._get_video_info_by_share_target(target)
        if not info or not info.get("bvid"):
            if show_errors:
                yield event.plain_result("⚠️ 获取B站视频信息失败，请检查链接或稍后重试。")
            return
        bvid = info.get("bvid", "")
        if self._share_recent_hit(event, bvid):
            if show_errors:
                yield event.plain_result("⚠️ 这个视频刚刚解析过，冷却结束后可以再次解析。")
            return

        intro = self._share_video_intro(info)
        share_record = self._remember_recent_group_share(event, info, intro)
        yield event.plain_result(self._build_share_card_text(info, intro))

        # 解析只是把视频下载发出去，不算看过，也不写长期记忆；“上面那个视频”靠 share_record 的会话上下文。
        # 看不看由 Bot 自己决定，和下载发送并行；评价要等视频发出后才能引用它。
        reply_target = asyncio.get_running_loop().create_future()
        self._start_share_reaction(event, info, share_record, reply_target)
        sent_message_id = None
        try:
            async for result in self._send_shared_video(event, bvid, info.get("duration")):
                if isinstance(result, dict):
                    sent_message_id = result.get("message_id") or sent_message_id
                else:
                    yield result
        finally:
            if not reply_target.done():
                reply_target.set_result(sent_message_id or self._event_message_id(event))

    async def _send_shared_video(self, event, bvid, duration=None):
        """Download and send the original video; a dict result reports its message id."""
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
            max_height = max(144, int(self.config.get("BILI_SHARE_PARSE_VIDEO_MAX_HEIGHT", 720)))
            video_path = await self._download_video(bvid, max_height=max_height, duration=duration)
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
                comp = self._share_video_component(path)
                if not comp:
                    yield event.plain_result(f"当前 AstrBot 适配器没有可用的视频/文件组件，只能保留链接： https://www.bilibili.com/video/{bvid}")
                    return
                components.append(comp)
            # 单段直接发；多段用一个聊天记录合集，每个节点仅含视频，不能混入文字。
            if len(components) == 1:
                chain = components
            else:
                forward = self._share_forward_component(event, components)
                chain = [forward] if forward is not None else None
            if chain is None:
                yield event.plain_result("当前适配器不支持视频聊天记录合集，改为逐段发送。")
                for comp in components:
                    yield event.chain_result([comp])
            else:
                handled, message_id = await self._send_onebot_chain(event, chain[0])
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
                self._share_cleanup_tasks = getattr(self, "_share_cleanup_tasks", set())
                self._share_cleanup_tasks.add(task)
                task.add_done_callback(self._share_cleanup_tasks.discard)

    @staticmethod
    def _event_message_id(event):
        message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        return str(message_id) if message_id not in (None, "") else None

    async def _send_onebot_chain(self, event, component):
        """Send one media component through OneBot so its message id can be quoted.

        Returns ``(handled, message_id)``. ``handled`` is False when the adapter is
        not OneBot, so the caller falls back to the normal AstrBot send path. Once a
        send was attempted it is never retried, because it may already have arrived;
        a failed send reports ``None`` and a send without a returned id reports "".
        """
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        try:
            platform = str(event.get_platform_name() or "").lower()
        except Exception:
            platform = ""
        if platform != "aiocqhttp" or not callable(call_action):
            return False, None
        try:
            group_id = str(event.get_group_id() or "").strip()
            target = {"group_id": int(group_id)} if group_id else {"user_id": int(event.get_sender_id())}
            if getattr(component, "type", "") in ("nodes", "node") or type(component).__name__ in ("Nodes", "Node"):
                payload = component.to_dict()
                if asyncio.iscoroutine(payload):
                    payload = await payload
                action = "send_group_forward_msg" if group_id else "send_private_forward_msg"
                params = {**payload, **target}
            else:
                convert = getattr(type(event), "_from_segment_to_dict", None)
                segment = convert(component) if callable(convert) else component.to_dict()
                if asyncio.iscoroutine(segment):
                    segment = await segment
                action = "send_group_msg" if group_id else "send_private_msg"
                params = {**target, "message": [segment]}
        except Exception as exc:
            logger.debug(f"[BiliBot] 无法直接构造 OneBot 视频消息，改用普通发送: {exc}")
            return False, None
        try:
            ret = await call_action(action, **params)
        except Exception as exc:
            logger.warning(f"[BiliBot] 分享视频发送失败: {exc}")
            return True, None
        message_id = ret.get("message_id") if isinstance(ret, dict) else None
        return True, str(message_id) if message_id not in (None, "") else ""

    def _start_share_reaction(self, event, info, share_record, reply_target):
        if not self.config.get("BILI_SHARE_PARSE_AUTO_WATCH", True):
            return
        task = asyncio.create_task(self._react_to_shared_video(
            str(getattr(event, "unified_msg_origin", "") or ""),
            info,
            share_record,
            reply_target,
            scene=self._share_scene(event)[1],
            user_text=str(getattr(event, "message_str", "") or ""),
        ))
        self._share_reaction_tasks = getattr(self, "_share_reaction_tasks", set())
        self._share_reaction_tasks.add(task)
        task.add_done_callback(self._share_reaction_tasks.discard)

    async def _qq_session_provider_id(self, umo):
        """The chat model this QQ conversation is currently using."""
        getter = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(getter):
            try:
                provider_id = getter(umo=umo)
                if asyncio.iscoroutine(provider_id):
                    provider_id = await provider_id
                if provider_id:
                    return str(provider_id)
            except Exception as exc:
                logger.debug(f"[BiliBot] 读取当前会话模型失败: {exc}")
        getter = getattr(self.context, "get_using_provider", None)
        if callable(getter):
            for kwargs in ({"umo": umo}, {}):
                try:
                    provider = getter(**kwargs)
                    meta = provider.meta() if provider is not None else None
                    if getattr(meta, "id", None):
                        return str(meta.id)
                except TypeError:
                    continue
                except Exception as exc:
                    logger.debug(f"[BiliBot] 读取当前会话模型失败: {exc}")
                    break
        return ""

    async def _qq_session_persona_prompt(self, umo):
        """The persona of this QQ conversation: its selected persona, else the default."""
        personas = getattr(self.context, "persona_manager", None)
        conversations = getattr(self.context, "conversation_manager", None)
        persona_id = None
        try:
            cid = await conversations.get_curr_conversation_id(umo)
            conversation = await conversations.get_conversation(umo, cid) if cid else None
            persona_id = getattr(conversation, "persona_id", None)
        except Exception as exc:
            logger.debug(f"[BiliBot] 读取当前会话人格失败: {exc}")
        if persona_id == "[%None]":
            return ""
        if persona_id:
            try:
                persona = await personas.get_persona(persona_id)
                prompt = getattr(persona, "system_prompt", None)
                if prompt is None and isinstance(persona, dict):
                    prompt = persona.get("prompt")
                if prompt:
                    return str(prompt).strip()
            except Exception as exc:
                logger.debug(f"[BiliBot] 读取会话人格 {persona_id} 失败: {exc}")
        try:
            persona = await personas.get_default_persona_v3(umo=umo)
        except TypeError:
            persona = await personas.get_default_persona_v3()
        except Exception as exc:
            logger.debug(f"[BiliBot] 读取默认人格失败: {exc}")
            return ""
        prompt = persona.get("prompt") if isinstance(persona, dict) else getattr(persona, "prompt", "")
        return str(prompt or "").strip()

    async def _decide_share_watch(self, info, *, scene, user_text, persona, provider_id):
        video = {
            "title": info.get("title", ""),
            "up": info.get("owner_name", ""),
            "partition": info.get("tname", ""),
            "duration": self._format_duration(info.get("duration", 0)),
            "desc": self._share_video_intro(info),
        }
        prompt = (
            f"有人在QQ{scene}里分享了一个B站视频，解析卡和原视频已经发出来了。"
            "现在由你决定自己要不要点开看。\n"
            f"{wrap_untrusted(json.dumps(video, ensure_ascii=False), 'shared_video')}\n"
        )
        if user_text.strip():
            prompt += f"分享时的消息：\n{wrap_untrusted(user_text[:300], 'share_message')}\n"
        prompt += "看视频要花些时间；只是没兴趣也完全可以不看。\n" + SHARE_WATCH_SCHEMA_PROMPT
        raw = await self._llm_call(prompt, system_prompt=persona, max_tokens=60, provider_id=provider_id)
        if not raw:
            return False
        try:
            return parse_share_watch_decision(raw)["decision"] == "watch"
        except ContentProtocolError as exc:
            logger.warning(f"[BiliBot] 分享视频观看决定结构校验失败，不看: {exc}")
            return False

    async def _share_reaction_text(self, watch_message, *, scene, persona, provider_id):
        prompt = (
            f"你看完了对方在QQ{scene}里分享的这个B站视频，要引用这条视频随口评价一句。\n"
            f"{wrap_untrusted(watch_message[:1600], 'watch_result')}\n"
            "只说一句，基于真正看到的内容；不复述标题，不带链接，不提分析、评分这些内部过程。\n"
            + SHARE_REACTION_SCHEMA_PROMPT
        )
        raw = await self._llm_call(prompt, system_prompt=persona, max_tokens=120, provider_id=provider_id)
        if not raw:
            return ""
        try:
            parsed = parse_share_reaction(raw)
        except ContentProtocolError as exc:
            logger.warning(f"[BiliBot] 分享视频评价结构校验失败，保持沉默: {exc}")
            return ""
        return parsed["text"] if parsed["decision"] == "comment" else ""

    async def _react_to_shared_video(self, umo, info, share_record, reply_target, *, scene, user_text):
        """Decide to watch a parsed share; if watched, quote the sent video with one remark."""
        bvid = str(info.get("bvid", "") or "")
        try:
            provider_id = await self._qq_session_provider_id(umo)
            if not provider_id:
                logger.info("[BiliBot] 找不到当前 QQ 会话的聊天模型，跳过分享视频观看")
                return
            persona = await self._qq_session_persona_prompt(umo)
            # 看过且记得内容就不用再决定；只剩“看过但忘了”的痕迹时不重看也不评价。
            if not self._cached_video_memory(bvid):
                if await self._has_seen_video(bvid):
                    return
                if not await self._decide_share_watch(
                    info, scene=scene, user_text=user_text, persona=persona, provider_id=provider_id
                ):
                    logger.info(f"[BiliBot] 分享视频决定不看：{bvid}")
                    return
            result = await self._watch_video_and_save_memory(bvid, memory_source="qq_share")
            if not result.get("ok") or result.get("seen_only"):
                return
            cached = self._cached_video_memory(bvid) or {}
            if isinstance(share_record, dict):
                share_record["watched_summary"] = str(
                    cached.get("summary") or cached.get("analysis") or ""
                )[:260]
            text = await self._share_reaction_text(
                result.get("message", ""), scene=scene, persona=persona, provider_id=provider_id
            )
            if not text:
                return
            try:
                reply_to = await asyncio.wait_for(asyncio.shield(reply_target), timeout=1800)
            except asyncio.TimeoutError:
                reply_to = None
            await self._send_share_reaction(umo, text, reply_to)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[BiliBot] 分享视频观看/评价失败: {exc}")

    async def _tool_watch_followup_text(self, watch_message, *, scene, user_text, persona, provider_id):
        prompt = (
            f"刚才在QQ{scene}里，对方让你看的B站视频你转到后台去看了，现在有结果了，要主动接着说一句。\n"
            f"{wrap_untrusted(watch_message[:1600], 'watch_result')}\n"
        )
        if user_text.strip():
            prompt += f"对方当时的消息：\n{wrap_untrusted(user_text[:300], 'share_message')}\n"
        prompt += (
            "看完了就基于真正看到的内容说一句；结果显示没看成就如实说没看成，不要编内容。"
            "不复述标题，不带链接，不提分析、评分这些内部过程。\n"
            + TOOL_WATCH_FOLLOWUP_SCHEMA_PROMPT
        )
        raw = await self._llm_call(prompt, system_prompt=persona, max_tokens=120, provider_id=provider_id)
        if not raw:
            return ""
        try:
            parsed = parse_share_reaction(raw)
        except ContentProtocolError as exc:
            logger.warning(f"[BiliBot] 工具看片结果消息结构校验失败，改发固定提示: {exc}")
            return ""
        return parsed["text"] if parsed["decision"] == "comment" else ""

    async def _send_tool_watch_followup(self, umo, watch_message, *, scene, user_text, failed=False):
        """Tell the conversation that asked how a backgrounded watch ended."""
        text = "刚才那个视频没看成，晚点再试试吧。" if failed else ""
        if not failed:
            try:
                provider_id = await self._qq_session_provider_id(umo)
                if provider_id:
                    persona = await self._qq_session_persona_prompt(umo)
                    text = await self._tool_watch_followup_text(
                        watch_message, scene=scene, user_text=user_text,
                        persona=persona, provider_id=provider_id,
                    )
            except Exception as exc:
                logger.warning(f"[BiliBot] 工具看片结果消息生成失败，改发固定提示: {exc}")
        # 用户还在等结果，模型说不出话也要告诉一声；固定文字不含模型输出。
        await self._send_share_reaction(umo, text or "刚才那个视频有结果了，可以再问问我。", None)

    async def _send_share_reaction(self, umo, text, reply_to):
        import astrbot.api.message_components as Comp
        from astrbot.api.event import MessageChain

        chain = [Comp.Plain(text)]
        if reply_to:
            chain.insert(0, Comp.Reply(id=str(reply_to)))
        await self.context.send_message(umo, MessageChain(chain))










