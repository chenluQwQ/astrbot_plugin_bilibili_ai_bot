"""主动评论额度与主人通知回归；仅使用虚构账号、内存日志和模拟发送。"""
import asyncio
import copy
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_proactive_flow_snapshot import BASE_CONFIG, FlowProbe, evaluation, video
from core.proactive import ProactiveMixin


class CommentProbe(FlowProbe):
    def __init__(self, config=None, **kwargs):
        super().__init__({
            **BASE_CONFIG, "PROACTIVE_LIKE": False, "PROACTIVE_FAV": False,
            "PROACTIVE_HOT_REPLY_COUNT": 2, "RECOMMEND_OWNER_DELIVERY": "off",
            **(config or {}),
        }, **kwargs)
        self.sent = []
        self.send_status = "published"
        self._reply_hot_comments = AsyncMock(return_value=2)
        self._owner_recommended_bvids = set()

    async def _send_comment(self, oid, text, at_names=None, *, result_sink=None, **kwargs):
        self.sent.append({"oid": oid, "text": text, "at_names": at_names})
        if result_sink is not None:
            result_sink.update(send_status=self.send_status, rpid="fake-reply")
        if self.send_status == "unknown":
            raise TimeoutError("fake_unknown_send")
        await asyncio.sleep(0)
        return self.send_status == "published"

    async def _send_gallery_comment(self, oid, text, *, at_names, result_sink, **kwargs):
        result_sink.update(images=[], image_selection={})
        return await self._send_comment(oid, text, at_names, result_sink=result_sink)


def watch_round(flow="proactive", **kwargs):
    # 同一支架可运行于服务器旧主流程和 Fork 重构版的共享发送 helper。
    return SimpleNamespace(
        is_special=flow == "special_follow", action_key_prefix=flow,
        per_video_comment=1, comment_count=0, daily_comment_limit=2,
        commented_videos=set(), current_evaluation=evaluation(9),
        can_recommend_owner=False, owner_recommend_count=0, recommend_ledger={},
        **kwargs,
    )


class DailyCountTests(unittest.TestCase):
    def test_seconds_minutes_and_duplicate_publication_records_count_once(self):
        today = datetime.now().strftime("%Y-%m-%d")
        root = {"time": f"{today} 10:00:37", "bvid": "BV_FAKE_A",
                "comment": "正文", "send_status": "published"}
        watch = [{"time": f"{today} 10:02", "bvid": "BV_FAKE_A",
                  "actions": ["💬评论", "💬热评×2", "📢评论区推荐给主人"]}]
        self.assertEqual(ProactiveMixin._today_proactive_comment_count(watch, [root, root]), 1)

    def test_hot_replies_recommendations_and_failed_sends_do_not_take_root_quota(self):
        today = datetime.now().strftime("%Y-%m-%d")
        watch = [
            {"time": today, "bvid": "BV_HOT", "actions": ["💬热评×3"]},
            {"time": today, "bvid": "BV_REC", "actions": ["📢评论区推荐给主人"]},
        ]
        proactive = [
            {"time": today, "bvid": "BV_FAIL", "send_status": "failed"},
            {"time": today, "bvid": "BV_UNKNOWN", "send_status": "unknown"},
            {"time": today, "bvid": "BV_HOT", "type": "hot_reply", "comment": "热评"},
            {"time": today, "bvid": "BV_REC", "type": "owner_recommend", "comment": "推荐"},
            {"time": "2000-01-01", "bvid": "BV_OLD", "comment": "旧评论"},
        ]
        self.assertEqual(ProactiveMixin._today_proactive_comment_count(watch, proactive), 0)

    def test_legacy_bangumi_and_root_records_still_count(self):
        today = datetime.now().strftime("%Y-%m-%d")
        proactive = [
            {"time": f"{today} 10:00", "bvid": "BV_FAKE_A", "comment": "旧格式"},
            {"time": f"{today} 11:00", "type": "bangumi", "title": "虚构番剧第1话", "comment": "好"},
        ]
        self.assertEqual(ProactiveMixin._today_proactive_comment_count([], proactive), 2)


class QuotaFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_hot_replies_do_not_prevent_second_primary_comment(self):
        bot = CommentProbe()
        round_state = watch_round()
        for index in (1, 2):
            item = video(f"BV_FAKE_{index}", "100", "虚构UP", "虚构视频")
            actions = []
            await bot._interact_with_video(
                round_state, item, index, {"analysis_basis": "video_media"},
                "视频证据", evaluation(9), {"score": 9, "review": "观感"}, actions,
            )
            self.assertIn("💬评论", actions)
            self.assertIn("💬热评×2", actions)
        self.assertEqual(round_state.comment_count, 2)
        self.assertEqual(len(bot.sent), 2)
        self.assertEqual(bot._reply_hot_comments.await_count, 2)

    async def test_special_follow_keeps_independent_hot_reply_switch(self):
        bot = CommentProbe()
        round_state = watch_round("special_follow")
        await bot._interact_with_video(
            round_state, video("BV_SPECIAL", "100", "虚构UP", "虚构视频"), 1,
            {"analysis_basis": "video_media"}, "证据", evaluation(9),
            {"score": 9, "review": "观感"}, [],
        )
        bot._reply_hot_comments.assert_awaited_once()
        self.assertEqual(round_state.comment_count, 1)

    async def test_hot_replies_still_run_when_primary_quota_is_full_or_disabled(self):
        for enabled in (True, False):
            with self.subTest(primary_enabled=enabled):
                bot = CommentProbe({"PROACTIVE_COMMENT": enabled})
                round_state = watch_round()
                round_state.comment_count = round_state.daily_comment_limit
                await bot._interact_with_video(
                    round_state, video("BV_FULL", "100", "虚构UP", "虚构视频"), 1,
                    {"analysis_basis": "video_media"}, "证据", evaluation(9),
                    {"score": 9, "review": "观感"}, [],
                )
                self.assertEqual(bot.sent, [])
                bot._reply_hot_comments.assert_awaited_once()
                self.assertEqual(round_state.comment_count, 2)


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def send(self, bot, *, bvid="BV_FAKE_A", recommendation=False):
        sink = {}
        result = await bot._send_proactive_comment_once(
            1, "有趣的转场", bvid=bvid, title="虚构视频",
            result_sink=sink, recommendation=recommendation,
        )
        return result, sink

    async def test_published_root_suppresses_second_owner_mention_after_reload(self):
        bot = CommentProbe()
        self.assertTrue((await self.send(bot))[0])
        restarted = CommentProbe(files=copy.deepcopy(bot.files))
        self.assertFalse((await self.send(restarted, recommendation=True))[0])
        self.assertEqual(restarted.sent, [])
        self.assertEqual(bot.sent[0]["at_names"], {"柠弥": "777"})

    async def test_ordinary_comment_without_actual_mention_allows_recommendation(self):
        bot = CommentProbe({"OWNER_MID": ""})
        await self.send(bot)
        self.assertIsNone(bot.sent[0]["at_names"])
        bot.config["OWNER_MID"] = "777"
        self.assertTrue((await self.send(bot, recommendation=True))[0])
        self.assertEqual(len(bot.sent), 2)
        self.assertEqual(bot.sent[1]["at_names"], {"柠弥": "777"})

    async def test_explicit_failure_allows_recommendation(self):
        bot = CommentProbe()
        bot.send_status = "failed"
        self.assertFalse((await self.send(bot))[0])
        bot.send_status = "published"
        self.assertTrue((await self.send(bot, recommendation=True))[0])

    async def test_unknown_send_blocks_recommendation_after_reload(self):
        bot = CommentProbe()
        bot.send_status = "unknown"
        with self.assertRaises(TimeoutError):
            await self.send(bot)
        restarted = CommentProbe(files=copy.deepcopy(bot.files))
        self.assertFalse((await self.send(restarted, recommendation=True))[0])
        self.assertEqual(restarted.sent, [])

    async def test_legacy_actual_owner_mention_is_imported(self):
        bot = CommentProbe(files={"proactive_log.json": [{
            "time": "2026-10-25 10:00:37", "bvid": "BV_FAKE_A",
            "comment": "@柠弥 旧评论", "send_status": "published", "rpid": "fake-old",
        }]})
        self.assertFalse((await self.send(bot, recommendation=True))[0])
        self.assertEqual(bot.sent, [])
        self.assertIn("owner_comment_notify_log.json", bot.files)

    async def test_explicit_no_mention_metadata_overrides_legacy_text_prefix(self):
        bot = CommentProbe(files={"proactive_log.json": [{
            "bvid": "BV_FAKE_A", "comment": "@柠弥 纯文本",
            "owner_mention_mid": "", "send_status": "published",
        }]})
        self.assertTrue((await self.send(bot, recommendation=True))[0])

    async def test_failed_post_send_bookkeeping_does_not_retry_published_comment(self):
        bot = CommentProbe()
        original_save = bot._save_json
        saves = 0

        def fail_second_save(path, data):
            nonlocal saves
            saves += 1
            if saves == 2:
                raise OSError("fake_disk_failure")
            original_save(path, data)

        bot._save_json = fail_second_save
        self.assertTrue((await self.send(bot))[0])
        restarted = CommentProbe(files=copy.deepcopy(bot.files))
        self.assertFalse((await self.send(restarted, recommendation=True))[0])
        self.assertEqual(restarted.sent, [])

    async def test_failed_reservation_aborts_before_external_send(self):
        bot = CommentProbe()
        bot._save_json = lambda *_: (_ for _ in ()).throw(OSError("fake_disk_failure"))
        with self.assertRaises(OSError):
            await self.send(bot)
        self.assertEqual(bot.sent, [])

    async def test_changed_owner_or_another_video_gets_own_notification(self):
        bot = CommentProbe()
        await self.send(bot)
        bot.config["OWNER_MID"] = "778"
        self.assertTrue((await self.send(bot, recommendation=True))[0])
        self.assertTrue((await self.send(bot, bvid="BV_FAKE_B", recommendation=True))[0])
        self.assertEqual(len(bot.sent), 3)

    async def test_concurrent_root_and_recommendation_produce_one_mention(self):
        bot = CommentProbe()
        await asyncio.gather(self.send(bot), self.send(bot, recommendation=True))
        self.assertEqual(sum(bool(item["at_names"]) for item in bot.sent), 1)

    async def test_earlier_recommendation_does_not_block_plain_root_comment(self):
        bot = CommentProbe()
        await self.send(bot, recommendation=True)
        result, sink = await self.send(bot)
        self.assertTrue(result)
        self.assertIsNone(bot.sent[-1]["at_names"])
        self.assertNotIn("@", bot.sent[-1]["text"])
        self.assertEqual(sink["owner_mention_mid"], "")

    async def test_all_delivery_keeps_private_channels_without_second_comment_at(self):
        bot = CommentProbe({"RECOMMEND_OWNER_DELIVERY": "all"})
        bot._send_owner_recommend_qq_group = AsyncMock(return_value=True)
        await self.send(bot)
        state = watch_round()
        actions = ["💬评论"]
        await bot._recommend_video_to_owner(
            state, video("BV_FAKE_A", "100", "虚构UP", "虚构视频"), 1,
            "具体画面", evaluation(9), "观感", actions,
        )
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("✉️私信推荐给主人", actions)
        self.assertIn("💬QQ推荐给主人", actions)
        self.assertIn("👥QQ群推荐给主人", actions)
        self.assertNotIn("📢评论区推荐给主人", actions)
        self.assertEqual(state.owner_recommend_count, 1)

    async def test_comment_action_marker_alone_does_not_suppress_actual_recommendation(self):
        bot = CommentProbe({"RECOMMEND_OWNER_DELIVERY": "comment"})
        actions = ["💬评论"]
        await bot._recommend_video_to_owner(
            watch_round(), video("BV_FAKE_A", "100", "虚构UP", "虚构视频"), 1,
            "具体画面", evaluation(9), "观感", actions,
        )
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0]["at_names"], {"柠弥": "777"})
        self.assertIn("📢评论区推荐给主人", actions)
