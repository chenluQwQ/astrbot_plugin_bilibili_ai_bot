"""Regression tests for the administrator-facing configuration layout."""

import json
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
        self.assertEqual(self.schema["LLM_CIRCUIT_FAILURE_THRESHOLD"]["default"], 5)
        self.assertEqual(self.schema["LLM_CIRCUIT_COOLDOWN_SECONDS"]["default"], 120)

    def test_private_message_settings_are_one_contiguous_section(self):
        self.assert_contiguous([
            "ENABLE_PRIVATE_MESSAGES",
            "PRIVATE_MESSAGE_REPLY_SCOPE",
            "PRIVATE_MESSAGE_REPLY_WHITELIST_UIDS",
            "PRIVATE_MESSAGE_AUTO_REPLY",
            "PRIVATE_MESSAGE_POLL_INTERVAL",
            "PRIVATE_MESSAGE_IDLE_POLL_INTERVAL",
            "PRIVATE_MESSAGE_ACTIVE_WINDOW",
            "PRIVATE_MESSAGE_MAX_PER_POLL",
            "PRIVATE_MESSAGE_MAX_MESSAGE_AGE",
            "INTEREST_APPLY_TO_PRIVATE",
            "PRIVATE_MESSAGE_AUTO_WATCH_VIDEO",
            "PRIVATE_MESSAGE_BILI_SEARCH_ENABLED",
            "PRIVATE_MESSAGE_BILI_SEARCH_LIMIT",
            "BILI_PRIVATE_SHARE_TOOL_ENABLED",
            "BILI_PRIVATE_SHARE_COOLDOWN",
            "CUSTOM_PRIVATE_MESSAGE_INSTRUCTION",
            "PRIVATE_MESSAGE_AUTO_BLOCK",
            "PRIVATE_MESSAGE_BLOCK_WHITELIST_UIDS",
            "PRIVATE_MESSAGE_TRUSTED_DOMAINS",
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
        ])

    def test_video_memory_switch_precedes_lifecycle_windows(self):
        self.assert_contiguous([
            "VIDEO_VISUAL_ANALYSIS_POLICY",
            "ENABLE_VIDEO_LONG_TERM_MEMORY",
            "VIDEO_MEMORY_DETAIL_DAYS",
            "VIDEO_MEMORY_FADE_DAYS",
            "VIDEO_SEGMENT_MINUTES",
            "VIDEO_SEGMENT_MAX_COUNT",
        ])
        self.assertTrue(self.schema["ENABLE_VIDEO_LONG_TERM_MEMORY"]["default"])
        self.assertEqual(self.schema["VIDEO_MEMORY_DETAIL_DAYS"]["default"], 15)
        self.assertEqual(self.schema["VIDEO_MEMORY_FADE_DAYS"]["default"], 90)


if __name__ == "__main__":
    unittest.main()
