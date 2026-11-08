"""Preferences live in sqlite; preference_state.json is the copy written after each sync."""

from base_parts import BaseParts
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.config import PREFERENCE_STATE_FILE
from core.domain.preferences import Preferences
from core.layered_runtime import LayeredRuntime

SAMPLE = json.loads((Path(__file__).parent / "fixtures/legacy_data/preference_state.json").read_text(encoding="utf-8"))


class _Host(BaseParts):
    def __init__(self, layered=None, files=None):
        self.layered_runtime = layered
        self.files = dict(files or {})

    def _load_json(self, path, default=None):
        return self.files.get(path, default)

    def _save_json(self, path, value):
        self.files[path] = json.loads(json.dumps(value, ensure_ascii=False))


class PreferencesWithoutDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_copy_is_read_when_database_is_closed(self):
        prefs = Preferences(_Host(files={PREFERENCE_STATE_FILE: SAMPLE}))
        self.assertFalse(prefs.is_live())
        self.assertEqual(prefs.cached(), SAMPLE["current"])
        self.assertEqual(await prefs.current(limit=20), [])
        self.assertEqual(await prefs.sync([("BV1:x", [{"type": "theme", "value": "灯塔", "polarity": "like", "strength": 1}], 0.0)]), SAMPLE)

    async def test_broken_copy_reads_as_empty(self):
        prefs = Preferences(_Host(files={PREFERENCE_STATE_FILE: ["坏"]}))
        self.assertEqual(prefs.cached(), [])
        self.assertEqual(prefs.cached_state(), {"current": [], "changes": []})


class PreferencesWithDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.layers = LayeredRuntime({}, Path(self.temp_dir.name) / "bilibot.sqlite3")
        await self.layers.open()

    async def asyncTearDown(self):
        await self.layers.close()
        self.temp_dir.cleanup()

    async def test_sync_records_evidence_and_writes_the_copy(self):
        host = _Host(self.layers, files={PREFERENCE_STATE_FILE: SAMPLE})
        prefs = Preferences(host)
        at = datetime.now().timestamp()
        signal = [{"type": "theme", "value": "灯塔", "polarity": "like", "strength": 0.9}]
        state = await prefs.sync([("BV1:a", signal, at), ("BV2:b", signal, at - 60)])
        values = [item["value"] for item in state["current"]]
        self.assertIn("灯塔", values)
        self.assertEqual(host.files[PREFERENCE_STATE_FILE]["current"], state["current"])
        self.assertEqual([item["value"] for item in await prefs.current(limit=5)], values[:5])


if __name__ == "__main__":
    unittest.main()
