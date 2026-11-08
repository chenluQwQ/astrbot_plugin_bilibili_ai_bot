"""备忘录（B1）：存储规则、评论和私信里的 bili_memo 工具、谁能记、/bili备忘录。"""
import asyncio
import tempfile
import types
import unittest
from datetime import datetime
from unittest.mock import AsyncMock

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  先装上假的 astrbot
from core.domain.memos import EXPIRED, MemoRejected, Memos
from tests.test_comment_context import Bot, response
import test_private_tools
from test_private_reply_snapshot import message
from test_comment_reply_snapshot import reply_json
from tool_fakes import WithToolFakes, call

NOW = datetime(2026, 11, 6, 20, 0).timestamp()
DAY = 24 * 3600
BV = "BV1Ab411c7Xy"


def add(memos, target=BV, kind="video", uid="11", **kw):
    kw.setdefault("when", "tomorrow")
    kw.setdefault("source", "comment")
    return memos.add(kind=kind, target=target, from_uid=uid, from_name=f"用户{uid}", now=kw.pop("now", NOW), **kw)


class MemoStoreTests(unittest.TestCase):
    def setUp(self):
        self.memos = Memos.in_memory()
        self.addCleanup(self.memos.close)

    def test_records_with_a_due_date(self):
        memo_id, created = add(self.memos, note="评论区说很好笑")
        self.assertTrue(created)
        item = self.memos.open_items(NOW)[0]
        self.assertEqual((item["id"], item["key"], item["due_date"], item["state"]), (memo_id, BV, "2026-11-07", "pending"))
        self.assertEqual(item["recommenders"], [{"uid": "11", "name": "用户11"}])
        _, _ = add(self.memos, target="葬送的芙莉莲", kind="bangumi", uid="12", when="someday")
        self.assertEqual(self.memos.open_items(NOW)[0]["due_date"], "")

    def test_same_thing_again_only_adds_the_recommender(self):
        first, _ = add(self.memos, target=f"看看 {BV}")
        again, created = add(self.memos, uid="12")
        add(self.memos, uid="12")
        self.assertEqual((again, created), (first, False))
        self.assertEqual([p["uid"] for p in self.memos.open_items(NOW)[0]["recommenders"]], ["11", "12"])
        _, created = add(self.memos, target="葬送的 芙莉莲", kind="bangumi")
        _, again = add(self.memos, target="葬送的芙莉莲", kind="bangumi", uid="13")
        self.assertTrue(created)
        self.assertFalse(again)

    def test_bad_input_is_rejected_with_a_reason(self):
        for kw in (dict(kind="song"), dict(when="next_week"), dict(target="不是BV号"), dict(target=""),
                   dict(target="长" * 61, kind="other"), dict(note="长" * 81)):
            with self.subTest(kw=kw), self.assertRaises(MemoRejected):
                add(self.memos, **kw)

    def test_two_per_person_per_day_unless_unlimited(self):
        add(self.memos, target="BV1Ab411c7X1")
        add(self.memos, target="BV1Ab411c7X2")
        with self.assertRaises(MemoRejected):
            add(self.memos, target="BV1Ab411c7X3")
        add(self.memos, target="BV1Ab411c7X3", now=NOW + DAY)  # 第二天又可以
        add(self.memos, target="BV1Ab411c7X4", uid="777", unlimited=True)
        add(self.memos, target="BV1Ab411c7X5", uid="777", unlimited=True)
        add(self.memos, target="BV1Ab411c7X6", uid="777", unlimited=True)

    def test_full_list_pushes_out_the_oldest_someday_first(self):
        self.memos.max_pending = 3
        old_someday, _ = add(self.memos, target="BV1Ab411c7X1", uid="1", when="someday", now=NOW)
        add(self.memos, target="BV1Ab411c7X2", uid="2", now=NOW + 1)
        add(self.memos, target="BV1Ab411c7X3", uid="3", now=NOW + 2)
        add(self.memos, target="BV1Ab411c7X4", uid="4", now=NOW + 3)
        self.assertNotIn(old_someday, [item["id"] for item in self.memos.open_items(NOW + 3)])
        add(self.memos, target="BV1Ab411c7X5", uid="5", now=NOW + 4)
        self.assertEqual([item["key"][-1] for item in self.memos.open_items(NOW + 4)], ["5", "4", "3"])

    def test_pending_expires_after_three_days_and_drop_is_final(self):
        memo_id, _ = add(self.memos)
        self.assertEqual(len(self.memos.open_items(NOW + 2 * DAY)), 1)
        self.assertEqual(self.memos.open_items(NOW + 3 * DAY + 1), [])
        self.assertEqual(self.memos.all()[0]["state"], EXPIRED)
        self.assertFalse(self.memos.drop(memo_id, "晚了"))
        other, _ = add(self.memos, target="BV1Ab411c7X9", now=NOW + 8 * DAY)
        self.assertTrue(self.memos.drop(other, "不想看了"))
        self.assertFalse(self.memos.drop(other, "再删一次"))

    def test_scheduled_then_done(self):
        memo_id, _ = add(self.memos)
        self.assertTrue(self.memos.schedule(memo_id, "2026-11-07", now=NOW))
        self.assertFalse(self.memos.schedule(memo_id, "2026-11-07", now=NOW))  # 已经排了
        self.assertEqual(self.memos.pending(NOW), [])
        self.assertEqual(self.memos.get(memo_id)["plan_date"], "2026-11-07")
        self.assertTrue(self.memos.finish(memo_id, "看完了，8分", now=NOW))
        self.assertEqual((self.memos.get(memo_id)["state"], self.memos.get(memo_id)["result"]), ("done", "看完了，8分"))
        self.assertEqual(self.memos.open_items(NOW), [])

    def test_scheduled_but_not_done_goes_back_and_still_expires(self):
        memo_id, _ = add(self.memos)
        self.memos.schedule(memo_id, "2026-11-07", now=NOW)
        self.assertEqual(self.memos.release_stale("2026-11-07", now=NOW), 0)  # 当天的不动
        self.assertEqual(self.memos.release_stale("2026-11-08", now=NOW + DAY), 1)
        self.assertEqual([item["id"] for item in self.memos.pending(NOW + DAY)], [memo_id])
        self.assertEqual(self.memos.pending(NOW + 3 * DAY + 1), [])  # 从记下那天算 3 天

    def test_closed_ones_are_purged_after_a_day(self):
        done, _ = add(self.memos, target="BV1Ab411c7X1", uid="1")
        dropped, _ = add(self.memos, target="BV1Ab411c7X2", uid="2")
        still_open, _ = add(self.memos, target="BV1Ab411c7X3", uid="3")
        self.memos.finish(done, "看完了", now=NOW)
        self.memos.drop(dropped, "不想看", now=NOW)
        self.assertEqual(self.memos.purge_closed(NOW + 3600), 0)  # 当天还留着
        self.assertEqual(self.memos.purge_closed(NOW + DAY + 1), 2)
        self.assertEqual([item["id"] for item in self.memos.all()], [still_open])
        _, created = add(self.memos, target="BV1Ab411c7X1", uid="4", now=NOW + DAY + 2)
        self.assertTrue(created)

    def test_old_table_gets_the_new_column(self):
        import sqlite3
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute(
            "CREATE TABLE memos (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL,"
            " target TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', due_date TEXT NOT NULL DEFAULT '',"
            " source TEXT NOT NULL, from_uid TEXT NOT NULL DEFAULT '', recommenders TEXT NOT NULL DEFAULT '[]',"
            " state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',"
            " created_at REAL NOT NULL, state_at REAL NOT NULL)")
        memos = Memos(conn)
        memo_id, _ = add(memos)
        self.assertTrue(memos.schedule(memo_id, "2026-11-07", now=NOW))

    def test_survives_reopening_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/bot.sqlite3"
            memos = Memos.open(path)
            add(memos)
            memos.close()
            memos = Memos.open(path)
            self.assertEqual(len(memos.open_items(NOW)), 1)
            memos.close()


def memo(target=BV, kind="video", when="tomorrow", note="说很好笑"):
    return call("bili_memo", kind=kind, target=target, when=when, note=note)


def comment_bot(script):
    """评论测试的假插件没有好感和身份，这里补上（真插件装配时都有）。"""
    bot = Bot(script)
    bot.roles = types.SimpleNamespace(is_owner=lambda uid: uid == "777")
    bot.affection = types.SimpleNamespace(score=lambda uid: 5)
    bot.seen_videos = types.SimpleNamespace(has_seen=AsyncMock(return_value=False))
    return bot


class CommentMemoTests(WithToolFakes, unittest.IsolatedAsyncioTestCase):
    async def test_comment_can_record_then_stay_silent(self):
        bot = comment_bot([memo(), response("ignore")])
        result = await bot.flow(nickname="甲")
        self.assertEqual(result["decision"], "ignore")
        self.assertIn("bili_memo", bot.tool_loop.calls[0]["tools"])
        self.assertIn("记下了", bot.answers()[0])
        item = bot.memos.open_items()[0]
        self.assertEqual((item["source"], item["from_uid"], item["recommenders"][0]["name"]), ("comment", "1", "甲"))

    async def test_disliked_people_cannot_make_the_bot_record(self):
        bot = comment_bot([memo(), response()])
        bot.affection.score = lambda uid: -3
        await bot.flow()
        self.assertIn("不记", bot.answers()[0])
        self.assertIsNone(getattr(bot, "memos", None))  # 连备忘录都没打开

    async def test_switch_off_and_read_only_test_do_not_offer_it(self):
        bot = Bot([response()])
        bot.config["ENABLE_MEMO"] = False
        await bot.flow()
        self.assertNotIn("bili_memo", bot.tool_loop.calls[0]["tools"])
        bot = Bot([response()])
        await bot.comment_context._run_comment_reply_flow("p", "s", 123, 1, "manual_test", test_mode=True)
        self.assertNotIn("bili_memo", bot.tool_loop.calls[0]["tools"])

    async def test_already_watched_video_is_not_recorded(self):
        bot = comment_bot([memo(), response()])
        bot.seen_videos.has_seen = AsyncMock(return_value=True)
        await bot.flow()
        bot.seen_videos.has_seen.assert_awaited_once_with(BV)
        self.assertIn("已经看过", bot.answers()[0])
        self.assertIsNone(getattr(bot, "memos", None))

    async def test_one_memo_per_comment(self):
        bot = comment_bot([memo(), memo("BV1Ab411c7X2"), response()])
        await bot.flow()
        self.assertEqual(len(bot.memos.open_items()), 1)
        self.assertIn("次数用完", bot.answers()[1])


class PrivateMemoTests(test_private_tools.PrivateToolTests):
    async def test_owner_records_without_a_daily_limit(self):
        probe = self.probe([memo("葬送的芙莉莲", kind="bangumi"), reply_json("记下啦")])
        self.assertTrue(await self.handle(probe, message(777, "小柠", "去看芙莉莲第二季")))
        item = probe.memos.all()[0]  # 测试把时钟定在了 2025 年，按现在算早过期了
        self.assertEqual((item["kind"], item["source"], item["from_uid"]), ("bangumi", "owner", "777"))

    async def test_stranger_records_as_private(self):
        probe = self.probe([memo(), reply_json("好呀")], PRIVATE_MESSAGE_REPLY_SCOPE="all")
        probe.affection.score = lambda uid: 5
        probe.reply_gate._should_reply_by_interest = lambda *a, **k: asyncio.sleep(0, (True, "ok"))
        self.assertTrue(await self.handle(probe, message(11, "甲", f"推荐你看 {BV}，很好笑")))
        self.assertEqual(probe.memos.all()[0]["source"], "private")

    # 这个类继承来的测试已经在 test_private_tools 里跑过了。
    for _name in [n for n in dir(test_private_tools.PrivateToolTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name


class MemoReviewTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_keeps_only_valid_pending_ids(self):
        from core.features.memo_review import parse_memo_review
        raw = '{"drop":[{"id":1,"reason":"没兴趣了"},{"id":9,"reason":"不存在"},{"id":"2"},{"id":3},{"id":1}]}'
        self.assertEqual(parse_memo_review(raw, {1, 2, 3}), [(1, "没兴趣了"), (3, "不想看了")])
        self.assertEqual(parse_memo_review('```json\n{"drop":[]}\n```', {1}), [])
        for bad in ('{"drop":[], "extra":1}', '{"keep":[]}', "不丢", '{"drop":{"id":1}}'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_memo_review(bad, {1})

    def review(self, reply):
        from core.features.memo_review import MemoReview
        memos = Memos.in_memory()
        add(memos, now=datetime.now().timestamp())
        add(memos, target="BV1Ab411c7X2", uid="12", now=datetime.now().timestamp())
        prompts = []

        async def llm(prompt, **_kw):
            prompts.append(prompt)
            if isinstance(reply, Exception):
                raise reply
            return reply

        async def persona():
            return "人设"

        host = types.SimpleNamespace(
            memo_tool=types.SimpleNamespace(enabled=lambda: True, _memos=lambda: memos),
            models=types.SimpleNamespace(_llm_call=llm, _get_system_prompt=persona), settings={},
        )
        return MemoReview(host), memos, prompts

    async def test_drops_what_the_model_picks_once_a_night(self):
        review, memos, prompts = self.review('{"drop":[{"id":2,"reason":"像是刷的"}]}')
        await review.maybe_review()
        await review.maybe_review()
        self.assertEqual(len(prompts), 1)
        self.assertIn('trust="untrusted"', prompts[0])
        self.assertEqual([item["id"] for item in memos.pending()], [1])
        self.assertIn("像是刷的", memos.get(2)["reason"])

    async def test_bad_output_or_failure_drops_nothing(self):
        for reply in ("我觉得都可以丢", RuntimeError("403")):
            review, memos, _ = self.review(reply)
            await review.maybe_review()
            self.assertEqual(len(memos.pending()), 2)


class MemoCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_and_deletes(self):
        from command_modules import load
        commands = load("memory")
        memos = Memos.in_memory()
        add(memos, note="说很好笑", now=datetime.now().timestamp())
        app = types.SimpleNamespace(open_memos=memos.open_items, drop_memo=lambda i: memos.drop(i, "主人删除"))

        class Event:
            def __init__(self, text):
                self.message_str = text

            def plain_result(self, text):
                return text

        listed = "\n".join([chunk async for chunk in commands.memos(app, Event("/bili备忘录"))])
        self.assertIn(f"#1 [视频] {BV}", listed)
        self.assertIn("用户11", listed)
        self.assertIn("说很好笑", listed)
        self.assertEqual([c async for c in commands.memos(app, Event("/bili备忘录 删除 #1"))], ["✅ 已删除备忘 #1"])
        self.assertEqual([c async for c in commands.memos(app, Event("/bili备忘录 删除 1"))], ["❌ 没有还开着的备忘 #1"])
        self.assertEqual([c async for c in commands.memos(app, Event("/bili备忘录"))], ["📝 备忘录是空的"])


if __name__ == "__main__":
    unittest.main()
