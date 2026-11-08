"""B5b 低分 UP 跳过名单：刷到够多又总低分的进名单、到期出来、特别关注和主人不进、只数自己刷到的。"""
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.domain.up_skip import SKIP_DAYS, UpSkip, low_score_ups
from core.features import proactive_browse
from core.features.proactive_browse import ProactiveBrowse
from core.settings import Settings

NOW = datetime(2026, 11, 7, 12, 0).timestamp()


def watched(uid, score, days_ago=1, source="search", bvid=None, name="某UP"):
    return {"time": (datetime.fromtimestamp(NOW) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M"),
            "up_mid": uid, "up_name": name, "score": score, "source": source,
            "bvid": bvid or f"BV{uid}{score}{days_ago}{source}"}


class _File:
    def __init__(self, data=None):
        self.data, self.writes = data if data is not None else {}, 0

    def read(self, default=None):
        return self.data

    def update(self, change, default=None):
        self.writes += 1
        self.data = change(self.data)
        return self.data


class LowScoreRuleTests(unittest.TestCase):
    def test_three_low_videos_from_own_browsing_in_two_weeks(self):
        log = [watched("7", 3), watched("7", 4, days_ago=2), watched("7", 5, days_ago=3, source="pool")]
        self.assertEqual(low_score_ups(log, now=NOW), {"7": {"name": "某UP", "count": 3, "average": 4.0}})

    def test_what_does_not_count(self):
        log = [
            watched("7", 3), watched("7", 3, days_ago=2),
            watched("7", 2, days_ago=20),                      # 两周以前
            watched("7", 2, source="following"),               # 关注来的
            watched("7", 0, days_ago=3),                       # 评价失败
            watched("7", 3, bvid="BV_SAME"), watched("7", 3, bvid="BV_SAME"),  # 同一个视频只算一次
            watched("8", 3), watched("8", 4), watched("8", 6),  # 平均 4.3
        ]
        found = low_score_ups(log, now=NOW)
        self.assertEqual(found["7"]["count"], 3)
        self.assertNotIn("8", found)
        self.assertEqual(low_score_ups(log, now=NOW, exempt={"7"}), {})


class UpSkipTests(unittest.TestCase):
    LOG = [watched("7", 3), watched("7", 3, days_ago=2), watched("7", 3, days_ago=3)]

    def test_added_once_then_expires_and_unchanged_list_is_not_rewritten(self):
        file = _File()
        skip = UpSkip(file)
        self.assertEqual([item["uid"] for item in skip.review(self.LOG, now=NOW)], ["7"])
        self.assertEqual(skip.review(self.LOG, now=NOW + 60), [])
        self.assertEqual(file.writes, 1)
        self.assertEqual(set(skip.entries(now=NOW + 60)), {"7"})
        later = NOW + SKIP_DAYS * 86400 + 1
        self.assertEqual(skip.entries(now=later), {})
        skip.review([], now=later)
        self.assertEqual(file.data, {})

    def test_exempt_people_are_taken_off(self):
        file = _File({"7": {"until": NOW + 999, "name": "某UP"}})
        UpSkip(file).review([], exempt={"7"}, now=NOW)
        self.assertEqual(file.data, {})


class BrowseSkipTests(unittest.TestCase):
    def test_special_follows_and_owner_are_exempt_and_errors_do_not_stop_browsing(self):
        skip = MagicMock()
        skip.review.return_value = []
        skip.entries.return_value = {"9": {}}
        host = types.SimpleNamespace(up_skip=skip, settings=Settings({"PROACTIVE_FOLLOW_UIDS": ["100"], "OWNER_MID": "777"}))
        self.assertEqual(ProactiveBrowse(host)._skipped_ups([]), {"9"})
        self.assertEqual(set(skip.review.call_args.kwargs["exempt"]), {"100", "777"})
        skip.review.side_effect = RuntimeError("坏了")
        with patch.object(proactive_browse, "logger"):
            self.assertEqual(ProactiveBrowse(host)._skipped_ups([]), set())


if __name__ == "__main__":
    unittest.main()
