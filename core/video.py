"""视频分析：内容概括、媒体处理、视频/动态上下文构建。"""
import os
import base64
import shutil
from datetime import datetime
from astrbot.api import logger
from .config import VIDEO_MEMORY_FILE, TEMP_VIDEO_DIR


class VideoMixin:
    """视频分析、下载、截帧、上下文。"""

    # ── 补充上下文（标签+热评+联网搜索） ──
    async def _enrich_video_context(self, video_info):
        bvid = video_info.get("bvid", "")
        oid = video_info.get("oid") or (await self._get_video_oid(bvid) if bvid else None)
        tags = await self._get_video_tags(bvid) if bvid else []
        comments = await self._get_hot_comments(oid) if oid else []
        extra = ""
        if tags:
            extra += f"\n标签：{'、'.join(tags[:10])}"
        if comments:
            extra += "\n热门评论：\n" + "\n".join([f"- {c}" for c in comments[:5]])
        if self.config.get("ENABLE_WEB_SEARCH", False):
            search_query = await self._should_search_for_video(video_info, extra)
            if search_query:
                search_result = await self._web_search(search_query)
                if search_result:
                    extra += f"\n\n【联网搜索补充】\n{search_result[:800]}"
                    logger.info(f"[BiliBot] 🔍 视频搜索补充完成: {search_query[:40]} -> {len(search_result)}字")
        return extra

    # ── 视频分析 ──
    async def _analyze_video_with_vision(self, video_info):
        media_result = await self._analyze_video_media(video_info)
        if media_result:
            return media_result
        client = self._get_video_vision_client()
        model = self.config.get("VIDEO_VISION_MODEL", "")
        dur_min = video_info.get("duration", 0) // 60
        dur_sec = video_info.get("duration", 0) % 60
        extra_context = await self._enrich_video_context(video_info)
        text_prompt = f"""请根据以下B站视频信息，写一段简洁的内容概括（300字以内），包括：这个视频大概在讲什么、是什么类型/风格、可能的受众。

视频标题：{video_info.get('title', '未知')}
UP主：{video_info.get('owner_name', '未知')}
分区：{video_info.get('tname', '未知')}
时长：{dur_min}分{dur_sec}秒
简介：{video_info.get('desc', '无')[:500]}{extra_context}

直接输出概括内容，不要加前缀。"""
        provider_id = self.config.get("VIDEO_VISION_PROVIDER_ID", "")
        provider_result = await self._astrbot_multimodal_generate(provider_id, [{"type": "text", "text": text_prompt}], max_tokens=250)
        if provider_result:
            return provider_result
        if client and model and video_info.get("pic"):
            try:
                b64 = await self._fetch_image_base64(video_info["pic"])
                if b64:
                    content = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}, {"type": "text", "text": text_prompt}]
                    result = await self._vision_call(client, model, content, max_tokens=250)
                    if result:
                        return result
            except Exception as e:
                logger.warning(f"[BiliBot] 视觉分析封面失败: {e}")
        result = await self._llm_call(text_prompt, max_tokens=250)
        return result or f"视频《{video_info.get('title', '未知')}》，UP主：{video_info.get('owner_name', '未知')}，分区：{video_info.get('tname', '未知')}。简介：{video_info.get('desc', '无')[:100]}"

    async def _analyze_video_text(self, video_info):
        extra_context = await self._enrich_video_context(video_info)
        prompt = f"""请根据以下B站视频信息，写一段简洁的内容概括（300字以内），包括：这个视频大概在讲什么、是什么类型/风格、可能的受众。

视频标题：{video_info.get('title', '未知')}
UP主：{video_info.get('up_name', '未知')}
分区：{video_info.get('tname', '未知')}
简介：{video_info.get('desc', '无')[:500]}{extra_context}

直接输出概括内容，不要加前缀。"""
        result = await self._llm_call(prompt, max_tokens=250)
        return result or f"视频《{video_info.get('title', '未知')}》，UP主：{video_info.get('up_name', '未知')}"

    async def _analyze_video_media(self, video_info):
        provider_id = self.config.get("VIDEO_VISION_PROVIDER_ID", "")
        client = self._get_video_vision_client()
        model = self.config.get("VIDEO_VISION_MODEL", "")
        if not provider_id and (not client or not model):
            return None
        bvid = video_info.get("bvid", "")
        if not bvid:
            return None
        video_path = await self._download_video(bvid)
        if not video_path:
            return None
        frames = []
        compressed_path = video_path
        try:
            compressed_path = await self._compress_video(video_path)
            with open(compressed_path, "rb") as f:
                video_b64 = base64.b64encode(f.read()).decode()
            text_prompt = (
                f"这是一个B站视频，标题是「{video_info.get('title', '未知')}」，"
                f"简介是「{video_info.get('desc', '无')[:300]}」。"
                "请用100字以内描述视频的主要内容、风格和亮点。"
            )
            content = [
                {"type": "image_url", "image_url": {"url": f"data:video/mp4;base64,{video_b64}"}},
                {"type": "text", "text": text_prompt},
            ]
            result = await self._astrbot_multimodal_generate(provider_id, content, max_tokens=200)
            if not result and client and model:
                result = await self._vision_call(client, model, content, max_tokens=200)
            if result:
                return result
            logger.warning(f"[BiliBot] 视频直读失败，回退截帧：{bvid}")
            frames = await self._extract_video_frames(compressed_path, count=5)
            if not frames:
                return None
            frame_content = []
            for frame_path in frames:
                with open(frame_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode()
                frame_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}})
            frame_content.append({
                "type": "text",
                "text": (
                    f"这些是一个B站视频的截图，标题是「{video_info.get('title', '未知')}」，"
                    f"简介是「{video_info.get('desc', '无')[:300]}」。"
                    "请用100字以内描述视频的主要内容、风格和亮点。"
                ),
            })
            result = await self._astrbot_multimodal_generate(provider_id, frame_content, max_tokens=200)
            if not result and client and model:
                result = await self._vision_call(client, model, frame_content, max_tokens=200)
            return result
        except Exception as e:
            logger.warning(f"[BiliBot] 视频媒体分析失败({bvid})：{e}")
            return None
        finally:
            self._cleanup_video_artifacts(compressed_path, frames)

    # ── 视频下载 / 压缩 / 截帧 ──
    async def _download_video(self, bvid):
        output_template = os.path.join(TEMP_VIDEO_DIR, f"{bvid}.%(ext)s")
        cookie_header = (
            f"Cookie: SESSDATA={self.config.get('SESSDATA', '')}; "
            f"bili_jct={self.config.get('BILI_JCT', '')}; "
            f"DedeUserID={self.config.get('DEDE_USER_ID', '')}"
        )
        code, _, stderr = await self._run_process(
            "yt-dlp", "-o", output_template,
            "--format", "bestvideo+bestaudio/best",
            "--no-playlist", "--merge-output-format", "mp4",
            "--recode-video", "mp4",
            "--add-header", cookie_header,
            "--add-header", "Referer: https://www.bilibili.com",
            f"https://www.bilibili.com/video/{bvid}",
            timeout=600,
        )
        if code != 0:
            logger.warning(f"[BiliBot] 视频下载失败({bvid}): {stderr[:200]}")
            return None
        for name in os.listdir(TEMP_VIDEO_DIR):
            fp = os.path.join(TEMP_VIDEO_DIR, name)
            if name.startswith(bvid) and os.path.isfile(fp):
                return fp
        return None

    async def _compress_video(self, input_path):
        output_path = input_path.rsplit(".", 1)[0] + "_compressed.mp4"
        code, _, stderr = await self._run_process(
            "ffmpeg", "-y", "-i", input_path,
            "-t", "30", "-vf", "scale=480:-2", "-an",
            "-c:v", "libx264", "-preset", "fast",
            output_path, timeout=600,
        )
        if code != 0:
            logger.warning(f"[BiliBot] 视频压缩失败，回退原视频: {stderr[:160]}")
            return input_path
        try:
            os.remove(input_path)
        except OSError:
            pass
        return output_path

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

    # ── 视频上下文（评论区用） ──
    async def _get_video_context(self, oid, comment_type):
        if comment_type != 1:
            return "", None
        vc = self._load_json(VIDEO_MEMORY_FILE, {})
        bvid = await self._oid_to_bvid(oid)
        if not bvid:
            return "", None
        if bvid in vc:
            c = vc[bvid]
            has_mem = any(m.get("bvid") == bvid or m.get("thread_id") == f"video:{bvid}" for m in self._memory)
            if not has_mem:
                mem_time = c.get("time", datetime.now().strftime("%Y-%m-%d %H:%M"))
                memory_text = (
                    f"[{mem_time}] 视频分析记忆：标题《{c['title']}》 "
                    f"UP主:{c['owner_name']} 分区:{c.get('tname', '')} "
                    f"简介:{c.get('desc', '')[:120]} 内容概括:{c.get('analysis', '')[:200]}"
                )
                await self._save_self_memory_record(
                    f"video:{bvid}", memory_text, memory_type="video",
                    extra={"bvid": bvid, "owner_mid": str(c.get("owner_mid", "")), "video_title": c["title"]},
                )
                logger.info(f"[BiliBot] 📹 补录视频记忆：《{c['title']}》")
            ctx = f"【当前视频】\n标题：{c['title']}\nUP主：{c['owner_name']}（UID:{c.get('owner_mid', '')}）\n分区：{c.get('tname', '')}\n简介：{c.get('desc', '')[:150]}\n内容概括：{c.get('analysis', '')}"
            tags = await self._get_video_tags(bvid)
            comments = await self._get_hot_comments(oid)
            if tags:
                ctx += f"\n标签：{'、'.join(tags[:10])}"
            if comments:
                ctx += "\n热门评论：" + " / ".join(comments[:3])
            return ctx, c
        vi = await self._get_video_info(oid)
        if not vi:
            return "", None
        logger.info(f"[BiliBot] 📹 新视频，分析中：《{vi['title']}》by {vi['owner_name']}")
        analysis = await self._analyze_video_with_vision(vi)
        logger.info(f"[BiliBot] 📹 分析结果：{analysis[:60]}...")
        analyzed_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        cache_entry = {"bvid": bvid, "title": vi["title"], "desc": vi.get("desc", "")[:200], "owner_name": vi["owner_name"], "owner_mid": str(vi["owner_mid"]), "tname": vi["tname"], "analysis": analysis, "time": analyzed_at}
        vc[bvid] = cache_entry
        self._save_json(VIDEO_MEMORY_FILE, vc)
        memory_text = (
            f"[{analyzed_at}] 视频分析记忆：标题《{vi['title']}》 "
            f"UP主:{vi['owner_name']} 分区:{vi['tname']} "
            f"简介:{vi.get('desc', '')[:120]} 内容概括:{analysis[:200]}"
        )
        await self._save_self_memory_record(
            f"video:{bvid}", memory_text, memory_type="video",
            extra={"bvid": bvid, "owner_mid": str(vi["owner_mid"]), "video_title": vi["title"]},
        )
        ctx = f"【当前视频】\n标题：{vi['title']}\nUP主：{vi['owner_name']}（UID:{vi['owner_mid']}）\n分区：{vi['tname']}\n简介：{vi.get('desc', '')[:150]}\n内容概括：{analysis}"
        tags = await self._get_video_tags(bvid)
        comments = await self._get_hot_comments(oid)
        if tags:
            ctx += f"\n标签：{'、'.join(tags[:10])}"
        if comments:
            ctx += "\n热门评论：" + " / ".join(comments[:3])
        return ctx, cache_entry

    # ── 动态上下文 ──
    async def _get_dynamic_context(self, oid):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail", params={"id": oid})
            if d.get("code") == 0:
                item = d.get("data", {}).get("item", {})
                modules = item.get("modules", {})
                desc = modules.get("module_dynamic", {}).get("desc", {})
                text = desc.get("text", "")
                author = modules.get("module_author", {})
                pub_time = author.get("pub_time", "")
                if text:
                    ctx = f"【当前动态（Bot自己发的）】\n内容：{text}"
                    if pub_time:
                        ctx += f"\n发布时间：{pub_time}"
                    return ctx
        except Exception as e:
            logger.debug(f"[BiliBot] 动态API获取失败: {e}")
        dynamic_mems = [m for m in self._memory if m.get("memory_type") == "dynamic"]
        if dynamic_mems:
            latest = dynamic_mems[-1]
            return f"【最近发布的动态】\n{latest.get('text', '')}"
        return ""
