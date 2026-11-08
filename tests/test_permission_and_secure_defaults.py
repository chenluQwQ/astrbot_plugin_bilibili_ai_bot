from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CommandPermissionTests(unittest.TestCase):
    def test_sensitive_commands_require_admin(self):
        expected = {
            "cmd_status",
            "cmd_schedule",
            "cmd_memory",
            "cmd_memory_integration",
            "cmd_affection",
            "cmd_interest",
            "cmd_personality",
            "cmd_blocklist",
            "cmd_daily_log",
            "cmd_bangumi_memory",
            "cmd_permanent_memory",
            "cmd_migrate_memory",
            "cmd_personality_rollback",
        }
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        methods = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing = []
        for name in sorted(expected):
            node = methods.get(name)
            decorators = (
                [ast.unparse(item) for item in node.decorator_list] if node else []
            )
            if not any(
                item == "filter.permission_type(filter.PermissionType.ADMIN)"
                for item in decorators
            ):
                missing.append(name)
        self.assertEqual([], missing, f"缺少 ADMIN 装饰器: {missing}")

    def test_public_and_self_service_commands_stay_available(self):
        expected = {"cmd_help", "cmd_bili_parse", "cmd_bind", "cmd_unbind"}
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        methods = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue(expected.issubset(methods))


class SecureDefaultTests(unittest.TestCase):
    def test_external_listening_and_automation_default_off(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in (
            "ENABLE_PRIVATE_MESSAGES",
            "PRIVATE_MESSAGE_AUTO_REPLY",
            "ENABLE_DYNAMIC",
            "ENABLE_LLM_TOOLS",
        ):
            self.assertIn(key, schema)
            self.assertIs(schema[key].get("default"), False, key)

    def test_tool_authorization_switches_default_off(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in (
            "SECURITY_ALLOW_NON_ADMIN_READ_TOOLS",
            "SECURITY_ENABLE_PRIVATE_READ_TOOLS",
            "SECURITY_ENABLE_WRITE_TOOLS",
        ):
            self.assertIn(key, schema)
            self.assertIs(schema[key].get("default"), False, key)
            self.assertEqual(
                {"ENABLE_LLM_TOOLS": True}, schema[key].get("condition"), key
            )

    def test_legacy_webui_fallbacks_match_secure_schema(self):
        text = (ROOT / "pages/bilibot/app.js").read_text(encoding="utf-8")
        for key in (
            "ENABLE_PRIVATE_MESSAGES",
            "PRIVATE_MESSAGE_AUTO_REPLY",
            "ENABLE_DYNAMIC",
            "ENABLE_LLM_TOOLS",
            "SECURITY_ALLOW_NON_ADMIN_READ_TOOLS",
            "SECURITY_ENABLE_PRIVATE_READ_TOOLS",
            "SECURITY_ENABLE_WRITE_TOOLS",
        ):
            match = re.search(
                rf"^\s*{re.escape(key)}:\s*\[.*$", text, re.MULTILINE
            )
            self.assertIsNotNone(match, key)
            self.assertRegex(match.group(0), r'"bool",\s*false\]')

    def test_runtime_fallbacks_match_secure_schema(self):
        # Code that reads through core.settings gets the schema default, which the
        # *_default_off tests above already pin to off. Code that still
        # reads the raw config must fall back to off as well.
        from core.settings import Settings

        for key in ("ENABLE_LLM_TOOLS", "PRIVATE_MESSAGE_AUTO_REPLY"):
            self.assertFalse(Settings({}).bool(key), key)
        for path in (*(ROOT / "core/tools").glob("*.py"), *(ROOT / "core/features").glob("*private*.py"), ROOT / "main.py"):
            text = path.read_text(encoding="utf-8")
            for key in ("ENABLE_LLM_TOOLS", "PRIVATE_MESSAGE_AUTO_REPLY"):
                for match in re.finditer(rf"config\.get\(\s*[\"']{key}[\"']\s*(,\s*([^)]*))?\)", text):
                    self.assertEqual((match.group(2) or "").strip(), "False", f"{path.name}: {match.group(0)}")


if __name__ == "__main__":
    unittest.main()
