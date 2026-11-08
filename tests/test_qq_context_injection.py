"""QQ 聊天前塞背景：只进本轮用户消息，不碰 system_prompt。"""
import sys
import types
import unittest
from unittest import mock

from core.infra.qq_chat import QQChat


class _TextPart:
    def __init__(self, text):
        self.text, self.temp = text, False

    def mark_as_temp(self):
        self.temp = True
        return self


class InjectContextTests(unittest.TestCase):
    def setUp(self):
        self.chat = QQChat.__new__(QQChat)

    def test_goes_into_temp_extra_part(self):
        message = types.ModuleType("astrbot.core.agent.message")
        message.TextPart = _TextPart
        req = types.SimpleNamespace(prompt="你好", system_prompt="人格", extra_user_content_parts=[])
        with mock.patch.dict(sys.modules, {"astrbot.core.agent.message": message}):
            self.assertTrue(self.chat._inject_context_block_before_user(req, "背景"))
        self.assertEqual([(p.text, p.temp) for p in req.extra_user_content_parts], [("背景", True)])
        self.assertEqual((req.prompt, req.system_prompt), ("你好", "人格"))

    def test_falls_back_to_prompt(self):
        req = types.SimpleNamespace(prompt="你好", system_prompt="人格")
        self.assertTrue(self.chat._inject_context_block_before_user(req, "背景"))
        self.assertEqual((req.prompt, req.system_prompt), ("背景\n\n你好", "人格"))

    def test_never_touches_system_prompt(self):
        req = types.SimpleNamespace(system_prompt="人格")
        self.assertFalse(self.chat._inject_context_block_before_user(req, "背景"))
        self.assertEqual(req.system_prompt, "人格")


if __name__ == "__main__":
    unittest.main()
