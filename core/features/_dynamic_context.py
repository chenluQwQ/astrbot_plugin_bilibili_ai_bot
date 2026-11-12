"""回复动态下的评论时拼「当前动态」：调动态详情或相簿接口、识图，都失败就用最近一条动态记忆兜底。

三个接口的请求和解析缠在同一个 try 里，拆开会改出错时走哪条兜底，所以原样放在一起。
"""
from datetime import datetime

from astrbot.api import logger
from ..infra.host_view import HostView


class DynamicContext(HostView):
    HOST_API = frozenset({
        "bili_http", "memories", "memory_writer", "settings", "video_watch", "vision",
    })

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
                d, _ = await self.bili_http._http_get("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail", params={
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
                    bot_mid = self.settings.get("DEDE_USER_ID")
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
                        ctx = f"【当前动态（{label}）】\n内容：{self.video_watch._clip_media_text(text, 1200) or '（无文字）'}"
                        if pub_time:
                            ctx += f"\n发布时间：{pub_time}"

                        if image_urls:
                            logger.info(f"[BiliBot] 🖼️ 动态含 {len(image_urls)} 张图片，识别中...")
                            image_desc = await self.vision._recognize_images(image_urls[:4])
                            if image_desc:
                                ctx += f"\n图片内容：{self.video_watch._clip_media_text(image_desc, 500)}"
                            else:
                                ctx += f"\n（动态含{len(image_urls)}张图片，识别失败）"

                        return ctx
                else:
                    logger.debug(f"[BiliBot] 动态详情API返回非0: code={d.get('code')} msg={d.get('message', '')}")
        except Exception as e:
            logger.debug(f"[BiliBot] 动态API获取失败: {e}")
        dynamic_mems = await self.memories.find(where=lambda m: m.get("memory_type") == "dynamic", newest_first=True, limit=1)
        if dynamic_mems:
            latest = dynamic_mems[0]
            return f"【最近发布的动态】\n{self.video_watch._clip_media_text(latest.get('text', ''), 1200)}"
        return ""

    async def _get_draw_context(self, doc_id):
        """通过相簿API获取图文动态内容（comment_type=11时oid是doc_id）"""
        # 方案1: 相簿详情API
        for api_url in [
            f"https://api.bilibili.com/x/dynamic/feed/draw/doc_detail?doc_id={doc_id}",
            f"https://api.vc.bilibili.com/link_draw/v1/doc/detail?doc_id={doc_id}",
        ]:
            try:
                d, _ = await self.bili_http._http_get(api_url)
                if isinstance(d, dict) and d.get("code") == 0:
                    item = d.get("data", {}).get("item", {})
                    description = item.get("description", "")
                    pictures = item.get("pictures", [])
                    image_urls = [p.get("img_src", "") for p in pictures if p.get("img_src")]

                    user = d.get("data", {}).get("user", {})
                    author_name = user.get("name", user.get("head_url", ""))
                    author_mid = str(user.get("uid", user.get("mid", "")))
                    bot_mid = self.settings.get("DEDE_USER_ID")
                    is_self = author_mid == bot_mid

                    label = "Bot自己发的" if is_self else f"{author_name}发的"
                    ctx = f"【当前动态（{label}）】\n内容：{self.video_watch._clip_media_text(description, 1200) or '（无文字）'}"

                    if image_urls:
                        logger.info(f"[BiliBot] 🖼️ 图文动态含 {len(image_urls)} 张图片，识别中...")
                        image_desc = await self.vision._recognize_images(image_urls[:4])
                        if image_desc:
                            ctx += f"\n图片内容：{self.video_watch._clip_media_text(image_desc, 500)}"
                            mem_key = f"dynamic_img:{doc_id}"
                            has_mem = await self.memories.exists(thread_id=mem_key)
                            if not has_mem:
                                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                                mem_text = f"[{now_str}] 动态图片记忆：{author_name}的动态「{description[:60]}」图片内容：{image_desc[:200]}"
                                await self.memory_writer._save_self_memory_record(
                                    mem_key, mem_text, memory_type="dynamic",
                                    extra={"dynamic_id": str(doc_id), "author_mid": author_mid,
                                           **await self.memory_writer._score_self_memory("动态", mem_text)},
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
            bot_mid = self.settings.get("DEDE_USER_ID")
            if bot_mid:
                d, _ = await self.bili_http._http_get(
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
            d, _ = await self.bili_http._http_get("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail", params={
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
                bot_mid = self.settings.get("DEDE_USER_ID")
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
                ctx = f"【当前动态（{label}）】\n内容：{self.video_watch._clip_media_text(text, 1200) or '（无文字）'}"
                if pub_time:
                    ctx += f"\n发布时间：{pub_time}"

                if image_urls:
                    logger.info(f"[BiliBot] 🖼️ 动态含 {len(image_urls)} 张图片，识别中...")
                    image_desc = await self.vision._recognize_images(image_urls[:4])
                    if image_desc:
                        ctx += f"\n图片内容：{self.video_watch._clip_media_text(image_desc, 500)}"
                    else:
                        ctx += f"\n（动态含{len(image_urls)}张图片，识别失败）"

                logger.info(f"[BiliBot] ✅ 动态详情获取成功 (id={dynamic_id}): {text[:50] if text else '无文字'}")
                return ctx
            else:
                logger.debug(f"[BiliBot] 动态详情API返回非0 (id={dynamic_id}): code={d.get('code')} msg={d.get('message', '')} data_keys={list((d.get('data') or {}).keys())}")
        except Exception as e:
            logger.debug(f"[BiliBot] 动态详情API获取失败(id={dynamic_id}): {e}")
        return ""
