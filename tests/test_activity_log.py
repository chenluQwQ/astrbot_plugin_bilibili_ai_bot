"""活动日志搬进 sqlite：旧 json 启动时导入、重复导入不重复、坏文件不挡、按日期查、每晚清理、库打不开退回 json。"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.storage.activity_log import KINDS, MAX_ROWS, ActivityLogs

LEGACY = Path(__file__).resolve().parent / "fixtures" / "legacy_data"


class ActivityLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bilibot-activity-")
        self.db = os.path.join(self.dir, "bilibot.sqlite3")
        self.opened = []

    def tearDown(self):
        for logs in self.opened:
            logs.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def open(self):
        logs = ActivityLogs(self.dir)
        logs.connect(self.db)
        self.opened.append(logs)
        return logs

    def write(self, name, data):
        Path(self.dir, name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_imports_real_legacy_files_unchanged(self):
        for name, _keep in KINDS.values():
            if (LEGACY / name).exists():
                shutil.copy(LEGACY / name, self.dir)
        logs = self.open()
        self.assertTrue(logs.in_database)
        for kind, (name, keep) in KINDS.items():
            if (LEGACY / name).exists():
                original = json.loads((LEGACY / name).read_text(encoding="utf-8"))
                self.assertEqual(getattr(logs, kind).recent(), original[-keep:], kind)
        # 第二次启动：指纹没变，不重复
        again = self.open()
        self.assertEqual(again.watch.recent(), logs.watch.recent())

    def test_merges_entries_written_by_an_older_version_without_duplicates(self):
        first = {"time": "2026-11-01 10:00", "bvid": "BV1", "score": 7}
        self.write("watch_log.json", [first])
        self.open().watch.append({"time": "2026-11-02 10:00", "bvid": "BV2", "score": 8})
        # 退回旧版本跑了一段：旧版本往 json 里又写了一条
        self.write("watch_log.json", [first, {"time": "2026-11-01 12:00", "bvid": "BV3", "score": 5}])
        logs = self.open()
        self.assertEqual([item["bvid"] for item in logs.watch.recent()], ["BV1", "BV3", "BV2"])

    def test_bad_json_is_skipped_and_kept(self):
        Path(self.dir, "reply_log.json").write_text("[{", encoding="utf-8")
        self.write("watch_log.json", [{"time": "2026-11-01 10:00", "bvid": "BV1"}])
        logs = self.open()
        self.assertEqual(len(logs.watch.recent()), 1)
        self.assertEqual(logs.reply.recent(), [])
        logs.reply.append({"time": "2026-11-01 11:00", "reply": "在"})
        self.assertEqual(Path(self.dir, "reply_log.json").read_text(encoding="utf-8"), "[{")

    def test_recent_keeps_old_window_but_history_stays(self):
        logs = self.open()
        keep = KINDS["proactive"][1]
        for index in range(keep + 30):
            logs.proactive.append({"time": f"2026-11-{1 + index // 100:02d} {index // 60 % 24:02d}:{index % 60:02d}", "n": index})
        recent = logs.proactive.recent()
        self.assertEqual(len(recent), keep)
        self.assertEqual(recent[-1]["n"], keep + 29)
        self.assertEqual(len(logs.proactive.on_day("2026-11-01")), 100)
        self.assertEqual(logs.proactive.days(), {"2026-11-01", "2026-11-02"})

    def test_prune_drops_old_rows_but_keeps_the_window(self):
        logs = self.open()
        logs.dynamic.append({"time": "2026-01-01 10:00", "text": "很久以前"})
        logs.watch.append({"time": "2026-01-01 10:00", "bvid": "BVold"})
        for index in range(KINDS["watch"][1]):
            logs.watch.append({"time": "2026-11-01 10:00", "bvid": f"BV{index}"})
        removed = logs.prune(datetime(2026, 11, 7))
        self.assertEqual(removed, {"watch": 1})
        # 动态只有一条，留着凑够窗口
        self.assertEqual(logs.dynamic.recent()[0]["text"], "很久以前")
        self.assertNotIn("BVold", [item["bvid"] for item in logs.watch.on_day("2026-01-01")])

    def test_prune_caps_rows_per_kind(self):
        logs = self.open()
        conn = sqlite3.connect(self.db)
        conn.executemany("INSERT INTO activity_log(kind, at, data) VALUES('reply', '2026-11-06 10:00', '{}')",
                         [()] * (MAX_ROWS + 5))
        conn.commit()
        conn.close()
        logs.prune(datetime(2026, 11, 7))
        count = logs._conn.execute("SELECT COUNT(*) FROM activity_log WHERE kind='reply'").fetchone()[0]
        self.assertEqual(count, MAX_ROWS)

    def test_json_fallback_reads_and_writes_old_files(self):
        self.write("dynamic_log.json", [{"time": "2026-11-01 10:00", "text": "旧的"}])
        logs = ActivityLogs(self.dir)
        self.assertFalse(logs.in_database)
        logs.dynamic.append({"time": "2026-11-02 10:00", "text": "新的"})
        saved = json.loads(Path(self.dir, "dynamic_log.json").read_text(encoding="utf-8"))
        self.assertEqual([item["text"] for item in saved], ["旧的", "新的"])
        self.assertEqual(logs.dynamic.on_day("2026-11-02")[0]["text"], "新的")
        self.assertEqual(logs.prune(), {})

    def test_export_writes_old_format(self):
        logs = self.open()
        logs.reply.append({"time": "2026-11-02 10:00", "reply": "在"})
        written = logs.export_json()
        self.assertIn(os.path.join(self.dir, "reply_log.json"), written)
        saved = json.loads(Path(self.dir, "reply_log.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, [{"time": "2026-11-02 10:00", "reply": "在"}])


if __name__ == "__main__":
    unittest.main()
