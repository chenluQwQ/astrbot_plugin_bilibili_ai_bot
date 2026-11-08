"""core.storage.json_file: atomic writes, locked updates, corrupt files kept."""

import glob
import json
import os
import tempfile
import threading
import unittest

from core.storage.json_file import KEEP_CORRUPT_COPIES, JsonFile


class JsonFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "data.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_file_returns_default(self):
        self.assertEqual(JsonFile(self.path).read(), {})
        self.assertEqual(JsonFile(self.path).read([]), [])

    def test_write_then_read(self):
        JsonFile(self.path).write({"中文": [1, 2]})
        self.assertEqual(JsonFile(self.path).read(), {"中文": [1, 2]})
        self.assertEqual(os.listdir(self.temp.name), ["data.json"])

    def test_corrupt_file_is_copied_before_it_can_be_overwritten(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"half": ')
        with self.assertLogs("astrbot", level="ERROR"):
            self.assertEqual(JsonFile(self.path).read([]), [])
        backups = glob.glob(self.path + ".corrupt-*")
        self.assertEqual(len(backups), 1)
        with open(backups[0], encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"half": ')

        # Reading the same broken file again does not pile up copies.
        JsonFile(self.path).read([])
        self.assertEqual(len(glob.glob(self.path + ".corrupt-*")), 1)

        JsonFile(self.path).write(["new"])
        with open(backups[0], encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"half": ')

    def test_only_the_newest_corrupt_copies_are_kept(self):
        old_copies = [f"{self.path}.corrupt-2020010{i}-000000" for i in range(1, 6)]
        for name in old_copies:
            with open(name, "w", encoding="utf-8") as f:
                f.write("old")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not json")
        with self.assertLogs("astrbot", level="ERROR"):
            JsonFile(self.path).read()
        remaining = sorted(glob.glob(self.path + ".corrupt-*"))
        self.assertEqual(len(remaining), KEEP_CORRUPT_COPIES)
        self.assertEqual(remaining[:2], old_copies[-2:])
        with open(remaining[-1], encoding="utf-8") as f:
            self.assertEqual(f.read(), "not json")

    def test_update_keeps_every_concurrent_change(self):
        file = JsonFile(self.path)
        file.write([])

        def add(i):
            file.update(lambda data: data + [i], [])

        threads = [threading.Thread(target=add, args=(i,)) for i in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(file.read([])), list(range(40)))

    def test_update_accepts_in_place_changes(self):
        file = JsonFile(self.path)
        file.write({"n": 1})
        self.assertEqual(file.update(lambda data: data.update(n=2)), {"n": 2})
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"n": 2})

    def test_failed_write_leaves_old_file_and_no_temp(self):
        file = JsonFile(self.path)
        file.write({"ok": True})
        with self.assertRaises(TypeError):
            file.write({"bad": object()})
        self.assertEqual(file.read(), {"ok": True})
        self.assertEqual(os.listdir(self.temp.name), ["data.json"])


if __name__ == "__main__":
    unittest.main()
