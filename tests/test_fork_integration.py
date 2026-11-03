"""发布合并的边界测试：全部使用虚构账号与模拟接口。"""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.content_protocol import ContentProtocolError, parse_proactive_comment
from core import livingmemory_bridge, unified_knowledge, umo
from test_autonomous_ranges import ProactiveProbe, ReplyProbe


class PublicContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_search_never_uses_private_results_or_session_scope(self):
        search = AsyncMock(return_value={
            "status": "ok", "memory": [{"content": "私聊内容不得进入公开回复"}],
            "knowledge": [
                {"source": "memory", "kb_id": "kb", "doc_id": "d", "chunk_id": "c", "content": "私密"},
                {"source": "knowledge", "kb_id": "kb", "doc_id": "d", "chunk_id": "c", "content": "公开角色资料"},
            ],
        })
        bot = SimpleNamespace(context=object())
        with patch.object(livingmemory_bridge, "_livingmemory", return_value=SimpleNamespace(search_context=search)), \
                patch.object(livingmemory_bridge, "target_umo", return_value="demo:FriendMessage:10001"), \
                patch.object(livingmemory_bridge, "_resolve_persona_id", AsyncMock(return_value="demo-persona")):
            result = await unified_knowledge.public_knowledge_context(bot, "角色")
        self.assertIn("公开角色资料", result)
        self.assertNotIn("私密", result)
        self.assertNotIn("私聊内容不得", result)
        self.assertEqual(search.await_args.kwargs["purpose"], "public_comment")
        self.assertNotIn("umo", search.await_args.kwargs)

    async def test_missing_authorization_service_returns_empty(self):
        bot = SimpleNamespace(context=object())
        with patch.object(livingmemory_bridge, "_livingmemory", return_value=None):
            self.assertEqual(await unified_knowledge.public_knowledge_context(bot, "角色"), "")


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalizes_legacy_umo_and_checks_false_delivery(self):
        inst = SimpleNamespace(meta=lambda: SimpleNamespace(id="demo", type="onebot", name="test"))
        context = SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[inst]),
            send_message=AsyncMock(return_value=False),
        )
        self.assertEqual(umo.normalize_umo(context, "FriendMessage:10001"), "demo:FriendMessage:10001")
        delivered, _ = await umo.send_chain_to_umo(context, "FriendMessage:10001", object())
        self.assertFalse(delivered)
        self.assertEqual(context.send_message.await_args.args[0], "demo:FriendMessage:10001")

    async def test_changed_owner_cannot_receive_old_group_recommendation(self):
        bot = ProactiveProbe({"OWNER_QQ_UMO": "demo:FriendMessage:10002"})
        bot._load_json = lambda *_: {
            "umo": "demo:GroupMessage:20001", "owner_qq": "10001", "updated_at": 99999999999,
        }
        bot.context = SimpleNamespace(send_message=AsyncMock())
        self.assertFalse(await bot._send_owner_recommend_qq_group("文本", "BV_TEST"))
        bot.context.send_message.assert_not_awaited()

    async def check_reply_state(self, sent):
        bot = ReplyProbe({})
        saved = {}
        bot._load_json = lambda path, default: saved.get(path, default)
        bot._save_json = lambda path, value: saved.__setitem__(path, value)
        bot._apply_reply_result = AsyncMock(return_value=sent)
        bot._check_abuse_alert = AsyncMock()
        bot._is_owner = lambda _: False
        comment = {"mid": "10001", "username": "测试", "content": "文本", "oid": 1,
                   "rpid": "101", "comment_type": 1, "thread_id": "thread"}
        await bot._finish_comment_reply(comment, {"decision": "reply", "reply": "回复"})
        return saved

    async def test_explicit_failure_schedules_retry(self):
        saved = await self.check_reply_state(None)
        self.assertTrue(any(isinstance(value, dict) and "101" in value for value in saved.values()))

    async def test_unknown_or_ignored_result_does_not_schedule_retry(self):
        self.assertEqual(await self.check_reply_state(False), {})


class TextSafetyTests(unittest.TestCase):
    def test_overlong_valid_comment_is_trimmed(self):
        result = parse_proactive_comment(json.dumps({"decision": "comment", "text": "长" * 100}))
        self.assertEqual(len(result["text"]), 80)

    def test_internal_markup_after_limit_is_still_rejected(self):
        with self.assertRaises(ContentProtocolError):
            parse_proactive_comment(json.dumps({"decision": "comment", "text": "长" * 100 + "<think>秘密"}))
