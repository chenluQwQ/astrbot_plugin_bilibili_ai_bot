"""core.domain.blocklist: the one owner of block_log.json and block_count.json."""

import os
import tempfile
import unittest

from core.domain.blocklist import BlockList
from core.storage.json_file import JsonFile


class BlockListTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.blocklist = BlockList(
            JsonFile(os.path.join(self.temp.name, "block_log.json")),
            JsonFile(os.path.join(self.temp.name, "block_count.json")),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_add_contains_remove(self):
        self.assertFalse(self.blocklist.contains(123))
        self.blocklist.add(123, {"reason": "spam"})
        self.assertTrue(self.blocklist.contains("123"))
        self.assertEqual(self.blocklist.entries(), {"123": {"reason": "spam"}})
        self.assertTrue(self.blocklist.remove("123"))
        self.assertFalse(self.blocklist.remove("123"))
        self.assertEqual(self.blocklist.entries(), {})

    def test_entries_is_a_copy(self):
        self.blocklist.add("1", {"reason": "x"})
        self.blocklist.entries().pop("1")
        self.assertTrue(self.blocklist.contains("1"))

    def test_strikes(self):
        self.assertEqual(self.blocklist.strikes("7"), 0)
        self.assertFalse(self.blocklist.has_strikes_record("7"))
        self.blocklist.set_strikes("7", 2)
        self.assertEqual(self.blocklist.strikes(7), 2)
        self.blocklist.set_strikes("7", 0)
        self.assertTrue(self.blocklist.has_strikes_record("7"))


if __name__ == "__main__":
    unittest.main()
