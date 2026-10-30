"""Keep the WebUI pages in step with the configuration schema."""

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
APP_JS = (ROOT / "pages" / "bilibot" / "app.js").read_text(encoding="utf-8")


def _block(start, end):
    return APP_JS[APP_JS.index(start):APP_JS.index(end)]


def _capability_keys():
    block = _block("const AUTONOMY_CAPABILITIES", "function activityLabel")
    found = {}
    for match in re.finditer(r'\{ id: "([\w-]+)".*?toggle: "(\w+)".*?keys: \[(.*?)\] \}', block):
        found[match.group(1)] = (match.group(2), re.findall(r'"(\w+)"', match.group(3)))
    return found


class WebuiConfigCoverageTests(unittest.TestCase):
    def test_page_keys_exist(self):
        block = _block("const PAGE_KEYS", "const SCHEDULE_REGEN_KEYS")
        for key in re.findall(r'"([A-Z][A-Z0-9_]+)"', block):
            self.assertIn(key, SCHEMA, key)

    def test_capability_drawers_only_use_live_keys(self):
        for capability, (toggle, keys) in _capability_keys().items():
            for key in [toggle, *keys]:
                self.assertIn(key, SCHEMA, f"{capability}: {key}")
                self.assertFalse(SCHEMA[key].get("deprecated"), f"{capability}: {key}")

    def test_schedule_settings_have_a_drawer(self):
        drawers = _capability_keys()
        self.assertIn("SPECIAL_FOLLOW_TIMES_COUNT", drawers["special-follow"][1])
        self.assertIn("DYNAMIC_WATCH_TIMES_COUNT", drawers["dynamic-watch"][1])
        for key in ("DYNAMIC_TIMES_COUNT", "DYNAMIC_DAILY_COUNT", "DYNAMIC_POST_CHANCE", "DYNAMIC_MIN_INTERVAL_DAYS",
                    "DYNAMIC_FROM_VIDEO", "DYNAMIC_WEB_SEARCH_MAX", "DYNAMIC_PROMPT"):
            self.assertIn(key, drawers["dynamic"][1], key)

    def test_comment_sync_toggle_lives_in_the_proactive_drawer(self):
        drawers = _capability_keys()
        self.assertIn("COMMENT_SYNC_DYNAMIC", drawers["proactive"][1])
        page_keys = _block("const PAGE_KEYS", "const SCHEDULE_REGEN_KEYS")
        self.assertIn('"COMMENT_SYNC_DYNAMIC"', page_keys)
        self.assertNotIn('"COMMENT_SYNC_DYNAMIC"', _block("interaction: [", "autonomy: ["))
        self.assertNotIn("COMMENT_SYNC_DYNAMIC", _block("function renderInteraction", "const EVENT_STYLES"))

    def test_conditions_point_at_live_keys(self):
        for key, field in SCHEMA.items():
            if field.get("deprecated"):
                continue
            for dep in field.get("condition") or {}:
                self.assertIn(dep, SCHEMA, f"{key} depends on unknown {dep}")
                self.assertFalse(SCHEMA[dep].get("deprecated"), f"{key} depends on deprecated {dep}")

    def test_legacy_special_follow_keys_are_hidden(self):
        for key in ("SPECIAL_FOLLOW_MODE", "SPECIAL_FOLLOW_FIXED_TIMES"):
            self.assertTrue(SCHEMA[key].get("deprecated"), key)
            self.assertTrue(SCHEMA[key].get("invisible"), key)

    def test_main_webui_honours_schema_conditions(self):
        self.assertIn("function fieldConditionMet", APP_JS)
        self.assertIn('data-field-key="${key}"', APP_JS)


if __name__ == "__main__":
    unittest.main()
