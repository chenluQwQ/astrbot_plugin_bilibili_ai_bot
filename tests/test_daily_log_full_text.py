"""Reviews and comments reach QQ and the recall_today tool in full.

8-4：其余给人看的命令（动态、回复、永久记忆、记忆搜索、好感排行）也不截断，太长按行拆成几条；
只给模型看的上下文用 ``clip``：上限照旧，在句子边界截并标「…」。
"""

from base_parts import attach_base
import asyncio
import tempfile
import types
from pathlib import Path
import unittest
from datetime import datetime

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  (installs the AstrBot stub)
from core.chat_text import clip, split_for_chat
from core.config import (
    BANGUMI_WATCH_LOG_FILE, DYNAMIC_LOG_FILE, PERMANENT_MEMORY_FILE, PROACTIVE_LOG_FILE, REPLY_LOG_FILE,
    WATCH_LOG_FILE,
)
from core.memory_api import BiliBotMemoryAPI
from core.domain.self_memory import SelfMemories
from core.storage.json_file import JsonFile
from command_modules import app_for, load


TODAY = datetime.now().strftime("%Y-%m-%d")
REVIEW = "这一段点评很长，" * 20 + "结尾在这里。"
COMMENT = "评论也不能只剩一半，" * 8 + "评论结尾。"


def _bot(files):
    return attach_base(types.SimpleNamespace(_load_json=lambda path, default=None: files.get(path, default)))


class SplitForChatTests(unittest.TestCase):
    def test_short_text_is_one_message(self):
        self.assertEqual(split_for_chat("a\nb"), ["a\nb"])

    def test_long_text_splits_between_lines(self):
        lines = [f"第{i}行" + "字" * 40 for i in range(100)]
        chunks = split_for_chat("\n".join(lines), limit=500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        self.assertEqual("\n".join(chunks).split("\n"), lines)

    def test_overlong_line_is_kept_whole(self):
        line = "长" * 800
        self.assertEqual(split_for_chat(f"头\n{line}\n尾", limit=500), ["头", line, "尾"])


class DailyLogTests(unittest.TestCase):
    def test_video_log_keeps_review_and_comment(self):
        bot = _bot({
            WATCH_LOG_FILE: [{"time": f"{TODAY} 10:00", "title": "视频", "bvid": "BV1", "score": 8,
                              "mood": "开心", "review": REVIEW, "actions": []}],
            PROACTIVE_LOG_FILE: [{"time": f"{TODAY} 10:01", "title": "视频", "comment": COMMENT}],
        })
        text = load("logs")._render_video_log(app_for(bot), TODAY, False)
        self.assertIn(REVIEW, text)
        self.assertIn(COMMENT, text)

    def test_bangumi_log_keeps_review_and_comment(self):
        bot = _bot({BANGUMI_WATCH_LOG_FILE: [{"time": f"{TODAY} 20:00", "title": "番", "ep_index": 1,
                                              "score": 9, "mood": "感动", "review": REVIEW, "comment": COMMENT}]})
        text = load("logs")._render_bangumi_log(app_for(bot), TODAY, False)
        self.assertIn(REVIEW, text)
        self.assertIn(COMMENT, text)

    def test_recall_today_keeps_review_and_comment(self):
        bot = _bot({
            WATCH_LOG_FILE: [{"time": f"{TODAY} 10:00", "title": "视频", "score": 8, "mood": "开心", "review": REVIEW}],
            PROACTIVE_LOG_FILE: [{"time": f"{TODAY} 10:01", "title": "视频", "comment": COMMENT}],
        })
        text = BiliBotMemoryAPI(bot).activity_overview(TODAY)
        self.assertIn(REVIEW, text)
        self.assertIn(COMMENT, text)


class ClipTests(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(clip("一句话。", 10), "一句话。")

    def test_cuts_after_a_sentence_and_marks_it(self):
        self.assertEqual(clip("第一句说完了。第二句还很长很长很长很长", 12), "第一句说完了。…")

    def test_falls_back_to_a_clause_break(self):
        self.assertEqual(clip("前面一段话说了很多，后面又是一段没有句号的话", 14), "前面一段话说了很多，…")

    def test_break_too_early_is_not_used(self):
        # 唯一的标点在前一半里，截在那儿会丢掉太多，宁可在上限处截
        self.assertEqual(clip("短，" + "字" * 20, 10), "短，" + "字" * 8 + "…")

    def test_hard_cut_only_without_any_break(self):
        self.assertEqual(clip("字" * 20, 10), "字" * 10 + "…")

    def test_never_longer_than_limit_plus_mark(self):
        for text in (REVIEW, COMMENT, "a" * 500):
            self.assertLessEqual(len(clip(text, 60)), 61)


class _Event:
    def __init__(self, text):
        self.message_str = text

    def plain_result(self, text):
        return text


async def _collect(gen):
    return [chunk async for chunk in gen]


class OtherCommandsKeepFullTextTests(unittest.TestCase):
    def test_dynamic_log_keeps_the_whole_post(self):
        bot = _bot({DYNAMIC_LOG_FILE: [{"time": f"{TODAY} 12:00", "text": REVIEW}]})
        self.assertIn(REVIEW, load("logs")._render_dynamic_log(app_for(bot), TODAY, True))

    def test_reply_log_keeps_message_and_reply(self):
        bot = _bot({REPLY_LOG_FILE: [{"time": f"{TODAY} 12:00", "username": "u", "content": COMMENT,
                                      "reply": REVIEW, "score_delta": 1}]})
        text = load("logs")._render_reply_log(app_for(bot), TODAY, False)
        self.assertIn(COMMENT, text)
        self.assertIn(REVIEW, text)

    def test_permanent_memories_are_listed_in_full_and_split(self):
        entries = [{"time": f"{TODAY} 1{i}:00", "text": REVIEW} for i in range(10)]
        bot = _bot({})
        with tempfile.TemporaryDirectory() as data:
            JsonFile(Path(data) / "permanent_memory.json").write(entries)
            bot.self_memories = SelfMemories(JsonFile(Path(data) / "permanent_memory.json"))
            chunks = asyncio.run(_collect(load("memory").permanent(app_for(bot), _Event("/bili永久记忆"))))
        self.assertGreater(len(chunks), 1)
        self.assertEqual("\n".join(chunks).count(REVIEW), 10)

    def test_memory_search_shows_whole_memories(self):
        bot = _bot({})
        app = app_for(bot)

        async def search(*_a, **_k):
            return [REVIEW]
        app.search_memories = search
        self.assertIn(REVIEW, "\n".join(asyncio.run(_collect(load("memory").memory(app, _Event("/bili记忆 点评"))))))


if __name__ == "__main__":
    unittest.main()
