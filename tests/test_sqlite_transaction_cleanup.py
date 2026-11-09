import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from core.domain.dynamic_ideas import DynamicIdeas
from core.domain.memos import MemoRejected, Memos


class ExpireTransactionTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()

    def test_zero_row_expire_closes_transaction(self):
        for case in ("memo_pending", "memo_open_items", "dynamic_pending"):
            with self.subTest(case=case), closing(sqlite3.connect(":memory:")) as conn:
                if case.startswith("memo"):
                    store = Memos(conn)
                    store.add(kind="other", target="fresh", source="self", now=self.now)
                else:
                    store = DynamicIdeas(conn)
                    store.add(key="fresh", kind="分享", source="test", now=self.now)

                items = store.open_items(self.now) if case == "memo_open_items" else store.pending(self.now)

                self.assertEqual(len(items), 1)
                self.assertFalse(conn.in_transaction)

    def test_early_return_paths_close_transaction(self):
        for case in ("memo_duplicate", "memo_quota", "dynamic_duplicate"):
            with self.subTest(case=case), closing(sqlite3.connect(":memory:")) as conn:
                if case == "memo_duplicate":
                    store = Memos(conn)
                    memo_id, _ = store.add(kind="other", target="same", source="self", now=self.now)
                    duplicate_id, created = store.add(
                        kind="other", target="same", source="self", now=self.now
                    )
                    self.assertEqual(duplicate_id, memo_id)
                    self.assertFalse(created)
                elif case == "memo_quota":
                    store = Memos(conn)
                    for target in ("first", "second"):
                        store.add(
                            kind="other", target=target, source="comment", from_uid="user", now=self.now
                        )
                    with self.assertRaises(MemoRejected):
                        store.add(
                            kind="other", target="third", source="comment", from_uid="user", now=self.now
                        )
                else:
                    store = DynamicIdeas(conn)
                    store.add(key="same", kind="分享", source="test", now=self.now)
                    self.assertIsNone(
                        store.add(key="same", kind="分享", source="test", now=self.now)
                    )

                self.assertFalse(conn.in_transaction)

    def test_expire_sql_error_rolls_back(self):
        old = self.now - 4 * 24 * 3600
        for case, table in (("memo", "memos"), ("dynamic", "dynamic_ideas")):
            with self.subTest(case=case), closing(sqlite3.connect(":memory:")) as conn:
                if case == "memo":
                    store = Memos(conn)
                    store.add(kind="other", target="old", source="self", now=old)
                else:
                    store = DynamicIdeas(conn)
                    store.add(key="old", kind="分享", source="test", now=old)
                conn.execute(
                    f"CREATE TRIGGER fail_{table}_update BEFORE UPDATE ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'forced update failure'); END"
                )
                conn.commit()

                with self.assertRaises(sqlite3.Error):
                    store.pending(self.now)
                self.assertFalse(conn.in_transaction)

    def test_wal_zero_row_expire_allows_other_writer(self):
        for case in ("memo", "dynamic"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "store.db")
                with closing(sqlite3.connect(path)) as setup:
                    self.assertEqual(setup.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                    setup.execute("CREATE TABLE unrelated (value TEXT)")
                    setup.commit()

                with closing(sqlite3.connect(path)) as first, closing(
                    sqlite3.connect(path, timeout=0)
                ) as second:
                    if case == "memo":
                        store = Memos(first)
                        store.add(kind="other", target="fresh", source="self", now=self.now)
                    else:
                        store = DynamicIdeas(first)
                        store.add(key="fresh", kind="分享", source="test", now=self.now)

                    store.pending(self.now)
                    self.assertFalse(first.in_transaction)
                    second.execute("INSERT INTO unrelated VALUES (?)", (case,))
                    second.commit()
                    self.assertEqual(second.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
