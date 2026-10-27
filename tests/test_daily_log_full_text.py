"""Reviews and comments reach QQ and the recall_today tool in full."""

import types
import unittest
from datetime import datetime

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  (installs the AstrBot stub)
from core.chat_text import split_for_chat
from core.config import BANGUMI_WATCH_LOG_FILE, PROACTIVE_LOG_FILE, WATCH_LOG_FILE
from core.memory_api import BiliBotMemoryAPI
from test_audit_fixes_main import _load_methods


TODAY = datetime.now().strftime("%Y-%m-%d")
REVIEW = "这一段点评很长，" * 20 + "结尾在这里。"
COMMENT = "评论也不能只剩一半，" * 8 + "评论结尾。"


def _bot(files):
    return types.SimpleNamespace(_load_json=lambda path, default=None: files.get(path, default))


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
        render = _load_methods(["_render_video_log"], {"WATCH_LOG_FILE": WATCH_LOG_FILE,
                                                       "PROACTIVE_LOG_FILE": PROACTIVE_LOG_FILE})["_render_video_log"]
        text = render(bot, TODAY, False)
        self.assertIn(REVIEW, text)
        self.assertIn(COMMENT, text)

    def test_bangumi_log_keeps_review_and_comment(self):
        bot = _bot({BANGUMI_WATCH_LOG_FILE: [{"time": f"{TODAY} 20:00", "title": "番", "ep_index": 1,
                                              "score": 9, "mood": "感动", "review": REVIEW, "comment": COMMENT}]})
        render = _load_methods(["_render_bangumi_log"],
                               {"BANGUMI_WATCH_LOG_FILE": BANGUMI_WATCH_LOG_FILE})["_render_bangumi_log"]
        text = render(bot, TODAY, False)
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


if __name__ == "__main__":
    unittest.main()
