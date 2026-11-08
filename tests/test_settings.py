"""core.settings: one default per key, taken from _conf_schema.json."""

import json
import re
import unittest
from pathlib import Path

from core.settings import CODE_ONLY_DEFAULTS, LEGACY_KEYS, Settings, schema_defaults, with_schema_defaults


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
CONFIG_READ = re.compile(r"config\.get\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']")


class SettingsTests(unittest.TestCase):
    def test_defaults_come_from_schema(self):
        settings = Settings({})
        self.assertEqual(settings.get("POLL_INTERVAL"), SCHEMA["POLL_INTERVAL"]["default"])
        self.assertEqual(settings.get("CUSTOM_SYSTEM_PROMPT"), SCHEMA["CUSTOM_SYSTEM_PROMPT"]["default"])

    def test_none_falls_back_to_default(self):
        settings = Settings({"POLL_INTERVAL": None})
        self.assertEqual(settings.get("POLL_INTERVAL"), SCHEMA["POLL_INTERVAL"]["default"])

    def test_unknown_key_is_an_error(self):
        with self.assertRaises(KeyError):
            Settings({}).get("NO_SUCH_KEY")

    def test_reads_the_live_config(self):
        config = {"POLL_INTERVAL": 10}
        settings = Settings(config)
        config["POLL_INTERVAL"] = 99
        self.assertEqual(settings.int("POLL_INTERVAL"), 99)

    def test_int_falls_back_and_clamps(self):
        settings = Settings({"POLL_INTERVAL": "abc", "REPLY_COOLDOWN": -5})
        self.assertEqual(settings.int("POLL_INTERVAL"), SCHEMA["POLL_INTERVAL"]["default"])
        self.assertEqual(settings.int("REPLY_COOLDOWN", minimum=0), 0)

    def test_bool_accepts_strings(self):
        settings = Settings({"ENABLE_REPLY": "false", "ENABLE_DYNAMIC": "on"})
        self.assertFalse(settings.bool("ENABLE_REPLY"))
        self.assertTrue(settings.bool("ENABLE_DYNAMIC"))

    def test_list_drops_blank_items(self):
        settings = Settings({"REPLY_ALWAYS_UIDS": [" 1 ", "", 2], "BLOCK_WHITELIST_UIDS": "3\n\n4"})
        self.assertEqual(settings.list("REPLY_ALWAYS_UIDS"), ["1", "2"])
        self.assertEqual(settings.list("BLOCK_WHITELIST_UIDS"), ["3", "4"])

    def test_code_only_keys_are_not_in_schema(self):
        self.assertFalse(set(CODE_ONLY_DEFAULTS) & set(SCHEMA))

    def test_with_schema_defaults_is_a_fresh_copy(self):
        first = with_schema_defaults({"POLL_INTERVAL": 1})
        first["PROACTIVE_VIDEO_POOLS"].append("x")
        second = with_schema_defaults()
        self.assertEqual(first["POLL_INTERVAL"], 1)
        self.assertNotIn("x", second["PROACTIVE_VIDEO_POOLS"])

    def test_legacy_key_applies_only_while_new_key_is_default(self):
        default = SCHEMA["AUTONOMOUS_REPLY_DAILY_MAX"]["default"]
        self.assertEqual(Settings({"AUTONOMOUS_REPLY_DAILY_LIMIT": 12}).int("AUTONOMOUS_REPLY_DAILY_MAX"), 12)
        self.assertEqual(
            Settings({"AUTONOMOUS_REPLY_DAILY_MAX": 50, "AUTONOMOUS_REPLY_DAILY_LIMIT": 12})
            .int("AUTONOMOUS_REPLY_DAILY_MAX"),
            50,
        )
        self.assertEqual(Settings({}).int("AUTONOMOUS_REPLY_DAILY_MAX"), default)

    def test_legacy_keys_are_hidden_schema_keys(self):
        for new_key, old_key in LEGACY_KEYS.items():
            self.assertIn(new_key, SCHEMA)
            self.assertTrue(SCHEMA[old_key].get("invisible"), old_key)
            self.assertEqual(SCHEMA[old_key]["default"], SCHEMA[new_key]["default"], old_key)

    def test_every_key_the_code_reads_has_a_default(self):
        defaults = schema_defaults()
        sources = [ROOT / "main.py", *sorted((ROOT / "core").rglob("*.py"))]
        missing = {}
        for path in sources:
            for key in CONFIG_READ.findall(path.read_text(encoding="utf-8")):
                if key not in defaults:
                    missing.setdefault(key, path.relative_to(ROOT).as_posix())
        self.assertEqual(missing, {})


if __name__ == "__main__":
    unittest.main()
