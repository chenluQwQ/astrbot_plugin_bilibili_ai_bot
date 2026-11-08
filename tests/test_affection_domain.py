"""core.domain.affection: the one owner of affection.json."""

import json
import os
import tempfile
import unittest

from core.domain.affection import OWNER_SCORE, Affection, Milestones
from core.storage.json_file import JsonFile


class AffectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "affection.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"1": 40, "2": -20, "3": 60}, f)
        self.affection = Affection(JsonFile(self.path), lambda uid: str(uid) == "999")

    def tearDown(self):
        self.temp.cleanup()

    def stored(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_reads_scores_and_unknown_users_are_zero(self):
        self.assertEqual(self.affection.score("1"), 40)
        self.assertEqual(self.affection.score(1), 40)
        self.assertEqual(self.affection.score("404"), 0)
        self.assertTrue(self.affection.known("2"))
        self.assertFalse(self.affection.known("404"))

    def test_set_persists_immediately(self):
        self.affection.set("4", 12)
        self.assertEqual(self.stored()["4"], 12)
        self.assertEqual(Affection(JsonFile(self.path), lambda _: False).score("4"), 12)

    def test_owner_is_pinned_to_full_score(self):
        self.affection.pin_owner("999")
        self.assertEqual(self.stored()["999"], OWNER_SCORE)
        self.affection.pin_owner("")
        self.assertNotIn("", self.stored())

    def test_ranking_total_and_size(self):
        self.assertEqual(self.affection.ranking(2), [("3", 60), ("1", 40)])
        self.assertEqual(self.affection.total(), 80)
        self.assertEqual(len(self.affection), 3)

    def test_levels(self):
        cases = {-10: "cold", 0: "stranger", 11: "normal", 31: "friend", 51: "close"}
        for score, level in cases.items():
            self.assertEqual(self.affection.level(score, "1"), level, score)
        self.assertEqual(self.affection.level(-50, "999"), "special")

    def test_snapshot_is_a_copy(self):
        snapshot = self.affection.snapshot()
        snapshot["1"] = 0
        self.assertEqual(self.affection.score("1"), 40)

    def test_missing_file_starts_empty(self):
        missing = Affection(JsonFile(os.path.join(self.temp.name, "none.json")), lambda _: False)
        self.assertEqual(len(missing), 0)


class MilestoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.milestones = Milestones(JsonFile(os.path.join(self.temp.name, "milestones.json")))

    def tearDown(self):
        self.temp.cleanup()

    def test_crossing_a_threshold_is_reported_with_the_name(self):
        threshold, message = self.milestones.peek("1", 8, 12, "小明")
        self.assertEqual(threshold, 10)
        self.assertIn("小明", message)
        self.assertIsNone(self.milestones.peek("1", 10, 12, "小明"))

    def test_peek_records_nothing_until_commit(self):
        self.assertIsNotNone(self.milestones.peek("1", 8, 12, "小明"))
        self.assertIsNotNone(self.milestones.peek("1", 8, 12, "小明"))
        self.milestones.commit("1", 10, "小明")
        self.assertIsNone(self.milestones.peek("1", 8, 12, "小明"))
        self.assertIsNotNone(self.milestones.peek("2", 8, 12, "小红"))

    def test_lowest_uncommitted_threshold_comes_first(self):
        self.milestones.commit("1", 10)
        self.assertEqual(self.milestones.peek("1", 0, 60, "x")[0], 30)


if __name__ == "__main__":
    unittest.main()
