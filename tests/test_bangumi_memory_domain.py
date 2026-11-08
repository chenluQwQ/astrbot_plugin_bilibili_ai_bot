"""番剧记忆：感想只留最近 50 集，但看过的集不能忘。"""
import tempfile
import unittest
from pathlib import Path

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from core.domain.bangumi_memory import BangumiMemory
from core.storage.json_file import JsonFile


class BangumiMemoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.memory = BangumiMemory(JsonFile(Path(folder.name) / "bangumi_memory.json"))

    def _watch(self, number):
        self.memory.save_episode(7, "长番", {"ep_id": 100 + number, "ep_index": str(number)}, 7, "开心", "", "", "")

    def test_long_season_does_not_forget_early_episodes(self):
        for number in range(1, 56):
            self._watch(number)
        record = self.memory.all()["7"]
        self.assertEqual(len(record["episodes"]), 50)
        self.assertEqual(record["total_watched"], 55)
        self.assertEqual(self.memory.watched_ep_ids(7), {100 + n for n in range(1, 56)})

    def test_old_records_without_the_list_still_work(self):
        self.memory._file.write({"7": {"episodes": [{"ep_id": 101, "ep_index": "1"}]}})
        self.assertEqual(self.memory.watched_ep_ids(7), {101})
        self._watch(2)
        self.assertEqual(self.memory.watched_ep_ids(7), {101, 102})

    def test_rewatching_does_not_count_twice(self):
        self._watch(1)
        self._watch(1)
        self.assertEqual(self.memory.all()["7"]["total_watched"], 1)

    def test_mark_completed_only_for_known_seasons(self):
        self.assertFalse(self.memory.mark_completed(7))
        self.assertEqual(self.memory.all(), {})
        self._watch(1)
        self.assertTrue(self.memory.mark_completed(7))
        self.assertTrue(self.memory.all()["7"]["completed"])


if __name__ == "__main__":
    unittest.main()
