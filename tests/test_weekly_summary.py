import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _load_weekly_module(temp_dir):
    package_name = "weekly_test_core"
    package = sys.modules.setdefault(package_name, types.ModuleType(package_name))
    package.__path__ = [str(ROOT / "core")]

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.logger = _Logger()
    astrbot.api = api

    config_name = f"{package_name}.config"
    config = types.ModuleType(config_name)
    for name in (
        "WATCH_LOG_FILE",
        "BANGUMI_WATCH_LOG_FILE",
        "DYNAMIC_LOG_FILE",
        "PROACTIVE_LOG_FILE",
        "WEEKLY_SUMMARY_FILE",
        "DAILY_SUMMARY_FILE",
        "PREFERENCE_STATE_FILE",
    ):
        setattr(config, name, str(Path(temp_dir) / f"{name.lower()}.json"))
    config.TEMP_IMAGE_DIR = str(Path(temp_dir) / "images")
    sys.modules[config_name] = config

    module_name = f"{package_name}.weekly"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "core" / "weekly.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class WeeklySummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.module = _load_weekly_module(self.temp_dir.name)
        self.bot = self.module.WeeklySummaryMixin()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_excerpt_removes_account_identifiers_and_urls(self):
        excerpt = self.bot._weekly_excerpt(
            "[2026-08-16 12:00] 用户123456(小明)说：看看这个 https://example.com/a | Bot回复：好 UID:9988"
        )
        self.assertNotIn("123456", excerpt)
        self.assertNotIn("9988", excerpt)
        self.assertNotIn("https://", excerpt)
        self.assertIn("观众说：", excerpt)

    def test_live_heading_is_parsed_as_its_own_section(self):
        summary = """📅 周报 | 08.09 ~ 08.16
━━━━━━━━━━━━
视频2个 · 直播1场

📺 视频
看了一个很有意思的灯塔短片。

🎙️ 直播
有人聊到配乐，顺着说了两句。

✍️ 碎碎念
这周留下来的都是小片段。"""
        stats, sections = self.bot._parse_weekly_sections(summary)
        self.assertEqual(stats, "视频2个 · 直播1场")
        self.assertEqual([title for title, _body in sections], ["视频", "直播", "碎碎念"])

    def test_long_report_renders_without_exceeding_canvas_limit(self):
        body = "这是用于检查自动换行和极端长文本裁切的内容。" * 80
        sections = "\n\n".join(
            f"{heading}\n{body}"
            for heading in ("📺 视频", "🎬 追番", "🎙️ 直播", "💬 评论区", "📢 动态", "✍️ 碎碎念")
        )
        summary = f"📅 周报 | 08.09 ~ 08.16\n━━━━━━━━━━━━\n视频30个 · 番剧11集 · 动态3条 · 互动7次 · 直播2场\n\n{sections}"
        path = self.bot._render_weekly_summary_image(summary)
        self.assertIsNotNone(path)
        self.assertTrue(Path(path).exists())

        from PIL import Image

        with Image.open(path) as image:
            self.assertEqual(image.width, 1200)
            self.assertLess(image.height, 4000)

    def test_daily_render_uses_daily_filename(self):
        path = self.bot._render_weekly_summary_image("今天看了一段轻松的动画，记住了结尾那句台词。", report_kind="daily")
        self.assertIsNotNone(path)
        self.assertTrue(Path(path).name.startswith("daily_summary_"))
        from PIL import Image

        with Image.open(path) as image:
            self.assertEqual(image.size, (1200, 900))

    def test_prompt_requires_selection_instead_of_recounting(self):
        module = self.module

        class Bot(module.WeeklySummaryMixin):
            def __init__(self):
                self.config = {}
                self.prompt = ""

            def _collect_weekly_data(self, days=7):
                return {
                    "videos": [{"title": "灯塔", "up_name": "某UP", "score": 8, "mood": "平静", "review": "结尾的雾号很有意思"}],
                    "bangumi": [],
                    "dynamics": [],
                    "proactive_comments": [],
                    "chat_count": 1,
                    "active_users": [("123456", 1)],
                    "chat_highlights": ["观众说：结尾是什么意思？ | Bot回复：像是在等下一班船。"],
                    "live_events": [],
                }

            async def _get_system_prompt(self):
                return "测试人设"

            async def _llm_call(self, prompt, **_kwargs):
                self.prompt = prompt
                return "📅 周报 | 08.09 ~ 08.16\n视频1个 · 互动1次\n\n📺 视频\n记住了灯塔结尾的雾号。\n\n✍️ 碎碎念\n这一周很安静。"

        bot = Bot()
        result = asyncio.run(bot._generate_weekly_summary())
        self.assertIsNotNone(result)
        self.assertIn("找出2-4个最有具体信息的片段", bot.prompt)
        self.assertIn("不能充当正文", bot.prompt)
        self.assertIn("疑似错配字幕", bot.prompt)
        self.assertNotIn("123456", bot.prompt)

    def test_structured_daily_summary_keeps_video_signals_without_private_text(self):
        data = self.bot._empty_activity_data()
        data["videos"] = [{
            "time": "2026-08-20 20:00",
            "bvid": "BV-test",
            "title": "灯塔短片",
            "up_name": "某UP",
            "tname": "动画",
            "score": 8.6,
            "score_reason": "雾号与构图很有余味",
            "mood": "平静",
            "review": "结尾没有解释完，反而更像梦。",
            "preference_signals": [{
                "type": "theme", "value": "灯塔", "polarity": "curious",
                "strength": 0.8, "evidence": "结尾留下悬念",
            }],
            "search_keywords": ["灯塔动画短片"],
        }]
        data["chat_count"] = 1
        data["chat_highlights"] = []
        structured = self.bot._build_structured_activity_summary(
            data,
            period_key="2026-08-20",
            preferences=[],
            feedback=[],
        )
        encoded = str(structured)
        self.assertEqual(structured["counts"]["videos"], 1)
        self.assertEqual(structured["video_highlights"][0]["score"], 8.6)
        self.assertEqual(structured["high_score_partitions"][0]["name"], "动画")
        self.assertEqual(structured["frequent_high_score_ups"][0]["name"], "某UP")
        self.assertEqual(structured["mood_distribution"][0], {"mood": "平静", "count": 1})
        self.assertEqual(structured["preference_evidence"][0]["value"], "灯塔")
        self.assertIn("灯塔动画短片", structured["search_keywords"])
        self.assertNotIn("user_id", encoded)

    def test_private_chat_text_is_not_used_as_weekly_highlight(self):
        now = self.module.datetime.now().strftime("%Y-%m-%d %H:%M")
        self.bot._memory = [{
            "memory_type": "chat", "time": now, "user_id": "42",
            "source": "bilibili_private", "text": "这是一段私信原文",
        }]
        self.bot._load_json = lambda _path, default: default
        data = self.bot._collect_weekly_data()
        self.assertEqual(data["chat_count"], 1)
        self.assertEqual(data["chat_highlights"], [])


if __name__ == "__main__":
    unittest.main()
