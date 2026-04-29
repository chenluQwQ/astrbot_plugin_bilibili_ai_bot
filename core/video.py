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
        # 获取字幕
        subtitle_text = await self._get_video_subtitles(
            video_info.get("bvid", ""), video_info.get("cid", 0)
        )
        subtitle_section = ""
        if subtitle_text:
            subtitle_section = f"\n字幕内容：{subtitle_text[:1500]}"
        text_prompt = f"""请根据以下B站视频信息，写一段详细的内容概括（500字以内），包括：这个视频的主要内容和讲了什么、关键观点或亮点、视频类型/风格、可能的受众。

视频标题：{video_info.get('title', '未知')}
UP主：{video_info.get('owner_name', '未知')}
分区：{video_info.get('tname', '未知')}
时长：{dur_min}分{dur_sec}秒
简介：{video_info.get('desc', '无')[:500]}{extra_context}{subtitle_section}

直接输出概括内容，不要加前缀。"""
        provider_id = self.config.get("VIDEO_VISION_PROVIDER_ID", "")
        provider_result = await self._astrbot_multimodal_generate(provider_id, [{"type": "text", "text": text_prompt}], max_tokens=500)
        if provider_result:
            return provider_result
        if client and model and video_info.get("pic"):
            try:
                b64 = await self._fetch_image_base64(video_info["pic"])
                if b64:
                    content = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}, {"type": "text", "text": text_prompt}]
                    result = await self._vision_call(client, model, content, max_tokens=500)
                    if result:
                        return result
            except Exception as e:
                logger.warning(f"[BiliBot] 视觉分析封面失败: {e}")
        result = await self._llm_call(text_prompt, max_tokens=500)
        return result or f"视频《{video_info.get('title', '未知')}》，UP主：{video_info.get('owner_name', '未知')}，分区：{video_info.get('tname', '未知')}。简介：{video_info.get('desc', '无')[:100]}"

    async def _analyze_video_text(self, video_info):
        extra_context = await self._enrich_video_context(video_info)
        subtitle_text = await self._get_video_subtitles(
            video_info.get("bvid", ""), video_info.get("cid", 0)
        )
        subtitle_section = ""
        if subtitle_text:
            subtitle_section = f"\n字幕内容：{subtitle_text[:1500]}"
        prompt = f"""请根据以下B站视频信息，写一段详细的内容概括（500字以内），包括：这个视频的主要内容和讲了什么、关键观点或亮点、视频类型/风格、可能的受众。

视频标题：{video_info.get('title', '未知')}
UP主：{video_info.get('up_name', '未知')}
分区：{video_info.get('tname', '未知')}
简介：{video_info.get('desc', '无')[:500]}{extra_context}{subtitle_section}

直接输出概括内容，不要加前缀。"""
        result = await self._llm_call(prompt, max_tokens=500)
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
        # 生成 Netscape 格式 cookie 文件，兼容新版 yt-dlp
        cookie_file = os.path.join(TEMP_VIDEO_DIR, f"{bvid}_cookies.txt")
        sessdata = self.config.get('SESSDATA', '')
        bili_jct = self.config.get('BILI_JCT', '')
        dede_uid = self.config.get('DEDE_USER_ID', '')
        buvid3 = self.config.get('BUVID3', '')
        cookie_content = (
            "# Netscape HTTP Cookie File\n"
            f".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\t{sessdata}\n"
            f".bilibili.com\tTRUE\t/\tFALSE\t0\tbili_jct\t{bili_jct}\n"
            f".bilibili.com\tTRUE\t/\tFALSE\t0\tDedeUserID\t{dede_uid}\n"
        )
        if buvid3:
            cookie_content += f".bilibili.com\tTRUE\t/\tFALSE\t0\tbuvid3\t{buvid3}\n"
        try:
            with open(cookie_file, "w") as f:
                f.write(cookie_content)
        except Exception as e:
            logger.warning(f"[BiliBot] Cookie文件写入失败: {e}")
            return None
        try:
            code, _, stderr = await self._run_process(
                "yt-dlp", "-o", output_template,
                # 只下载 480p 以下，AI 分析不需要高画质，减少 CDN 请求量降低风控风险
                "--format", "bestvideo[height<=480]+bestaudio/best[height<=480]/worst",
                "--no-playlist", "--merge-output-format", "mp4",
                "--recode-video", "mp4",
                "--cookies", cookie_file,
                "--add-header", "Referer: https://www.bilibili.com",
                "--limit-rate", "2M",
                f"https://www.bilibili.com/video/{bvid}",
                timeout=600,
            )
        finally:
            try:
                os.remove(cookie_file)
            except OSError:
                pass
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
    async def _get_dynamic_context(self, oid, comment_type=17):
        # comment_type=11: 图文动态，oid是doc_id（相簿ID），用相簿API
        # comment_type=17: 纯文字动态，oid是dynamic_id，用动态详情API
        try:
            if comment_type == 11:
                # 图文动态：用相簿API获取内容（内部已含空间列表fallback）
                ctx = await self._get_draw_context(oid)
                if ctx:
                    return ctx
                # _get_draw_context 所有方案都失败了，直接走记忆兜底
                # 注意：oid 是 doc_id，不能当 dynamic_id 用，所以不走下面的详情API
            else:
                # 纯文字动态：oid 就是 dynamic_id，直接查详情API
                d, _ = await self._http_get("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail", params={
                    "id": oid, "timezone_offset": -480,
                    "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
                })
                if not isinstance(d, dict):
                    logger.debug(f"[BiliBot] 动态详情API返回非dict: {type(d)}")
                elif d.get("code") == 0:
                    item = (d.get("data") or {}).get("item") or {}
                    modules = item.get("modules") or {}
                    desc = (modules.get("module_dynamic") or {}).get("desc") or {}
                    text = desc.get("text", "")
                    author = modules.get("module_author") or {}
                    author_name = author.get("name", "")
                    author_mid = str(author.get("mid", ""))
                    pub_time = author.get("pub_time", "")
                    bot_mid = self.config.get("DEDE_USER_ID", "")
                    is_self = author_mid == bot_mid

                    # 提取图片（兼容opus和draw两种格式）
                    major = (modules.get("module_dynamic") or {}).get("major") or {}
                    major_type = major.get("type", "")
                    if major_type == "MAJOR_TYPE_OPUS" or "opus" in major:
                        opus = major.get("opus") or {}
                        opus_text = (opus.get("summary") or {}).get("text", "") or opus.get("title", "")
                        if opus_text and not text:
                            text = opus_text
                        image_urls = [p.get("url", "") for p in (opus.get("pics") or []) if p.get("url")]
                    elif "draw" in major:
                        draw = major.get("draw") or {}
                        image_urls = [img.get("src", "") for img in (draw.get("items") or []) if img.get("src")]
                    else:
                        image_urls = []

                    if not text and not image_urls:
                        pass
                    else:
                        label = "Bot自己发的" if is_self else f"{author_name}发的"
                        ctx = f"【当前动态（{label}）】\n内容：{text or '（无文字）'}"
                        if pub_time:
                            ctx += f"\n发布时间：{pub_time}"

                        if image_urls:
                            logger.info(f"[BiliBot] 🖼️ 动态含 {len(image_urls)} 张图片，识别中...")
                            image_desc = await self._recognize_images(image_urls[:4])
                            if image_desc:
                                ctx += f"\n图片内容：{image_desc}"
                            else:
                                ctx += f"\n（动态含{len(image_urls)}张图片，识别失败）"

                        return ctx
                else:
                    logger.debug(f"[BiliBot] 动态详情API返回非0: code={d.get('code')} msg={d.get('message', '')}")
        except Exception as e:
            logger.debug(f"[BiliBot] 动态API获取失败: {e}")
        dynamic_mems = [m for m in self._memory if m.get("memory_type") == "dynamic"]
        if dynamic_mems:
            latest = dynamic_mems[-1]
            return f"【最近发布的动态】\n{latest.get('text', '')}"
        return ""

    async def _get_draw_context(self, doc_id):
        """通过相簿API获取图文动态内容（comment_type=11时oid是doc_id）"""
        # 方案1: 相簿详情API
        for api_url in [
            f"https://api.bilibili.com/x/dynamic/feed/draw/doc_detail?doc_id={doc_id}",
            f"https://api.vc.bilibili.com/link_draw/v1/doc/detail?doc_id={doc_id}",
        ]:
            try:
                d, _ = await self._http_get(api_url)
                if isinstance(d, dict) and d.get("code") == 0:
                    item = d.get("data", {}).get("item", {})
                    description = item.get("description", "")
                    pictures = item.get("pictures", [])
                    image_urls = [p.get("img_src", "") for p in pictures if p.get("img_src")]

                    user = d.get("data", {}).get("user", {})
                    author_name = user.get("name", user.get("head_url", ""))
                    author_mid = str(user.get("uid", user.get("mid", "")))
                    bot_mid = self.config.get("DEDE_USER_ID", "")
                    is_self = author_mid == bot_mid

                    label = "Bot自己发的" if is_self else f"{author_name}发的"
                    ctx = f"【当前动态（{label}）】\n内容：{description or '（无文字）'}"

                    if image_urls:
                        logger.info(f"[BiliBot] 🖼️ 图文动态含 {len(image_urls)} 张图片，识别中...")
                        image_desc = await self._recognize_images(image_urls[:4])
                        if image_desc:
                            ctx += f"\n图片内容：{image_desc}"
                            mem_key = f"dynamic_img:{doc_id}"
                            has_mem = any(m.get("thread_id") == mem_key for m in self._memory)
                            if not has_mem:
                                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                                mem_text = f"[{now_str}] 动态图片记忆：{author_name}的动态「{description[:60]}」图片内容：{image_desc[:200]}"
                                await self._save_self_memory_record(
                                    mem_key, mem_text, memory_type="dynamic",
                                    extra={"dynamic_id": str(doc_id), "author_mid": author_mid},
                                )
                                logger.info(f"[BiliBot] 📸 存入动态图片记忆")
                        else:
                            ctx += f"\n（动态含{len(image_urls)}张图片，识别失败）"

                    logger.info(f"[BiliBot] ✅ 相簿API成功获取动态内容: doc_id={doc_id}")
                    return ctx
            except Exception as e:
                logger.debug(f"[BiliBot] 相簿API({api_url})获取失败: {e}")

        # 方案2: 从Bot自己的动态列表中查找匹配的dynamic_id
        try:
            bot_mid = self.config.get("DEDE_USER_ID", "")
            if bot_mid:
                d, _ = await self._http_get(
                    "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                    params={
                        "host_mid": bot_mid, "offset": "",
                        "timezone_offset": -480,
                        "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
                    },
                )
                if d.get("code") == 0:
                    for item in d.get("data", {}).get("items", []):
                        basic = item.get("basic", {})
                        # rid_str 对应 doc_id
                        if basic.get("rid_str") == str(doc_id) or basic.get("comment_id_str") == str(doc_id):
                            dynamic_id = item.get("id_str", "")
                            if dynamic_id:
                                logger.info(f"[BiliBot] 🔄 通过空间动态列表找到 dynamic_id={dynamic_id} (doc_id={doc_id})")
                                # 用 dynamic_id 调详情API
                                return await self._get_dynamic_context_by_id(dynamic_id)
        except Exception as e:
            logger.debug(f"[BiliBot] 空间动态列表查找失败: {e}")

        return ""

    async def _get_dynamic_context_by_id(self, dynamic_id):
        """用 dynamic_id 调动态详情API"""
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail", params={
                "id": dynamic_id, "timezone_offset": -480,
                "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
            })
            if not isinstance(d, dict):
                logger.debug(f"[BiliBot] 动态详情API返回非dict: {type(d)} (id={dynamic_id})")
                return ""
            if d.get("code") == 0:
                item = (d.get("data") or {}).get("item") or {}
                if not item:
                    logger.debug(f"[BiliBot] 动态详情API返回code=0但item为空 (id={dynamic_id})")
                    return ""
                modules = item.get("modules") or {}
                desc = (modules.get("module_dynamic") or {}).get("desc") or {}
                text = desc.get("text", "")
                author = modules.get("module_author") or {}
                author_name = author.get("name", "")
                pub_time = author.get("pub_time", "")
                bot_mid = self.config.get("DEDE_USER_ID", "")
                is_self = str(author.get("mid", "")) == bot_mid

                major = (modules.get("module_dynamic") or {}).get("major") or {}
                major_type = major.get("type", "")

                # opus格式（features=itemOpusStyle时）
                if major_type == "MAJOR_TYPE_OPUS" or "opus" in major:
                    opus = major.get("opus") or {}
                    # opus格式的文字在 summary.text 或 title
                    opus_summary = (opus.get("summary") or {}).get("text", "")
                    opus_title = opus.get("title", "")
                    if opus_summary:
                        text = opus_summary
                    elif opus_title:
                        text = opus_title
                    image_urls = [p.get("url", "") for p in (opus.get("pics") or []) if p.get("url")]
                # 传统draw格式
                elif "draw" in major:
                    draw = major.get("draw") or {}
                    image_urls = [img.get("src", "") for img in (draw.get("items") or []) if img.get("src")]
                else:
                    image_urls = []
                    if major:
                        logger.debug(f"[BiliBot] 未知major类型 (id={dynamic_id}): type={major_type} keys={list(major.keys())}")

                label = "Bot自己发的" if is_self else f"{author_name}发的"
                ctx = f"【当前动态（{label}）】\n内容：{text or '（无文字）'}"
                if pub_time:
                    ctx += f"\n发布时间：{pub_time}"

                if image_urls:
                    logger.info(f"[BiliBot] 🖼️ 动态含 {len(image_urls)} 张图片，识别中...")
                    image_desc = await self._recognize_images(image_urls[:4])
                    if image_desc:
                        ctx += f"\n图片内容：{image_desc}"
                    else:
                        ctx += f"\n（动态含{len(image_urls)}张图片，识别失败）"

                logger.info(f"[BiliBot] ✅ 动态详情获取成功 (id={dynamic_id}): {text[:50] if text else '无文字'}")
                return ctx
            else:
                logger.debug(f"[BiliBot] 动态详情API返回非0 (id={dynamic_id}): code={d.get('code')} msg={d.get('message', '')} data_keys={list((d.get('data') or {}).keys())}")
        except Exception as e:
            logger.debug(f"[BiliBot] 动态详情API获取失败(id={dynamic_id}): {e}")
        return ""
