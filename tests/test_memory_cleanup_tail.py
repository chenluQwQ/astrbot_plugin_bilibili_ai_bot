"""B4d-6 记忆清理收尾：别人的动态 90 天删、普通直播事件半年删、旧备份文件 30 天删、每周 VACUUM 一次。"""
import asyncio
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  先装上假的 astrbot
from test_video_memory_forgetting import NOW, _Bot as _ForgettingBot, _Settings
from test_video_memory_lifecycle import _load_consolidation_module


class _BotSettings(_Settings):
    def get(self, key, default=None):
        return {"DEDE_USER_ID": "1"}.get(key, default)


class _Bot(_ForgettingBot):
    def __init__(self, memories):
        super().__init__(memories)
        self.settings = _BotSettings()


class _Db:
    is_open = True

    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    async def vacuum(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("database is locked")


def _dynamic(rpid, days_ago, author="", **extra):
    return {"rpid": rpid, "memory_type": "dynamic", "text": f"{rpid} 动态", "author_mid": author,
            "created_at": (NOW - timedelta(days=days_ago)).timestamp(), **extra}


class CleanupTailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_consolidation_module()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original_memory_file = self.module.MEMORY_FILE
        self.module.MEMORY_FILE = os.path.join(self.temp.name, "memory.json")
        self.addCleanup(setattr, self.module, "MEMORY_FILE", self.original_memory_file)

    def test_others_dynamics_go_after_ninety_days_with_a_backup(self):
        memories = [
            _dynamic("others_old", 91, author="777"),
            _dynamic("others_new", 80, author="777"),
            _dynamic("mine_old", 400, author="1"),
            _dynamic("posted_old", 400),  # Bot 自己发的那种，没有作者
            {"rpid": "chat", "memory_type": "chat", "author_mid": "777", "text": "闲聊",
             "created_at": (NOW - timedelta(days=400)).timestamp()},
        ]
        bot = _Bot(memories)
        self.assertEqual(self.module.ConsolidationEngine(bot)._forget_others_dynamics(), 1)
        self.assertEqual([m["rpid"] for m in bot.records], ["others_new", "mine_old", "posted_old", "chat"])
        (backup,) = [v for k, v in bot.files.items() if "memory_forgotten_backup_" in k]
        self.assertEqual([m["rpid"] for m in backup], ["others_old"])
        self.assertEqual(bot.removed_refs, {"others_old"})

    def test_ordinary_live_events_go_after_half_a_year_but_super_chats_stay(self):
        def live(rpid, days_ago, importance):
            return {"rpid": rpid, "memory_type": "live", "text": rpid, "importance": importance, "level": "today",
                    "created_at": (NOW - timedelta(days=days_ago)).timestamp()}

        memories = [live("danmaku_old", 181, 6), live("danmaku_new", 170, 6), live("sc_old", 400, 8)]
        bot = _Bot(memories)
        engine = self.module.ConsolidationEngine(bot)
        self.assertEqual(engine._forget_old_live_events(), 1)
        self.assertEqual([m["rpid"] for m in bot.records], ["danmaku_new", "sc_old"])
        self.assertEqual(engine._auto_promote_non_chat(), 2)
        self.assertEqual({m["level"] for m in bot.records}, {"recent"})
        self.assertEqual(bot.records[1]["importance"], 8)

    def test_old_backup_files_are_removed_by_the_date_in_their_name(self):
        def touch(name, mtime=None):
            path = os.path.join(self.temp.name, name)
            open(path, "w").close()
            if mtime:
                os.utime(path, (mtime, mtime))
            return name

        old = (NOW - timedelta(days=31)).strftime("%Y%m%d_%H%M%S")
        new = (NOW - timedelta(days=29)).strftime("%Y%m%d_%H%M%S")
        touch(f"memory_forgotten_backup_{old}.json")
        touch(f"memory_cleanup_backup_{old}.json")
        touch(f"user_profiles_cleared_backup_{old}.json")
        kept = {
            touch(f"memory_forgotten_backup_{new}.json"),
            touch("memory_forgotten_backup_weird.json"),  # 认不出日期，按修改时间（刚建）
            touch(f"memory_{old}.json"),  # 不是清算备份
            touch("memory.json"),
        }
        touch("memory_cleanup_backup_weird.json", mtime=time.time() - 40 * 86400)
        self.assertEqual(self.module.ConsolidationEngine(_Bot([]))._remove_old_backups(), 4)
        self.assertEqual(set(os.listdir(self.temp.name)), kept)

    def test_vacuum_runs_once_a_week_and_failure_is_not_fatal(self):
        bot = _Bot([])
        db = _Db()
        bot.layered_runtime = type("Layered", (), {"db": db})()
        engine = self.module.ConsolidationEngine(bot)
        state = {}
        self.assertTrue(asyncio.run(engine._vacuum_if_due(state)))
        self.assertFalse(asyncio.run(engine._vacuum_if_due(state)))
        state["last_vacuum"] = time.time() - 8 * 86400
        self.assertTrue(asyncio.run(engine._vacuum_if_due(state)))
        self.assertEqual(db.calls, 2)

        failing = _Db(fail=True)
        bot.layered_runtime = type("Layered", (), {"db": failing})()
        state = {}
        self.assertFalse(asyncio.run(engine._vacuum_if_due(state)))
        self.assertNotIn("last_vacuum", state)

    def test_no_database_means_no_vacuum(self):
        self.assertFalse(asyncio.run(self.module.ConsolidationEngine(_Bot([]))._vacuum_if_due({})))


if __name__ == "__main__":
    unittest.main()
