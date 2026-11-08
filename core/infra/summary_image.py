"""把总结文字画成图片：找字体、排版、渲染。不管内容也不管发到哪。"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta

from astrbot.api import logger

from ..config import TEMP_IMAGE_DIR
from .host_view import HostView


class SummaryImage(HostView):
    HOST_API = frozenset({"config"})

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

    _WEEKLY_KNOWN_TITLES = (
        "本周切片", "意外与反差", "有回声的瞬间", "兴趣坐标", "留给下周",
        "视频", "追番", "直播", "评论区", "动态", "碎碎念", "本周摘要", "总结",
    )

    @staticmethod
    def _weekly_font_has_chinese(path):
        """验证字体是否真的包含中文字形，避免把缺字方框误当作可用字体。"""
        try:
            from PIL import ImageFont
            font = ImageFont.truetype(path, size=28)
            glyphs = []
            for char in "中文周报视频":
                mask = font.getmask(char)
                if not mask.getbbox():
                    return False
                glyphs.append((mask.size, bytes(mask)))
            # 缺字时多个中文字符通常都会映射到同一枚 tofu 方框。
            return len(set(glyphs)) >= 3
        except Exception:
            return False

    def _find_weekly_font(self, bold=False):
        """寻找并验证可渲染中文的字体；没有字体时返回 None。"""
        config = getattr(self, "config", {}) or {}
        configured = str(config.get("SUMMARY_CJK_FONT_PATH", "") or "").strip()
        if configured:
            configured = os.path.abspath(os.path.expanduser(os.path.expandvars(configured)))
        cache_key = (configured, bool(bold))
        cache = getattr(self, "_weekly_font_path_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._weekly_font_path_cache = cache
        if cache_key in cache:
            return cache[cache_key]

        candidates = []

        def _add(path):
            if path and path not in candidates:
                candidates.append(path)

        _add(configured)
        if bold:
            _add(r"C:\Windows\Fonts\msyhbd.ttc")
            _add("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")
            _add("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf")
            _add("/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Bold.otf")
        _add(r"C:\Windows\Fonts\msyh.ttc")
        _add(r"C:\Windows\Fonts\simhei.ttf")
        _add(r"C:\Windows\Fonts\simsun.ttc")
        _add("/System/Library/Fonts/PingFang.ttc")
        _add("/System/Library/Fonts/STHeiti Medium.ttc")
        _add("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
        _add("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf")
        _add("/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf")
        _add("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc")
        _add("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc")
        _add("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")
        _add("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")
        _add("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf")
        # 不同 Linux 发行版的字体目录并不统一，最后做一次有边界的名称查找。
        font_roots = (
            "/usr/share/fonts", "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"), os.path.expanduser("~/.local/share/fonts"),
        )
        name_markers = (
            "notosanscjk", "sourcehansans", "wqy", "droidsansfallback",
            "sarasa", "unifont",
        )
        discovered = []
        for root in font_roots:
            if not os.path.isdir(root):
                continue
            try:
                for directory, _dirs, files in os.walk(root):
                    for filename in files:
                        lower = filename.lower()
                        if lower.endswith((".ttf", ".otf", ".ttc")) and any(
                            marker in lower for marker in name_markers
                        ):
                            discovered.append(os.path.join(directory, filename))
            except OSError:
                continue
        for path in sorted(discovered, key=lambda item: ("bold" not in item.lower(), item.lower())):
            _add(path)

        # 插件自带的 OFL 字体放在所有系统候选之后，只作为最终兜底。
        bundled_font = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "assets", "fonts", "NotoSansSC-VF.otf",
        )
        _add(bundled_font)

        for path in candidates:
            if os.path.isfile(path) and self._weekly_font_has_chinese(path):
                cache[cache_key] = path
                return path
        cache[cache_key] = None
        return None

    def _load_weekly_font(self, size, bold=False):
        from PIL import ImageFont
        font_path = self._find_weekly_font(bold=bold)
        if font_path:
            font = ImageFont.truetype(font_path, size=size)
            # 内置字体是可变字体，默认轴是 Thin；显式切到 Regular/Bold 才与
            # 原模板的层级一致。静态字体不支持该方法时直接忽略。
            try:
                font.set_variation_by_name("Bold" if bold else "Regular")
            except Exception:
                pass
            return font
        raise RuntimeError(
            "未找到可渲染中文的字体；Linux 请安装 Noto Sans CJK，"
            "或在 SUMMARY_CJK_FONT_PATH 中填写中文字体文件路径"
        )

    @staticmethod
    def _strip_weekly_emoji(text):
        return re.sub(r"^[\s📅📺🎬🎙🎤💬📢✍️📝✨⭐🌙📌🎢🧭🔀·|]+", "", text or "").strip()

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

    def _truncate_weekly_line(self, draw, text, font, max_width):
        """把单行文字安全截到指定像素宽度，避免统计条溢出卡片。"""
        value = str(text or "").strip()
        if self._text_width(draw, value, font) <= max_width:
            return value
        suffix = "…"
        while value and self._text_width(draw, value + suffix, font) > max_width:
            value = value[:-1]
        return value.rstrip() + suffix

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
                any(line.startswith(p) for p in ("📌", "🎢", "🧭", "🔀", "📺", "🎬", "🎙", "🎤", "💬", "📢", "✍"))
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

    def _render_weekly_summary_image(self, summary, report_kind="weekly"):
        """把日/周总结渲染成固定模板 PNG。失败时返回 None，不影响文本投递。"""
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
            # 最多六个栏目时，8行/栏可保证全部卡片落在单张图内；异常长内容在栏内明确省略。
            max_body_lines = 8

            title_font = self._load_weekly_font(56, bold=True)
            sub_font = self._load_weekly_font(26)
            stat_font = self._load_weekly_font(28, bold=True)
            card_title_font = self._load_weekly_font(32, bold=True)
            body_font = self._load_weekly_font(28)
            small_font = self._load_weekly_font(22)

            is_daily = str(report_kind).lower() == "daily"
            week_start = (datetime.now() - timedelta(days=7)).strftime("%m.%d")
            week_end = datetime.now().strftime("%m.%d")
            stats_line, sections = self._parse_weekly_sections(summary)
            stats_line = self._clean_weekly_render_text(stats_line) or (
                "今天的B站生活记录" if is_daily else "这一周的B站生活记录"
            )

            # 先量后画：用临时画布把每张卡片的行数算出来，画布高度按内容伸缩
            meas = ImageDraw.Draw(Image.new("RGB", (width, 8)))
            max_text_w = width - margin * 2 - 70
            cards = []
            for title, body in sections:
                title = self._clean_weekly_render_text(title) or "小记"
                if is_daily and title == "本周摘要":
                    title = "今日小记"
                body = self._clean_weekly_render_text(body) or "这块内容有点安静。"
                wrapped = self._wrap_weekly_text(meas, body, body_font, max_text_w)
                if len(wrapped) > max_body_lines:
                    wrapped = wrapped[:max_body_lines]
                    last_line = wrapped[-1].rstrip()
                    wrapped[-1] = (last_line[:-1].rstrip() if len(last_line) > 1 else last_line) + "…"
                card_h = 86 + max(1, len(wrapped)) * line_h + 32
                cards.append((title, wrapped, card_h))

            header_h = 342          # 大标题 + 日期 + 统计条
            footer_h = 116
            content_h = sum(h for _, _, h in cards) + 24 * max(len(cards) - 1, 0)
            minimum_height = 900 if is_daily else 1280
            height = max(minimum_height, header_h + content_h + footer_h + 40)

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
            draw.text((margin, y), "BiliBot 日记" if is_daily else "BiliBot 周报", fill="#24312f", font=title_font)
            y += 72
            date_text = week_end if is_daily else f"{week_start} - {week_end}"
            draw.text((margin + 2, y), f"{date_text} · 自动生成", fill="#6f746f", font=sub_font)

            badge_text = "DAILY" if is_daily else "WEEKLY"
            badge_w = self._text_width(draw, badge_text, small_font) + 42
            self._rounded_rect(draw, (width - margin - badge_w, 86, width - margin, 132), 23, fill=(36, 49, 47, 230))
            draw.text((width - margin - badge_w + 21, 98), badge_text, fill="#f8f0df", font=small_font)

            y += 78
            self._rounded_rect(draw, (margin, y, width - margin, y + 86), 30, fill=(255, 252, 244, 220), outline=(229, 215, 192, 220), width=2)
            stats_line = self._truncate_weekly_line(draw, stats_line, stat_font, width - margin * 2 - 68)
            draw.text((margin + 34, y + 26), stats_line, fill="#4a4f49", font=stat_font)
            y += 118

            palette = ["#d86f45", "#477c73", "#526c9d", "#a56a43", "#6f6f48", "#8b627a"]
            content_bottom = height - 116
            for idx, (title, wrapped, card_h) in enumerate(cards):
                card_x1, card_x2 = margin, width - margin
                if y + card_h > content_bottom:
                    logger.warning("[BiliBot] 总结卡片高度计算异常，停止绘制剩余栏目")
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
            filename_prefix = "daily_summary" if is_daily else "weekly_summary"
            path = os.path.join(TEMP_IMAGE_DIR, f"{filename_prefix}_{int(time.time())}.png")
            img.convert("RGB").save(path, "PNG", optimize=True)
            logger.info(f"[BiliBot] 周总结图片已渲染: {path}")
            return path
        except Exception as e:
            logger.warning(f"[BiliBot] 周总结图片渲染失败: {e}", exc_info=True)
            return None
