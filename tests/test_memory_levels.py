"""长期只放要点：6 分以上的聊天和总结才升长期；不够格的留在近期，按以前的时间老化，不多删任何东西。"""

import unittest
from datetime import datetime, timedelta

from test_video_memory_lifecycle import _Bot, _load_consolidation_module


class _LevelBot(_Bot):
    @staticmethod
    def _is_derived_memory(memory):
        return bool(memory.get("derived_from_rpids") or memory.get("summary_kind"))


def _ago(days):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")


def _memory(rpid, memory_type, importance, level="recent", days=20, **extra):
    return {"rpid": rpid, "memory_type": memory_type, "importance": importance,
            "level": level, "promoted_at": _ago(days), "time": _ago(days), **extra}


class LongTermLevelTests(unittest.TestCase):
    def setUp(self):
        self.engine_cls = _load_consolidation_module().ConsolidationEngine

    def run_promote(self, memories):
        self.engine_cls(_LevelBot(memories))._promote_recent_to_longterm()
        return {m["rpid"]: m["level"] for m in memories}

    def test_only_important_chats_and_summaries_go_long_term(self):
        levels = self.run_promote([
            _memory("chat6", "chat", 6),
            _memory("chat5", "chat", 5),
            _memory("young", "chat", 9, days=3),
            _memory("summary", "user_summary", 7),
            _memory("dynamic", "dynamic", 8),
            _memory("live", "live", 7),
        ])
        self.assertEqual(levels, {
            "chat6": "long_term", "chat5": "recent", "young": "recent",
            "summary": "long_term", "dynamic": "recent", "live": "recent",
        })

    def test_old_unfit_long_term_goes_back_to_recent_without_deleting(self):
        memories = [
            _memory("low", "chat", 4, level="long_term"),
            _memory("bangumi", "bangumi", 7, level="long_term"),
            _memory("keep", "chat", 8, level="long_term"),
            _memory("derived", "chat", 3, level="long_term", derived_from_rpids=["a"]),
        ]
        levels = self.run_promote(memories)
        self.assertEqual(levels, {"low": "recent", "bangumi": "recent", "keep": "long_term", "derived": "long_term"})
        self.assertEqual(len(memories), 4)

    def test_recent_ages_on_the_old_schedule(self):
        line = 14 + 180
        memories = [
            _memory("recent_old", "chat", 5, days=line + 1),
            _memory("recent_new", "chat", 5, days=line - 1),
            _memory("long_old", "chat", 7, level="long_term", days=181),
            _memory("video", "video", 5, days=line + 1),
        ]
        self.engine_cls(_LevelBot(memories))._mark_aged()
        self.assertEqual({m["rpid"] for m in memories if m.get("aged")}, {"recent_old", "long_old"})


if __name__ == "__main__":
    unittest.main()
