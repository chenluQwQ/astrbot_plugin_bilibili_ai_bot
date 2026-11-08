"""回复相关的三个数据主人：回复日志、处理过的评论和 @、语义去重库。"""
import unittest
from datetime import datetime

import test_autonomous_ranges  # noqa: F401  装上假的 astrbot
from core.domain.replied_comments import RepliedComments
from core.domain.replied_content import RepliedContent
from core.domain.reply_log import ReplyLog
from core.storage.activity_log import ActivityLog
from core.storage.processed_keys import ContentEntries, KeySet


class MemFile:
    def __init__(self, data=None):
        self.data = data

    def read(self, default=None):
        return default if self.data is None else self.data

    def write(self, data):
        self.data = data

    def update(self, change, default=None):
        self.data = change(self.read(default))
        return self.data


class ReplyLogTests(unittest.TestCase):
    def test_counts_and_duplicates_by_channel(self):
        today = datetime.now().strftime("%Y-%m-%d %H:%M")
        log = ReplyLog(ActivityLog("reply", file=MemFile([
            {"time": today, "content": "评论一"}, {"time": today, "content": "私信", "channel": "private"},
            {"time": "2000-01-01 00:00", "content": "旧评论"}, {"time": today, "content": "弹幕", "channel": "live"},
        ])))
        self.assertEqual(log.today_count("comment"), 1)  # 弹幕不算评论
        self.assertEqual(log.today_count("private"), 1)
        self.assertEqual(log.recent_contents("comment", 120), ["弹幕", "旧评论", "评论一"])  # 查重时弹幕算评论（原样）
        self.assertEqual(log.recent_contents("private", 120), ["私信"])

    def test_record_keeps_last_500(self):
        file = MemFile([{"i": i} for i in range(500)])
        ReplyLog(ActivityLog("reply", file=file)).record({"i": 500})
        self.assertEqual(len(file.data), 500)
        self.assertEqual(file.data[-1], {"i": 500})


class RepliedCommentsTests(unittest.TestCase):
    def test_at_ids_live_in_memory_until_saved(self):
        at_file = MemFile(["1"])
        replied = RepliedComments(KeySet("comment", file=MemFile()), KeySet("at", file=at_file))
        self.assertTrue(replied.at_seen("1"))
        replied.remember_at("2")
        self.assertEqual(at_file.data, ["1"])
        replied.save_at()
        self.assertEqual(sorted(at_file.data), ["1", "2"])

    def test_trim(self):
        file = MemFile([str(i) for i in range(2100)])
        replied = RepliedComments(KeySet("comment", file=file), KeySet("at", file=MemFile()))
        self.assertEqual(replied.trim(), (2100, 2000))
        self.assertEqual(file.data[0], "100")
        self.assertIsNone(replied.trim())


class RepliedContentTests(unittest.TestCase):
    def test_similar_and_record(self):
        file = MemFile(["旧指纹"])
        store = RepliedContent(ContentEntries(file=file))
        cosine = lambda a, b: 1.0 if a == b else 0.0
        self.assertFalse(store.has_similar([1, 0], 0.9, cosine))
        store.record("你好", [1, 0])
        self.assertTrue(store.has_similar([1, 0], 0.9, cosine))
        self.assertEqual([it["text"] for it in file.data], ["你好"])


if __name__ == "__main__":
    unittest.main()
