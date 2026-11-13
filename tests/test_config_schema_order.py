"""Regression tests for the administrator-facing configuration layout."""

import json
import re
import unittest
from pathlib import Path


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "_conf_schema.json"


class ConfigSchemaOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.keys = list(cls.schema)

    def assert_contiguous(self, expected):
        start = self.keys.index(expected[0])
        self.assertEqual(self.keys[start:start + len(expected)], expected)

    def test_llm_reliability_is_next_to_model_and_persona(self):
        self.assert_contiguous([
            "LLM_PROVIDER_ID",
            "USE_ASTRBOT_PERSONA",
            "CUSTOM_SYSTEM_PROMPT",
            "LLM_CIRCUIT_FAILURE_THRESHOLD",
            "LLM_CIRCUIT_COOLDOWN_SECONDS",
        ])

    def test_optional_proxy_is_in_system_section(self):
        self.assertEqual(self.schema["PROXY_URL"]["default"], "")
        self.assertEqual(self.schema["LLM_CIRCUIT_FAILURE_THRESHOLD"]["default"], 5)
        self.assertEqual(self.schema["LLM_CIRCUIT_COOLDOWN_SECONDS"]["default"], 120)
        self.assertIn("【系统】", self.schema["PROXY_URL"]["description"])

    def test_private_message_settings_are_one_contiguous_section(self):
        self.assert_contiguous([
            "ENABLE_PRIVATE_MESSAGES",
            "PRIVATE_MESSAGE_REPLY_SCOPE",
            "PRIVATE_MESSAGE_REPLY_WHITELIST_UIDS",
            "PRIVATE_MESSAGE_AUTO_REPLY",
            "ENABLE_PRIVATE_PSEUDO_STREAM",
            "CUSTOM_PRIVATE_MESSAGE_INSTRUCTION",
            "PRIVATE_MESSAGE_POLL_INTERVAL",
            "PRIVATE_MESSAGE_IDLE_POLL_INTERVAL",
            "PRIVATE_MESSAGE_ACTIVE_WINDOW",
            "PRIVATE_MESSAGE_MAX_PER_POLL",
            "PRIVATE_MESSAGE_MAX_MESSAGE_AGE",
            "PRIVATE_MESSAGE_AUTO_WATCH_VIDEO",
            "PRIVATE_MESSAGE_BILI_SEARCH_ENABLED",
            "PRIVATE_MESSAGE_BILI_SEARCH_LIMIT",
            "BILI_PRIVATE_SHARE_TOOL_ENABLED",
            "BILI_PRIVATE_SHARE_COOLDOWN",
        ])

    def test_share_parser_settings_are_one_contiguous_section(self):
        self.assert_contiguous([
            "ENABLE_BILI_SHARE_PARSE",
            "BILI_SHARE_PARSE_AUTO_TRIGGER_ENABLED",
            "BILI_SHARE_PARSE_MANUAL_TRIGGER_ENABLED",
            "BILI_SHARE_PARSE_LLM_TRIGGER_ENABLED",
            "BILI_SHARE_PENDING_MAX_AGE",
            "BILI_SHARE_PARSE_SEND_VIDEO",
            "BILI_SHARE_PARSE_SEGMENT_SECONDS",
            "BILI_SHARE_PARSE_MAX_SEGMENTS",
            "BILI_SHARE_PARSE_MAX_VIDEO_MB",
            "BILI_SHARE_PARSE_VIDEO_MAX_HEIGHT",
            "BILI_SHARE_PARSE_COOLDOWN",
            "BILI_SHARE_PARSE_AUTO_WATCH",
        ])

    def test_video_memory_switch_precedes_lifecycle_windows(self):
        self.assert_contiguous([
            "ENABLE_VIDEO_LONG_TERM_MEMORY",
            "VIDEO_MEMORY_DETAIL_DAYS",
            "VIDEO_MEMORY_FADE_DAYS",
            "VIDEO_LOW_SCORE_FORGET_BELOW",
            "VIDEO_LOW_SCORE_FORGET_DAYS",
            "MEMORY_LOW_SCORE_KEEP_DAYS",
            "MEMORY_LONG_TERM_KEEP_DAYS",
        ])
        self.assertTrue(self.schema["ENABLE_VIDEO_LONG_TERM_MEMORY"]["default"])
        self.assertEqual(self.schema["VIDEO_MEMORY_DETAIL_DAYS"]["default"], 15)
        self.assertEqual(self.schema["VIDEO_MEMORY_FADE_DAYS"]["default"], 180)
        self.assertEqual(self.schema["VIDEO_LOW_SCORE_FORGET_BELOW"]["default"], 5)
        self.assertEqual(self.schema["VIDEO_LOW_SCORE_FORGET_DAYS"]["default"], 15)

    def test_sections_follow_webui_categories(self):
        # 管理页「全部配置」按 ALL_CONFIG_GROUPS 的顺序分组；schema 也按同样的分类、同样的顺序排，
        # 每个分类的项连在一起（AstrBot 自带的配置页是平铺的，顺序就看这里）。
        app_js = (SCHEMA_PATH.parent / "pages" / "bilibot" / "app.js").read_text(encoding="utf-8")
        groups = re.findall(r'"([^"]+)"', re.search(r"const ALL_CONFIG_GROUPS = \[(.*?)\];", app_js).group(1))
        seen = []
        for key in self.keys:
            field = self.schema[key]
            if field.get("deprecated"):
                continue
            tag = re.match(r"【([^】]+)】", field["description"]).group(1)
            self.assertIn(tag, groups, key)
            if not seen or seen[-1] != tag:
                self.assertNotIn(tag, seen, f"{key}: {tag} 被别的分类隔开了")
                seen.append(tag)
        self.assertEqual(seen, [group for group in groups if group in seen])
        self.assertEqual(seen[:3], ["账号与主人", "人设与模型", "评论回复"])

    def test_hidden_legacy_keys_come_last(self):
        hidden = [key for key in self.keys if self.schema[key].get("invisible")]
        self.assertEqual(self.keys[-len(hidden):], hidden)

    def test_no_key_is_removed_or_renamed(self):
        # AstrBot drops config keys that are missing from the schema when it loads
        # the plugin config, so removing or renaming a key wipes the user's value.
        # Retire a key by marking it deprecated and invisible instead.
        frozen = (Path(__file__).with_name("schema_keys.txt")).read_text(encoding="utf-8").split()
        missing = sorted(set(frozen) - set(self.keys))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
