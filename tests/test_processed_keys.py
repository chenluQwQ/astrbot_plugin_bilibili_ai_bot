"""去重账本搬进 sqlite：旧 json 启动时导入、重复导入不重复、坏文件不挡、每晚截评论和 @、库打不开退回 json、导出。"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core.storage.processed_keys import KINDS, ProcessedKeys

LEGACY = Path(__file__).resolve().parent / "fixtures" / "legacy_data"


class ProcessedKeysTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bilibot-keys-")
        self.db = os.path.join(self.dir, "bilibot.sqlite3")
        self.opened = []

    def tearDown(self):
        for keys in self.opened:
            keys.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def open(self):
        keys = ProcessedKeys(self.dir)
        keys.connect(self.db)
        self.opened.append(keys)
        return keys

    def write(self, name, data):
        Path(self.dir, name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def read(self, name):
        return json.loads(Path(self.dir, name).read_text(encoding="utf-8"))

    def test_imports_real_legacy_files(self):
        for name, _keep in KINDS.values():
            shutil.copy(LEGACY / name, self.dir)
        keys = self.open()
        self.assertTrue(keys.in_database)
        for kind in ("comment", "at", "commented_video"):
            original = json.loads((LEGACY / KINDS[kind][0]).read_text(encoding="utf-8"))
            self.assertEqual(getattr(keys, kind).all(), set(original), kind)
        # 样例里的语义去重库全是早期的字符串指纹，不要
        self.assertEqual(keys.content.items(), [])
        again = self.open()
        self.assertEqual(again.comment.all(), keys.comment.all())

    def test_merges_keys_written_by_an_older_version(self):
        self.write("replied.json", ["1"])
        self.open().comment.add(["2"])
        self.write("replied.json", ["1", "3"])
        keys = self.open()
        self.assertEqual(keys.comment.all(), {"1", "2", "3"})
        self.assertTrue(keys.comment.has("3"))
        self.assertFalse(keys.comment.has("4"))

    def test_bad_json_is_skipped_and_kept(self):
        Path(self.dir, "replied_at.json").write_text("[", encoding="utf-8")
        self.write("replied.json", ["1"])
        keys = self.open()
        self.assertEqual(keys.comment.all(), {"1"})
        keys.at.add(["9"])
        self.assertTrue(keys.at.has("9"))
        self.assertEqual(Path(self.dir, "replied_at.json").read_text(encoding="utf-8"), "[")

    def test_prune_keeps_newest_comments_and_ats_but_not_videos(self):
        keys = self.open()
        keys.comment.add(str(i) for i in range(2100))
        keys.at.add(["a"])
        keys.commented_video.add(f"BV{i}" for i in range(2100))
        self.assertEqual(keys.prune(), {"comment": 100})
        self.assertFalse(keys.comment.has("99"))
        self.assertTrue(keys.comment.has("100"))
        self.assertEqual(len(keys.commented_video.all()), 2100)

    def test_content_keeps_last_80(self):
        keys = self.open()
        for i in range(85):
            keys.content.append({"text": f"t{i}", "embedding": [i], "time": "2026-11-07 10:00"})
        items = keys.content.items()
        self.assertEqual(len(items), 80)
        self.assertEqual(items[0]["text"], "t5")
        self.assertEqual(items[-1]["embedding"], [84])

    def test_json_fallback_reads_and_writes_old_files(self):
        self.write("commented_videos.json", ["BV1"])
        keys = ProcessedKeys(self.dir)
        self.assertFalse(keys.in_database)
        keys.commented_video.add(["BV2", "BV1"])
        self.assertEqual(self.read("commented_videos.json"), ["BV1", "BV2"])
        keys.content.append({"text": "你好", "embedding": [1], "time": "2026-11-07 10:00"})
        self.assertEqual(self.read("replied_content_keys.json")[0]["text"], "你好")
        self.assertEqual(keys.prune(), {})
        self.assertEqual(keys.export_json(), [])

    def test_export_writes_old_format(self):
        keys = self.open()
        keys.comment.add(["1", "2"])
        keys.content.append({"text": "你好", "embedding": [1], "time": "2026-11-07 10:00"})
        written = keys.export_json()
        self.assertIn(os.path.join(self.dir, "replied.json"), written)
        self.assertEqual(self.read("replied.json"), ["1", "2"])
        self.assertEqual(self.read("replied_content_keys.json"), [{"text": "你好", "embedding": [1], "time": "2026-11-07 10:00"}])


if __name__ == "__main__":
    unittest.main()
