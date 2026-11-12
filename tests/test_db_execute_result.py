import asyncio
import tempfile
import unittest
from pathlib import Path

from core.storage.db import Database, now
from core.storage.stores import MemoryStore


class DbExecuteResultTest(unittest.TestCase):
    """``execute`` 的返回值：插入给 rowid，删改给真实行数。"""

    def _run(self, body):
        async def wrapper():
            with tempfile.TemporaryDirectory() as tmp:
                db = Database(str(Path(tmp) / "t.db"))
                await db.open()
                try:
                    await body(db)
                finally:
                    await db.close()

        asyncio.run(wrapper())

    def test_delete_and_update_report_affected_rows_not_last_insert_id(self):
        async def body(db):
            for i in range(5):
                await db.kv_set(f"k{i}", i)
            first = await db.execute(
                "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?)", ("extra", "1", now())
            )
            self.assertGreater(first, 0)
            self.assertEqual(await db.execute("DELETE FROM kv WHERE key=?", ("missing",)), 0)
            self.assertEqual(await db.execute("UPDATE kv SET value='2' WHERE key='nope'"), 0)
            self.assertEqual(await db.execute("DELETE FROM kv WHERE key IN ('k0','k1')"), 2)

        self._run(body)

    def test_purging_with_nothing_expired_reports_zero(self):
        async def body(db):
            store = MemoryStore(db)
            for i in range(4):
                await store.add("bili", f"第 {i} 条", actor_id="u1")
            self.assertEqual(await store.purge_expired(), 0)
            self.assertEqual(await store.total_count(), 4)

        self._run(body)


if __name__ == "__main__":
    unittest.main()
