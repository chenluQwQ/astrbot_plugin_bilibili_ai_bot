"""周总结：回顾一周的B站生活，生成总结并通过 QQ 私信 / B站动态发送。

数据源（近7天）：
  - watch_log.json          主动看过的视频（评分/心情/感想）
  - bangumi_watch_log.json  看过的番剧
  - dynamic_log.json        发过的动态
  - proactive_log.json      主动评论
  - memory.json             chat 类型记忆（互动用户统计）

触发：
  - 自动：每周 WEEKLY_SUMMARY_DAY（0=周一...6=周日）的睡眠时段，
          日终清算之后由主循环调用 _maybe_weekly_summary()
  - 手动：/bili周总结 命令
"""
import json
import os
import re
import time
from datetime import datetime, timedelta
from collections import Counter
from astrbot.api import logger
from .config import (
    WATCH_LOG_FILE, BANGUMI_WATCH_LOG_FILE, DYNAMIC_LOG_FILE,
    PROACTIVE_LOG_FILE, WEEKLY_SUMMARY_FILE, TEMP_IMAGE_DIR,
)


class WeeklySummaryMixin:
    """周总结生成与投递。"""

    # ── 数据收集 ──

    def _collect_weekly_data(self, days=7):
        """收集近 N 天的活动数据，返回结构化 dict。"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")

        def _recent(entries):
            return [e for e in entries if isinstance(e, dict) and e.get("time", "") >= cutoff]

        videos = _recent(self._load_json(WATCH_LOG_FILE, []))
        bangumi = _recent(self._load_json(BANGUMI_WATCH_LOG_FILE, []))
        dynamics = _recent(self._load_json(DYNAMIC_LOG_FILE, []))
        proactive_comments = _recent(self._load_json(PROACTIVE_LOG_FILE, []))

        # 聊天互动：chat 类型记忆，统计互动条数和活跃用户
        chats = [
            m for m in getattr(self, "_memory", [])
            if isinstance(m, dict)
            and m.get("memory_type") == "chat"
            and m.get("time", "") >= cutoff
            and m.get("user_id") not in (None, "", "self")
        ]
        user_counter = Counter(m.get("user_id", "") for m in chats)

        return {
            "videos": videos,
            "bangumi": bangumi,
            "dynamics": dynamics,
            "proactive_comments": proactive_comments,
            "chat_count": len(chats),
            "active_users": user_counter.most_common(5),
        }

    def _format_weekly_data(self, data):
        """把收集到的数据格式化为给 LLM 的文本。"""
        lines = []

        videos = data["videos"]
        if videos:
            lines.append(f"【看过的视频】共 {len(videos)} 个：")
            # 高分和低分的更值得提
            shown = sorted(videos, key=lambda v: v.get("score", 0), reverse=True)[:10]
            for v in shown:
                lines.append(
                    f"- 《{v.get('title', '')[:30]}》(UP:{v.get('up_name', '')}) "
                    f"评分{v.get('score', '?')}/10 心情:{v.get('mood', '')} "
                    f"感想:{(v.get('review', '') or '')[:40]}"
                )
        else:
            lines.append("【看过的视频】这周没看视频")

        bangumi = data["bangumi"]
        if bangumi:
            seasons = Counter(b.get("title", "") for b in bangumi)
            lines.append(f"【追的番】共 {len(bangumi)} 集：")
            for title, cnt in seasons.most_common(5):
                eps = [b for b in bangumi if b.get("title") == title]
                avg = sum(b.get("score", 0) for b in eps) / max(len(eps), 1)
                lines.append(f"- 《{title[:25]}》看了{cnt}集，平均评分{avg:.0f}/10")

        dynamics = data["dynamics"]
        if dynamics:
            lines.append(f"【发过的动态】共 {len(dynamics)} 条：")
            for d in dynamics[-5:]:
                lines.append(f"- {(d.get('text', '') or '')[:40]}")

        pc = data["proactive_comments"]
        if pc:
            lines.append(f"【主动发的评论】共 {len(pc)} 条")

        if data["chat_count"]:
            lines.append(f"【评论区互动】共 {data['chat_count']} 次对话")
            if data["active_users"]:
                top = "、".join(f"{uid}({cnt}次)" for uid, cnt in data["active_users"][:3])
                lines.append(f"互动最多的用户UID：{top}")
        else:
            lines.append("【评论区互动】这周没什么人来聊天")

        return "\n".join(lines)

    # ── 生成 ──

    async def _generate_weekly_summary(self):
        """生成周总结文本，失败返回 None。"""
        data = self._collect_weekly_data()
        if not (data["videos"] or data["bangumi"] or data["dynamics"] or data["chat_count"]):
            logger.info("[BiliBot] 📅 这周没有任何活动记录，跳过周总结")
            return None

        data_text = self._format_weekly_data(data)
        week_start = (datetime.now() - timedelta(days=7)).strftime("%m.%d")
        week_end = datetime.now().strftime("%m.%d")

        # 预算统计数据给模板用
        v_count = len(data["videos"])
        v_top = max((v.get("score", 0) for v in data["videos"]), default=0) if data["videos"] else 0
        b_count = len(data["bangumi"])
        d_count = len(data["dynamics"])
        chat_count = data["chat_count"]

        stats_line = f"视频{v_count}个"
        if b_count:
            stats_line += f" · 番剧{b_count}集"
        if d_count:
            stats_line += f" · 动态{d_count}条"
        if chat_count:
            stats_line += f" · 互动{chat_count}次"

        prompt = f"""现在是周末，你想回顾这一周在B站的生活，写一份有格式感的周报。

这周的活动记录：
{data_text}

请严格按照以下格式输出（每个板块的内容用你自己的语气写，有感受有情绪，不是流水账）：

📅 周报 | {week_start} ~ {week_end}
━━━━━━━━━━━━
{stats_line}

📺 视频
（挑2-3个印象最深的视频聊，说说看完什么感觉、哪里戳到你了，不用每个都提）

{"🎬 追番" + chr(10) + "（追了什么番、追到第几集、感受如何）" + chr(10) + chr(10) if b_count else ""}{"💬 评论区" + chr(10) + "（和谁聊得多、有没有印象深的互动、评论区氛围怎么样）" + chr(10) + chr(10) if chat_count else ""}{"📢 动态" + chr(10) + "（发了什么动态、当时在想什么）" + chr(10) + chr(10) if d_count else ""}✍️ 碎碎念
（用1-2句话总结这周的心情/状态，随意收尾）

要求：
- 每个板块标题行保持原样（📺 视频、🎬 追番 等），内容紧跟其后
- 内容用你自己的语气，像跟亲近的人聊天
- 总字数200-350字（不含格式符号）
- 直接输出，不要加额外的标题或前缀"""

        custom_inst = self.config.get("CUSTOM_WEEKLY_INSTRUCTION", "")
        if custom_inst:
            prompt += f"\n【补充提示词】{custom_inst}"

        summary = await self._llm_call(
            prompt, system_prompt=await self._get_system_prompt(), max_tokens=800
        )
        return (summary or "").strip() or None

    # ── 图片渲染 ──

    def _find_weekly_font(self, bold=False):
        """寻找可渲染中文的字体，找不到则退回 Pillow 默认字体。"""
        try:
            from PIL import ImageFont
        except Exception:
            return None
        candidates = [
            r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\simsun.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return path
        return None

    def _load_weekly_font(self, size, bold=False):
        from PIL import ImageFont
        font_path = self._find_weekly_font(bold=bold)
        if font_path:
            return ImageFont.truetype(font_path, size=size)
        return ImageFont.load_default()

    @staticmethod
    def _strip_weekly_emoji(text):
        return re.sub(r"^[\s📅📺🎬💬📢✍️📝✨⭐🌙·|]+", "", text or "").strip()

    # 中文字体没有彩色 emoji 字形，画出来是豆腐块，渲染前全部去掉
    _WEEKLY_EMOJI_RE = re.compile(
        "["
        "\U0001F000-\U0001FAFF"   # 各类 emoji / 符号 / 补充区
        "\U00002190-\U000021FF"   # 箭头
        "\U00002460-\U000024FF"   # 带圈数字
        "\U00002600-\U000027BF"   # 杂项符号、装饰符号
        "\U00002B00-\U00002BFF"   # 杂项符号与箭头（⭐ 等）
        "\U0001F1E6-\U0001F1FF"   # 区域指示符
        "\ufe0e\ufe0f\u200d\u20e3"  # 变体选择符 / ZWJ / 组合键帽
        "]+"
    )

    @classmethod
    def _clean_weekly_render_text(cls, text):
        """去掉字体画不出来的 emoji 和 LLM 夹带的 markdown 记号。"""
        s = cls._WEEKLY_EMOJI_RE.sub("", text or "")
        s = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", s)  # **加粗** → 加粗
        s = s.replace("**", "").replace("`", "")
        return re.sub(r"[ \t]{2,}", " ", s).strip()

    @staticmethod
    def _text_width(draw, text, font):
        if not text:
            return 0
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0]

    def _wrap_weekly_text(self, draw, text, font, max_width):
        lines = []
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line:
                lines.append("")
                continue
            buf = ""
            for ch in line:
                trial = buf + ch
                if buf and self._text_width(draw, trial, font) > max_width:
                    lines.append(buf)
                    buf = ch
                else:
                    buf = trial
            if buf:
                lines.append(buf)
        return lines

    # LLM 不带 emoji 前缀时，靠这些标题词兜底识别板块
    _WEEKLY_KNOWN_TITLES = ("视频", "追番", "评论区", "动态", "碎碎念", "本周摘要", "总结")

    def _parse_weekly_sections(self, summary):
        sections = []
        current_title = "本周摘要"
        current_lines = []
        stats_line = ""
        for raw in (summary or "").splitlines():
            line = raw.strip()
            if not line or set(line) <= {"━", "-", "—", "=", "*"}:
                continue
            clean = self._strip_weekly_emoji(line)
            # 标题行：📅 开头，或 markdown 标题/纯文字形式的「周报 xx.xx ~ xx.xx」
            md = re.match(r"^#{1,4}\s*(.+)$", clean)
            md_clean = self._strip_weekly_emoji(md.group(1)) if md else ""
            if line.startswith("📅") or re.match(r"^周报\b|^周报[\s|｜]", md_clean or clean):
                current_title = (md_clean or clean) or "周报"
                continue
            # 板块标题：emoji 前缀 / markdown 标题 / 单独一行的已知标题词
            bare = (md_clean or clean).rstrip("：:").replace("**", "").strip()
            is_header = (
                any(line.startswith(p) for p in ("📺", "🎬", "💬", "📢", "✍"))
                or (md and bare)
                or bare in self._WEEKLY_KNOWN_TITLES
            )
            if is_header:
                if current_lines:
                    sections.append((current_title, "\n".join(current_lines).strip()))
                current_title = bare or clean or line
                current_lines = []
                continue
            if not stats_line and ("视频" in line or "番剧" in line or "互动" in line or "动态" in line) and "·" in line:
                stats_line = line
                continue
            current_lines.append(line)
        if current_lines:
            sections.append((current_title, "\n".join(current_lines).strip()))
        return stats_line, sections[:6]

    @staticmethod
    def _rounded_rect(draw, xy, radius, fill, outline=None, width=1):
        draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)

    def _render_weekly_summary_image(self, summary):
        """把周总结渲染成固定模板 PNG。失败时返回 None，不影响文本投递。"""
        try:
            from PIL import Image, ImageDraw, ImageFilter
        except Exception as e:
            logger.warning(f"[BiliBot] 周总结图片渲染不可用（缺少Pillow）: {e}")
            return None
        try:
            os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
            width = 1200
            margin = 72
            line_h = 38
            max_body_lines = 14

            title_font = self._load_weekly_font(56, bold=True)
            sub_font = self._load_weekly_font(26)
            stat_font = self._load_weekly_font(28, bold=True)
            card_title_font = self._load_weekly_font(32, bold=True)
            body_font = self._load_weekly_font(28)
            small_font = self._load_weekly_font(22)

            week_start = (datetime.now() - timedelta(days=7)).strftime("%m.%d")
            week_end = datetime.now().strftime("%m.%d")
            stats_line, sections = self._parse_weekly_sections(summary)
            stats_line = self._clean_weekly_render_text(stats_line) or "这一周的B站生活记录"

            # 先量后画：用临时画布把每张卡片的行数算出来，画布高度按内容伸缩
            meas = ImageDraw.Draw(Image.new("RGB", (width, 8)))
            max_text_w = width - margin * 2 - 70
            cards = []
            for title, body in sections:
                title = self._clean_weekly_render_text(title) or "小记"
                body = self._clean_weekly_render_text(body) or "这块内容有点安静。"
                wrapped = self._wrap_weekly_text(meas, body, body_font, max_text_w)
                if len(wrapped) > max_body_lines:
                    wrapped = wrapped[:max_body_lines]
                    wrapped[-1] = wrapped[-1][:-1] + "…"
                card_h = 86 + max(1, len(wrapped)) * line_h + 32
                cards.append((title, wrapped, card_h))

            header_h = 342          # 大标题 + 日期 + 统计条
            footer_h = 116
            content_h = sum(h for _, _, h in cards) + 24 * max(len(cards) - 1, 0)
            height = max(1280, header_h + content_h + footer_h + 40)
            height = min(height, 4000)

            img = Image.new("RGB", (width, height), "#f7f1e8")
            draw = ImageDraw.Draw(img)
            for y in range(height):
                t = y / max(height - 1, 1)
                r = int(247 * (1 - t) + 228 * t)
                g = int(241 * (1 - t) + 238 * t)
                b = int(232 * (1 - t) + 230 * t)
                draw.line((0, y, width, y), fill=(r, g, b))

            # 柔和色块，纯代码渲染的模板背景（底部色块跟随画布高度）
            overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            od.ellipse((-180, -120, 520, 460), fill=(246, 169, 122, 85))
            od.ellipse((760, 80, 1420, 720), fill=(94, 145, 132, 75))
            od.ellipse((650, height - 560, 1320, height + 140), fill=(76, 98, 142, 55))
            overlay = overlay.filter(ImageFilter.GaussianBlur(36))
            img = Image.alpha_composite(img.convert("RGBA"), overlay)
            draw = ImageDraw.Draw(img)

            y = 74
            draw.text((margin, y), "BiliBot 周报", fill="#24312f", font=title_font)
            y += 72
            draw.text((margin + 2, y), f"{week_start} - {week_end} · 自动生成", fill="#6f746f", font=sub_font)

            badge_text = "WEEKLY"
            badge_w = self._text_width(draw, badge_text, small_font) + 42
            self._rounded_rect(draw, (width - margin - badge_w, 86, width - margin, 132), 23, fill=(36, 49, 47, 230))
            draw.text((width - margin - badge_w + 21, 98), badge_text, fill="#f8f0df", font=small_font)

            y += 78
            self._rounded_rect(draw, (margin, y, width - margin, y + 86), 30, fill=(255, 252, 244, 220), outline=(229, 215, 192, 220), width=2)
            draw.text((margin + 34, y + 26), stats_line, fill="#4a4f49", font=stat_font)
            y += 118

            palette = ["#d86f45", "#477c73", "#526c9d", "#a56a43", "#6f6f48", "#8b627a"]
            content_bottom = height - 116
            for idx, (title, wrapped, card_h) in enumerate(cards):
                card_x1, card_x2 = margin, width - margin
                if y + card_h > content_bottom:
                    break
                self._rounded_rect(draw, (card_x1, y, card_x2, y + card_h), 34, fill=(255, 253, 248, 232), outline=(229, 218, 201, 210), width=2)
                accent = palette[idx % len(palette)]
                self._rounded_rect(draw, (card_x1 + 24, y + 28, card_x1 + 38, y + card_h - 28), 7, fill=accent)
                draw.text((card_x1 + 58, y + 28), title, fill="#283330", font=card_title_font)
                ty = y + 78
                for line in wrapped:
                    draw.text((card_x1 + 58, ty), line, fill="#4b4d49", font=body_font)
                    ty += line_h
                y += card_h + 24

            footer = "Generated by astrbot_plugin_bilibili_ai_bot"
            draw.text((margin, height - 62), footer, fill="#8b8d87", font=small_font)
            path = os.path.join(TEMP_IMAGE_DIR, f"weekly_summary_{int(time.time())}.png")
            img.convert("RGB").save(path, "PNG", optimize=True)
            logger.info(f"[BiliBot] 周总结图片已渲染: {path}")
            return path
        except Exception as e:
            logger.warning(f"[BiliBot] 周总结图片渲染失败: {e}", exc_info=True)
            return None

    def _append_image_to_chain(self, chain, image_path):
        from astrbot.api.message_components import Image as MsgImage
        img = MsgImage.fromFileSystem(image_path)
        if hasattr(chain, "chain"):
            chain.chain.append(img)
            return chain
        if hasattr(chain, "append"):
            chain.append(img)
            return chain
        return None

    # ── 投递 ──

    async def _deliver_weekly_summary(self, summary, image_path=None):
        """按配置投递周总结，返回投递结果描述列表。"""
        mode = str(self.config.get("WEEKLY_SUMMARY_MODE", "qq")).lower().strip()
        results = []

        if mode in ("qq", "both"):
            umo = (self.config.get("WEEKLY_SUMMARY_QQ_UMO", "") or "").strip()
            if not umo:
                umo = (self.config.get("ABUSE_ALERT_QQ_UMO", "") or "").strip()
            if umo:
                try:
                    from astrbot.api.event import MessageChain
                    chain = MessageChain().message("📅 本周B站生活周报")
                    if image_path:
                        chain = self._append_image_to_chain(chain, image_path) or MessageChain().message(summary)
                    else:
                        chain = MessageChain().message(summary)
                    await self.context.send_message(umo, chain)
                    results.append("QQ私信图片" if image_path else "QQ私信")
                    logger.info("[BiliBot] 📅 周总结已通过QQ私信发送")
                except Exception as e:
                    logger.warning(f"[BiliBot] 周总结QQ图片发送失败，尝试退回文本: {e}")
                    try:
                        from astrbot.api.event import MessageChain
                        await self.context.send_message(umo, MessageChain().message(summary))
                        results.append("QQ私信文本")
                    except Exception as e2:
                        logger.warning(f"[BiliBot] 周总结QQ文本发送失败: {e2}")
            else:
                logger.warning("[BiliBot] 周总结模式包含qq但未配置UMO（周总结/恶意告警的UMO都为空）")

        if mode in ("dynamic", "both"):
            try:
                success = False
                has_image = False
                dynamic_text = summary
                if image_path:
                    img_info = await self._upload_image_to_bilibili(image_path)
                    if img_info:
                        dynamic_text = "📅 这周的B站生活周报来啦，整理成图片存档一下。"
                        success = await self._post_dynamic_with_image(dynamic_text, img_info)
                        has_image = success
                if not success:
                    success = await self._post_dynamic_text(summary)
                if success:
                    results.append("B站动态图片" if has_image else "B站动态")
                    log = self._load_json(DYNAMIC_LOG_FILE, [])
                    log.append({
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "text": dynamic_text, "has_image": has_image, "weekly": True,
                        "image_path": image_path if has_image else "",
                    })
                    self._save_json(DYNAMIC_LOG_FILE, log[-100:])
                    logger.info("[BiliBot] 📅 周总结已发布为B站动态")
            except Exception as e:
                logger.warning(f"[BiliBot] 周总结动态发布失败: {e}")

        return results

    # ── 调度 ──

    def _weekly_summary_done_this_week(self):
        """检查本ISO周是否已生成过周总结。"""
        records = self._load_json(WEEKLY_SUMMARY_FILE, [])
        if not records:
            return False
        this_week = datetime.now().strftime("%G-W%V")
        return any(r.get("week") == this_week for r in records if isinstance(r, dict))

    def _save_weekly_summary_record(self, summary, delivered, image_path=""):
        records = self._load_json(WEEKLY_SUMMARY_FILE, [])
        records.append({
            "week": datetime.now().strftime("%G-W%V"),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": summary,
            "delivered": delivered,
            "image_path": image_path or "",
        })
        self._save_json(WEEKLY_SUMMARY_FILE, records[-20:])

    async def _maybe_weekly_summary(self):
        """主循环睡眠时段调用：到了周总结日且本周未生成则执行。"""
        if not self.config.get("ENABLE_WEEKLY_SUMMARY", False):
            return
        try:
            target_day = int(self.config.get("WEEKLY_SUMMARY_DAY", 6))
        except (ValueError, TypeError):
            target_day = 6
        if datetime.now().weekday() != target_day % 7:
            return
        if self._weekly_summary_done_this_week():
            return
        await self.run_weekly_summary()

    async def run_weekly_summary(self):
        """生成并投递周总结（自动调度和手动命令共用）。返回 (summary, delivered)。"""
        logger.info("[BiliBot] 📅 开始生成周总结...")
        try:
            summary = await self._generate_weekly_summary()
        except Exception as e:
            logger.error(f"[BiliBot] 周总结生成异常: {e}")
            return None, [], None
        if not summary:
            # 没有活动也记录一下，避免同一周反复尝试
            self._save_weekly_summary_record("（本周无活动，未生成）", [], "")
            return None, [], None
        image_path = self._render_weekly_summary_image(summary) if self.config.get("WEEKLY_SUMMARY_RENDER_IMAGE", True) else None
        delivered = await self._deliver_weekly_summary(summary, image_path=image_path)
        self._save_weekly_summary_record(summary, delivered, image_path or "")
        logger.info(f"[BiliBot] 📅 周总结完成，投递：{delivered or ['仅存档']}")
        return summary, delivered, image_path
