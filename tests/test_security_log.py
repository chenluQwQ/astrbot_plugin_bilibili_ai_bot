"""core.security.log: the one owner of the security log."""

import os
import tempfile
import unittest

from core.security.log import SecurityLog
from core.storage.activity_log import KINDS, ActivityLog
from core.storage.json_file import JsonFile

KEEP_ENTRIES = KINDS["security"][1]


class SecurityLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = SecurityLog(ActivityLog("security", file=JsonFile(os.path.join(self.temp.name, "security_log.json"))))

    def tearDown(self):
        self.temp.cleanup()

    def test_record_and_read(self):
        self.log.record("injection_attempt", 42, "tester", "x" * 300, "pattern")
        [entry] = self.log.entries()
        self.assertEqual(entry["type"], "injection_attempt")
        self.assertEqual(entry["uid"], "42")
        self.assertEqual(len(entry["content"]), 200)

    def test_keeps_the_newest_entries(self):
        for i in range(KEEP_ENTRIES + 5):
            self.log.record("t", i, "u", "", "")
        entries = self.log.entries()
        self.assertEqual(len(entries), KEEP_ENTRIES)
        self.assertEqual(entries[-1]["uid"], str(KEEP_ENTRIES + 4))


if __name__ == "__main__":
    unittest.main()
