"""core.storage.migration: backups and json → sqlite imports, with fake modules."""

import json
import os
import sqlite3
import tempfile
import time
import unittest

from core.storage.migration import (
    Importer,
    MigrationRunner,
    backup_data_dir,
    imported_json_files,
    record_fingerprint,
    stow_json_files,
)


class NotesImporter(Importer):
    """Rows with a primary key and an update time: newer wins."""

    module = "notes"
    sources = ("notes.json",)

    def prepare(self, conn):
        conn.execute("CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, text TEXT, updated_at REAL)")

    def import_data(self, conn, data):
        for item in data["notes.json"].get("items", []):
            conn.execute(
                "INSERT INTO notes(id, text, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at "
                "WHERE excluded.updated_at > notes.updated_at",
                (item["id"], item["text"], item["updated_at"]),
            )

    def export_data(self, conn):
        rows = conn.execute("SELECT id, text, updated_at FROM notes ORDER BY id").fetchall()
        return {"notes.json": {"items": [dict(row) for row in rows]}}


class LogImporter(Importer):
    """Log entries without ids: deduplicated by fingerprint."""

    module = "log"
    sources = ("log.json",)
    defaults = {"log.json": []}

    def prepare(self, conn):
        conn.execute("CREATE TABLE IF NOT EXISTS log (fp TEXT PRIMARY KEY, time TEXT, text TEXT)")

    def import_data(self, conn, data):
        for item in data["log.json"]:
            conn.execute(
                "INSERT OR IGNORE INTO log(fp, time, text) VALUES(?, ?, ?)",
                (record_fingerprint(item["time"], "log", item["text"]), item["time"], item["text"]),
            )


class BrokenImporter(NotesImporter):
    module = "broken"

    def import_data(self, conn, data):
        super().import_data(conn, data)
        raise RuntimeError("boom halfway")


class _DataDir(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = self.temp.name
        self.db = os.path.join(self.data, "bilibot.sqlite3")
        self.runner = MigrationRunner(self.db, self.data)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, content):
        with open(os.path.join(self.data, name), "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False)

    def read_raw(self, name):
        with open(os.path.join(self.data, name), encoding="utf-8") as f:
            return f.read()

    def rows(self, sql):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


class MigrationTests(_DataDir):
    def test_first_import_writes_rows_and_leaves_json_in_place(self):
        self.write("notes.json", {"items": [{"id": "a", "text": "一", "updated_at": 1}]})
        before = self.read_raw("notes.json")
        report = self.runner.run([NotesImporter()])
        self.assertEqual(report.results["notes"].status, "imported")
        self.assertEqual(self.rows("SELECT id, text FROM notes"), [("a", "一")])
        self.assertEqual(self.read_raw("notes.json"), before)

    def test_rerun_without_changes_is_skipped_and_does_not_duplicate(self):
        self.write("log.json", [{"time": "10:00", "text": "看了视频"}])
        self.runner.run([LogImporter()])
        report = self.runner.run([LogImporter()])
        self.assertEqual(report.results["log"].status, "up_to_date")
        self.assertEqual(len(self.rows("SELECT * FROM log")), 1)

    def test_downgrade_then_upgrade_merges_without_duplicates(self):
        self.write("notes.json", {"items": [{"id": "a", "text": "旧", "updated_at": 1},
                                            {"id": "b", "text": "b1", "updated_at": 1}]})
        self.write("log.json", [{"time": "10:00", "text": "看了视频"}])
        self.runner.run([NotesImporter(), LogImporter()])

        # The new version keeps writing to sqlite...
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE notes SET text='新版改的', updated_at=5 WHERE id='b'")
        conn.commit()
        conn.close()
        # ...then the user downgrades, and the old version writes json.
        self.write("notes.json", {"items": [{"id": "a", "text": "旧版改的", "updated_at": 3},
                                            {"id": "b", "text": "旧版也改了", "updated_at": 2},
                                            {"id": "c", "text": "旧版新增", "updated_at": 3}]})
        self.write("log.json", [{"time": "10:00", "text": "看了视频"}, {"time": "11:00", "text": "又看了一个"}])

        report = self.runner.run([NotesImporter(), LogImporter()])
        self.assertEqual(report.results["notes"].status, "imported")
        self.assertEqual(
            self.rows("SELECT id, text FROM notes ORDER BY id"),
            [("a", "旧版改的"), ("b", "新版改的"), ("c", "旧版新增")],
        )
        self.assertEqual(len(self.rows("SELECT * FROM log")), 2)

    def test_corrupt_json_skips_only_that_module_and_keeps_the_file(self):
        with open(os.path.join(self.data, "notes.json"), "w", encoding="utf-8") as f:
            f.write('{"items": [')
        self.write("log.json", [{"time": "10:00", "text": "ok"}])
        with self.assertLogs("astrbot", level="ERROR"):
            report = self.runner.run([NotesImporter(), LogImporter()])
        self.assertEqual(report.results["notes"].status, "failed")
        self.assertEqual(report.results["log"].status, "imported")
        self.assertEqual(report.disabled_modules, ["notes"])
        self.assertEqual(self.read_raw("notes.json"), '{"items": [')
        self.assertEqual(self.rows("SELECT * FROM json_imports WHERE module='notes'"), [])

    def test_failure_halfway_rolls_back_and_retries_next_time(self):
        self.write("notes.json", {"items": [{"id": "a", "text": "一", "updated_at": 1}]})
        with self.assertLogs("astrbot", level="ERROR"):
            report = self.runner.run([BrokenImporter()])
        self.assertEqual(report.results["broken"].status, "failed")
        # Even the table creation inside the transaction is rolled back.
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name='notes'"), [])
        self.assertEqual(self.rows("SELECT * FROM json_imports"), [])
        report = self.runner.run([NotesImporter()])
        self.assertEqual(report.results["notes"].status, "imported")

    def test_missing_file_imports_the_default(self):
        report = self.runner.run([LogImporter()])
        self.assertEqual(report.results["log"].status, "imported")
        self.assertEqual(self.rows("SELECT source, fingerprint FROM json_imports"), [("log.json", "missing")])

    def test_database_that_cannot_open_disables_every_module(self):
        runner = MigrationRunner(os.path.join(self.data, "no", "such", "dir", "x.sqlite3"), self.data)
        with self.assertLogs("astrbot", level="ERROR"):
            report = runner.run([NotesImporter(), LogImporter()])
        self.assertEqual({r.status for r in report.results.values()}, {"db_unavailable"})
        self.assertEqual(sorted(report.disabled_modules), ["log", "notes"])

    def test_export_writes_old_json_format(self):
        self.write("notes.json", {"items": [{"id": "a", "text": "一", "updated_at": 1}]})
        self.runner.run([NotesImporter()])
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO notes VALUES('z', '升级后新增', 9)")
        conn.commit()
        conn.close()
        self.runner.export(NotesImporter())
        with open(os.path.join(self.data, "notes.json"), encoding="utf-8") as f:
            self.assertEqual([item["id"] for item in json.load(f)["items"]], ["a", "z"])


class StowOldJsonTests(_DataDir):
    """只挪导入过、之后没改过的；挪走以后再启动，库里的数据不变。"""

    def test_only_unchanged_imported_files_are_stowed(self):
        self.write("notes.json", {"items": [{"id": "a", "text": "一", "updated_at": 1}]})
        self.write("log.json", [{"time": "t1", "text": "x"}])
        self.write("other.json", {"not": "imported"})
        self.runner.run([NotesImporter(), LogImporter()])
        # 数据库断开期间又写过：指纹变了，下次启动还要合并，不能挪
        self.write("log.json", [{"time": "t1", "text": "x"}, {"time": "t2", "text": "y"}])
        self.assertEqual(imported_json_files(self.db, self.data), ["notes.json"])

        target = stow_json_files(self.data, ["notes.json"])
        self.assertFalse(os.path.exists(os.path.join(self.data, "notes.json")))
        self.assertTrue(os.path.exists(os.path.join(target, "notes.json")))
        self.assertTrue(os.path.exists(os.path.join(self.data, "other.json")))

        # 下次启动：文件没了，按空的导入一次，库里还是原来的
        self.runner.run([NotesImporter()])
        self.assertEqual(self.rows("SELECT id, text FROM notes"), [("a", "一")])
        self.assertEqual(imported_json_files(self.db, self.data), [])

    def test_nothing_before_any_import(self):
        self.assertEqual(imported_json_files(self.db, self.data), [])
        sqlite3.connect(self.db).close()
        self.assertEqual(imported_json_files(self.db, self.data), [])


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = self.temp.name
        self.db = os.path.join(self.data, "bilibot.sqlite3")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        conn.close()
        with open(os.path.join(self.data, "memory.json"), "w", encoding="utf-8") as f:
            f.write("[1]")
        os.makedirs(os.path.join(self.data, "temp_videos"))
        with open(os.path.join(self.data, "temp_videos", "big.mp4"), "wb") as f:
            f.write(b"0" * 10)

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_copies_json_and_database_once_per_version(self):
        target = backup_data_dir(self.data, "1.8.0", self.db)
        self.assertEqual(sorted(os.listdir(target)), ["bilibot.sqlite3", "memory.json"])
        conn = sqlite3.connect(os.path.join(target, "bilibot.sqlite3"))
        self.assertEqual(conn.execute("SELECT x FROM t").fetchall(), [(42,)])
        conn.close()
        self.assertIsNone(backup_data_dir(self.data, "1.8.0", self.db))

    def test_only_the_newest_backups_are_kept(self):
        for i in range(4):
            target = backup_data_dir(self.data, f"1.8.{i}", self.db, keep=2)
            past = time.time() - 100 + i
            os.utime(target, (past, past))
        names = sorted(os.listdir(os.path.join(self.data, "backup")))
        self.assertEqual([name.split("-")[0] for name in names], ["1.8.2", "1.8.3"])


if __name__ == "__main__":
    unittest.main()
