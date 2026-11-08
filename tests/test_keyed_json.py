"""人和关系搬进 sqlite：旧 json 启动时导入、只写变了的行、旧版本写过的合并进来、坏文件不挡、库打不开退回 json、导出。"""

import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

import test_autonomous_ranges  # noqa: F401  装上假的 astrbot
from core.config import MEMORY_DOC_FILES, PEOPLE_FILES, STATE_FILES
from core.domain.affection import Affection
from core.storage.keyed_json import KeyedDocs, StateDocs

LEGACY = Path(__file__).resolve().parent / "fixtures" / "legacy_data"


class KeyedJsonTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bilibot-people-")
        self.db = os.path.join(self.dir, "bilibot.sqlite3")
        self.opened = []

    def tearDown(self):
        for docs in self.opened:
            docs.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def open(self):
        docs = KeyedDocs(self.dir, PEOPLE_FILES)
        docs.connect(self.db)
        self.opened.append(docs)
        return docs

    def write(self, name, data):
        Path(self.dir, name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def read(self, name):
        return json.loads(Path(self.dir, name).read_text(encoding="utf-8"))

    def test_imports_real_legacy_files_unchanged(self):
        names = [name for name in PEOPLE_FILES.values() if (LEGACY / name).exists()]
        self.assertTrue(names)
        for name in names:
            shutil.copy(LEGACY / name, self.dir)
        docs = self.open()
        self.assertTrue(docs.in_database)
        for kind, name in PEOPLE_FILES.items():
            if (LEGACY / name).exists():
                original = json.loads((LEGACY / name).read_text(encoding="utf-8"))
                self.assertEqual(getattr(docs, kind).read({}), original, kind)
        again = self.open()
        self.assertEqual(again.user_profiles.read({}), docs.user_profiles.read({}))

    def test_memory_docs_import_real_legacy_files_unchanged(self):
        for name in MEMORY_DOC_FILES.values():
            shutil.copy(LEGACY / name, self.dir)
        docs = KeyedDocs(self.dir, MEMORY_DOC_FILES)
        docs.connect(self.db)
        self.opened.append(docs)
        for kind, name in MEMORY_DOC_FILES.items():
            original = json.loads((LEGACY / name).read_text(encoding="utf-8"))
            self.assertEqual(getattr(docs, kind).read({}), original, kind)
            self.assertEqual(list(getattr(docs, kind).read({})), list(original), kind)
        # 和人和关系同一张表，互不干扰
        people = self.open()
        people.affection.write({"1": 5})
        self.assertEqual(len(docs.video_memory.read({})), 12)

    def test_write_only_touches_changed_rows_and_drops_missing(self):
        docs = self.open()
        docs.affection.write({"1": 5, "2": 7})
        docs.affection.update(lambda data: {**{k: v for k, v in data.items() if k != "2"}, "3": 1})
        self.assertEqual(docs.affection.read({}), {"1": 5, "3": 1})
        rows = docs._conn.execute("SELECT key FROM keyed_json WHERE kind='affection' ORDER BY key").fetchall()
        self.assertEqual([row[0] for row in rows], ["1", "3"])
        # 别的种类不受影响
        docs.milestones.write({"1": [10]})
        self.assertEqual(docs.affection.read({}), {"1": 5, "3": 1})
        # 顺序和原来 json 一样：改了的留在原位，新的在最后
        docs.affection.update(lambda data: {"3": 2, "1": 6, "10": 0})
        self.assertEqual(list(docs.affection.read({})), ["1", "3", "10"])

    def test_merges_what_an_older_version_wrote(self):
        self.write("affection.json", {"1": 5, "2": 7})
        self.open().affection.update(lambda data: {**data, "3": 9})
        # 退回旧版本跑过：它只认 json，改了 1 号、没有 3 号
        self.write("affection.json", {"1": 6, "2": 7})
        self.assertEqual(self.open().affection.read({}), {"1": 6, "2": 7, "3": 9})

    def test_stale_json_does_not_overwrite_newer_scores(self):
        """第二次启动时 json 已经过期：好感度接上数据库后重读，主人满分写的是数据库里的新分数。"""
        self.write("affection.json", {"1": 5})
        first = self.open()
        Affection(first.affection, lambda uid: False).set("1", 40)
        first.close()
        docs = KeyedDocs(self.dir, PEOPLE_FILES)
        affection = Affection(docs.affection, lambda uid: uid == "9", docs.profile_reached)
        self.assertEqual(affection.score("1"), 5)  # 还没接上：读到的是过期的 json
        docs.connect(self.db)
        self.opened.append(docs)
        affection.reload()
        affection.pin_owner("9")
        self.assertEqual(docs.affection.read({}), {"1": 40, "9": 100})
        self.assertEqual(self.read("affection.json"), {"1": 5})

    def test_bad_json_is_skipped_and_kept(self):
        Path(self.dir, "user_profiles.json").write_text("{", encoding="utf-8")
        self.write("affection.json", {"1": 5})
        docs = self.open()
        self.assertEqual(docs.affection.read({}), {"1": 5})
        self.assertEqual(docs.user_profiles.read({}), {})
        docs.user_profiles.write({"1": {"username": "新"}})
        self.assertEqual(Path(self.dir, "user_profiles.json").read_text(encoding="utf-8"), "{")

    def test_locked_read_modify_write_from_threads(self):
        docs = self.open()
        docs.affection.write({"n": 0})

        def bump():
            for _ in range(50):
                docs.affection.update(lambda data: {**data, "n": data["n"] + 1})

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(docs.affection.read({})["n"], 200)

    def test_json_fallback_reads_and_writes_old_files(self):
        self.write("qq_bili_bindings.json", {"10": "1"})
        docs = KeyedDocs(self.dir, PEOPLE_FILES)
        self.assertFalse(docs.in_database)
        docs.qq_bindings.update(lambda data: {**data, "20": "2"})
        self.assertEqual(self.read("qq_bili_bindings.json"), {"10": "1", "20": "2"})
        self.assertEqual(docs.export_json(), [])

    def test_export_writes_old_format(self):
        docs = self.open()
        docs.milestones.write({"1": [10, 30]})
        written = docs.export_json()
        self.assertIn(os.path.join(self.dir, "milestones.json"), written)
        self.assertEqual(self.read("milestones.json"), {"1": [10, 30]})


class StateDocsTests(unittest.TestCase):
    """小状态：整份 json 存一行。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bilibot-states-")
        self.db = os.path.join(self.dir, "bilibot.sqlite3")
        self.opened = []

    def tearDown(self):
        for docs in self.opened:
            docs.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def open(self):
        docs = StateDocs(self.dir, STATE_FILES)
        docs.connect(self.db)
        self.opened.append(docs)
        return docs

    def test_imports_real_legacy_files_unchanged(self):
        present = {kind: name for kind, name in STATE_FILES.items() if (LEGACY / name).exists()}
        self.assertGreaterEqual(len(present), 10)
        for name in present.values():
            shutil.copy(LEGACY / name, self.dir)
        docs = self.open()
        for kind, name in present.items():
            original = json.loads((LEGACY / name).read_text(encoding="utf-8"))
            self.assertEqual(getattr(docs, kind).read({}), original, kind)
        # 没有文件的：读到给的默认值
        self.assertEqual(docs.creator_activity.read([]), [])

    def test_whole_doc_round_trip_delete_and_merge(self):
        Path(self.dir, "mood.json").write_text(json.dumps({"mood": "开心"}, ensure_ascii=False), encoding="utf-8")
        docs = self.open()
        docs.mood.update(lambda data: {**data, "intensity": 2})
        docs.permanent_memory.write([{"text": "第一条"}])
        self.assertEqual(docs.mood.read({}), {"mood": "开心", "intensity": 2})
        self.assertEqual(docs.permanent_memory.read([]), [{"text": "第一条"}])
        docs.schedule.write({"date": "2026-11-07"})
        docs.schedule.delete()
        self.assertEqual(docs.schedule.read({}), {})
        docs.close()
        # 退回旧版本改过 mood.json：整份以 json 为准
        Path(self.dir, "mood.json").write_text(json.dumps({"mood": "低落"}, ensure_ascii=False), encoding="utf-8")
        again = self.open()
        self.assertEqual(again.mood.read({}), {"mood": "低落"})
        self.assertEqual(again.permanent_memory.read([]), [{"text": "第一条"}])

    def test_export_and_json_fallback(self):
        docs = self.open()
        docs.up_skip.write({"1": {"until": "2026-12-01"}})
        self.assertIn(os.path.join(self.dir, "up_skip.json"), docs.export_json())
        self.assertEqual(json.loads(Path(self.dir, "up_skip.json").read_text(encoding="utf-8")), {"1": {"until": "2026-12-01"}})
        plain = StateDocs(self.dir, STATE_FILES)
        plain.tell_owner.write({"date": "2026-11-07", "sent": []})
        self.assertTrue(Path(self.dir, "tell_owner.json").exists())
        plain.tell_owner.delete()
        self.assertFalse(Path(self.dir, "tell_owner.json").exists())


if __name__ == "__main__":
    unittest.main()
