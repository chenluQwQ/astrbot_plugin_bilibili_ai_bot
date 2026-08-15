"""
测试事件适配层
"""

import unittest
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# 创建 astrbot stub
class MessageType:
    GROUP_MESSAGE = "group"
    FRIEND_MESSAGE = "friend"


class AstrMessageEvent:
    def __init__(self):
        self.message_str = ""
        self.message_type = None
        self.session_id = ""
        self.message_id = ""
        self.raw_message = ""
        self.unified_msg_origin = ""


from core.storage import Database  # noqa: E402
from core.event_adapter import EventAdapter  # noqa: E402


class TestEventAdapter(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.db = Database(self.db_path)
        asyncio.run(self.db.open())
        self.adapter = EventAdapter(self.db)

    def tearDown(self):
        asyncio.run(self.db.close())
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_ingest_and_claim(self):
        """测试事件入库和 claim 流程"""

        async def run():
            # 模拟一个评论事件
            event = AstrMessageEvent()
            event.message_str = "测试评论"
            event.message_type = MessageType.GROUP_MESSAGE
            event.session_id = "test_session_123"
            event.message_id = "msg_456"
            event.raw_message = "测试评论"
            event.unified_msg_origin = "bilibili"

            event_id = await self.adapter.ingest_message_event(
                event, platform_id="bili:comment:789", source_type="comment"
            )
            self.assertTrue(event_id.startswith("evt_"))

            # Claim 事件
            claimed = await self.adapter.claim_event(["comment"])
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["event_id"], event_id)
            self.assertEqual(claimed["status"], "claimed")
            self.assertEqual(claimed["content"], "测试评论")

            # 再次 claim 应该返回 None（已被 claim）
            claimed_again = await self.adapter.claim_event(["comment"])
            self.assertIsNone(claimed_again)

            # 完成事件
            await self.adapter.complete_event(event_id, "处理成功")

            # 验证状态
            result = await self.adapter.db.fetch_one(
                "SELECT state, draft FROM events WHERE id = ?", (int(event_id[4:]),)
            )
            self.assertEqual(result["state"], "sent")
            self.assertEqual(result["draft"], "处理成功")

        asyncio.run(run())

    def test_claim_timeout_recovery(self):
        """测试超时事件可被重新 claim"""

        async def run():
            event = AstrMessageEvent()
            event.message_str = "超时测试"
            event.message_type = MessageType.GROUP_MESSAGE
            event.session_id = "timeout_session"
            event.message_id = "msg_timeout"
            event.raw_message = "超时测试"
            event.unified_msg_origin = "bilibili"

            event_id = await self.adapter.ingest_message_event(
                event, platform_id="bili:comment:timeout", source_type="comment"
            )

            # 第一次 claim
            claimed = await self.adapter.claim_event()
            self.assertIsNotNone(claimed)

            # 模拟超时：手动修改 claimed_at 为 1 小时前
            import time

            old_time = time.time() - 3600
            numeric_id = int(event_id[4:])
            await self.adapter.db.execute(
                "UPDATE events SET claimed_at = ? WHERE id = ?", (old_time, numeric_id)
            )

            # 第二次 claim 应该成功（超时回收）
            reclaimed = await self.adapter.claim_event()
            self.assertIsNotNone(reclaimed)
            self.assertEqual(reclaimed["event_id"], event_id)

        asyncio.run(run())

    def test_fail_event(self):
        """测试事件失败标记"""

        async def run():
            event = AstrMessageEvent()
            event.message_str = "失败测试"
            event.message_type = MessageType.GROUP_MESSAGE
            event.session_id = "fail_session"
            event.message_id = "msg_fail"
            event.raw_message = "失败测试"
            event.unified_msg_origin = "bilibili"

            event_id = await self.adapter.ingest_message_event(
                event, platform_id="bili:comment:fail", source_type="comment"
            )

            claimed = await self.adapter.claim_event()
            self.assertIsNotNone(claimed)

            # 标记为失败
            await self.adapter.fail_event(event_id, "模拟处理错误")

            # 验证状态
            numeric_id = int(event_id[4:])
            result = await self.adapter.db.fetch_one(
                "SELECT state, error FROM events WHERE id = ?", (numeric_id,)
            )
            self.assertEqual(result["state"], "failed")
            self.assertIn("模拟处理错误", result["error"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
