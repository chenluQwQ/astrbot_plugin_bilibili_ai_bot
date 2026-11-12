"""WebUI「今天」页：按日期筛出看过的视频、评论、动态、番剧，长点评不截断，结果未知的评论也列出来；
删自己发的评论（发的时候记下 rpid，删了只标已删除，结果未知不再删，算进全局次数）。"""

import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from activity_parts import ListLog
from test_webui_interest import _install_astrbot_stub

_install_astrbot_stub()

from core.infra.bili.comments import BiliComments, PostedComment
from core.layered_runtime import LayeredRuntime
from core.runtime import ActionRequest, EventRuntime, WriteResultUnknownError
from core.web import today

TODAY = datetime.now().strftime("%Y-%m-%d")
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
LONG_REVIEW = "转场那一下太舒服了。" * 40


def _plugin(layered=None):
    logs = {
        "watch_log": [
            {"time": f"{YESTERDAY} 09:00", "bvid": "BV1yesterday", "title": "昨天的", "score": 6},
            {"time": f"{TODAY} 10:12", "bvid": "BV1abcdefghi", "title": "手绘短片", "up_name": "UP",
             "score": 8, "mood": "开心", "review": LONG_REVIEW, "actions": ["👍点赞"]},
            {"time": f"{TODAY} 10:20", "bvid": "BV1failedxxx", "title": "没看成", "score": 0,
             "mood": "未知", "comment": "评价失败", "evaluation_status": "failed"},
        ],
        "reply_log": [
            {"time": f"{TODAY} 15:31", "username": "路人", "content": "好看", "reply": "对吧", "bvid": "BV1abcdefghi"},
            {"time": f"{TODAY} 15:40", "username": "私信的人", "content": "在吗", "reply": "在", "channel": "private"},
        ],
        "proactive_log": [{"time": f"{TODAY} 10:13", "bvid": "BV1abcdefghi", "title": "手绘短片", "comment": "转场好棒"}],
        "dynamic_log": [{"time": f"{TODAY} 16:30", "text": "今天看了手绘短片", "comment_sync": True}],
        "bangumi_watch_log": [{"time": f"{TODAY} 12:10", "title": "番", "ep_index": "3", "score": 7, "review": "舒服"}],
    }
    kinds = {"watch_log": "watch", "reply_log": "reply", "proactive_log": "proactive",
             "dynamic_log": "dynamic", "bangumi_watch_log": "bangumi"}
    activity_logs = types.SimpleNamespace(**{kinds[name]: ListLog(items) for name, items in logs.items()})
    app = types.SimpleNamespace(layered_runtime=layered, activity_logs=activity_logs)
    return types.SimpleNamespace(app=app)


def _pool_plugin(pool):
    plugin = _plugin()
    plugin.app.dynamic_post = types.SimpleNamespace(idea_pool=lambda: pool)
    return plugin


def _ask(date=""):
    today.request = types.SimpleNamespace(query={"date": date} if date else {})


def _post(body):
    async def json(default=None):
        return body

    today.request = types.SimpleNamespace(query={}, json=json)


def _bili(responses):
    """假的B站接口：按顺序回 ``responses``，记下发出去的请求。"""
    sent = []

    async def post(url, data=None, **_kw):
        sent.append((url, dict(data or {})))
        reply = responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply, None

    transport = types.SimpleNamespace(
        settings={"BILI_JCT": "csrf"}, bili_http=types.SimpleNamespace(_http_post=post))
    return BiliComments(transport), sent


class TodayPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_one_day_in_full(self):
        _ask()
        data = (await today.handle_get_today(_plugin()))["data"]
        self.assertEqual(data["date"], TODAY)
        self.assertEqual(data["days"], [TODAY, YESTERDAY])
        self.assertEqual([item["bvid"] for item in data["videos"]], ["BV1abcdefghi", "BV1failedxxx"])
        self.assertEqual(data["videos"][0]["review"], LONG_REVIEW)
        self.assertTrue(data["videos"][1]["failed"])
        self.assertIsNone(data["videos"][1]["score"])
        # 私信不算评论；按时间排
        self.assertEqual([item["text"] for item in data["comments"]], ["转场好棒", "对吧"])
        self.assertEqual(data["comments"][1]["to_user"], "路人")
        self.assertEqual(data["dynamics"][0]["kind"], "comment_sync")
        self.assertEqual(data["bangumi"][0]["ep_index"], "3")

    async def test_other_day_and_bad_date(self):
        _ask(YESTERDAY)
        data = (await today.handle_get_today(_plugin()))["data"]
        self.assertEqual([item["title"] for item in data["videos"]], ["昨天的"])
        self.assertEqual(data["comments"], [])
        _ask("昨天")
        self.assertEqual((await today.handle_get_today(_plugin()))["status"], "error")


class TodayCommentActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.layers = LayeredRuntime({}, Path(self.temp_dir.name) / "bilibot.sqlite3")
        await self.layers.open()
        self.runtime = EventRuntime(observer=self.layers)

    async def asyncTearDown(self):
        await self.layers.close()
        self.temp_dir.cleanup()

    async def test_unknown_and_tool_comments_come_from_action_table(self):
        async def lost():
            raise WriteResultUnknownError("timeout")

        async def sent():
            return True

        await self.runtime.execute(ActionRequest(
            key="proactive_comment:BV1abcdefghi", kind="proactive_comment", target_id="BV1abcdefghi",
            metadata={"text": "不知道发没发", "title": "手绘短片"}), lost)
        await self.runtime.execute(ActionRequest(
            key="tool_comment:11:x", kind="tool_comment", target_id="11", metadata={"text": "QQ 让发的"}), sent)
        # 发成了的看片评论日志里已经有，不从动作表重复列
        await self.runtime.execute(ActionRequest(
            key="proactive_comment:BV1other0000", kind="proactive_comment", target_id="BV1other0000",
            metadata={"text": "发成了"}), sent)

        _ask()
        comments = (await today.handle_get_today(_plugin(self.layers)))["data"]["comments"]
        from_actions = {item["text"]: item for item in comments if item["text"] in {"不知道发没发", "QQ 让发的", "发成了"}}
        self.assertEqual(set(from_actions), {"不知道发没发", "QQ 让发的"})
        self.assertEqual(from_actions["不知道发没发"]["result"], "unknown")
        self.assertEqual(from_actions["不知道发没发"]["title"], "手绘短片")
        self.assertEqual(from_actions["QQ 让发的"]["kind"], "tool")
        self.assertEqual(from_actions["QQ 让发的"]["result"], "sent")


class PostedCommentTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_returns_rpid_and_stays_truthy(self):
        api, _ = _bili([{"code": 0, "data": {"rpid": 777}}, {"code": 0, "data": {"rpid": 778}}, {"code": 0, "data": {}}])
        ok, reason = await api._send_comment_detailed(11, "好看")
        self.assertEqual(ok, PostedComment(11, 1, 777, "好看"))
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        reply = await api._send_reply(12, 5, 1, "对吧")
        self.assertEqual((reply.oid, reply.rpid), (12, 778))
        # B站没回 rpid 就还是 True
        self.assertIs(await api._send_comment(11, "再来"), True)

    async def test_delete_codes(self):
        api, sent = _bili([{"code": 0}, {"code": 12022, "message": "已经被删除了"},
                           {"code": -101}, WriteResultUnknownError("timeout")])
        self.assertEqual(await api._delete_comment(11, 1, 777), (True, ""))
        self.assertEqual(sent[0][1], {"oid": 11, "type": 1, "rpid": 777, "csrf": "csrf"})
        self.assertEqual(await api._delete_comment(11, 1, 777), (True, "already"))
        self.assertEqual(await api._delete_comment(11, 1, 777), (False, "cookie"))
        with self.assertRaises(WriteResultUnknownError):
            await api._delete_comment(11, 1, 777)


class TodayDeleteCommentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.layers = LayeredRuntime({}, Path(self.temp_dir.name) / "bilibot.sqlite3")
        await self.layers.open()
        self.runtime = EventRuntime(observer=self.layers)
        self.deletes = []
        self.delete_result = (True, "")

        async def delete_comment(oid, reply_type, rpid):
            self.deletes.append((oid, reply_type, rpid))
            if isinstance(self.delete_result, Exception):
                raise self.delete_result
            return self.delete_result

        self.plugin = _plugin(self.layers)
        self.plugin.app.event_runtime = self.runtime
        self.plugin.app.delete_comment = delete_comment

    async def asyncTearDown(self):
        await self.layers.close()
        self.temp_dir.cleanup()

    async def _post_tool_comment(self, rpid=900):
        async def send():
            return PostedComment(11, 1, rpid, "QQ 让发的")

        await self.runtime.execute(ActionRequest(
            key=f"tool_comment:11:{rpid}", kind="tool_comment", target_id="11",
            metadata={"text": "QQ 让发的"}), send)

    async def _comments(self):
        _ask()
        return (await today.handle_get_today(self.plugin))["data"]["comments"]

    async def _delete(self, rpid):
        _post({"rpid": rpid})
        return await today.handle_delete_comment(self.plugin)

    async def test_sent_comment_is_remembered_and_listed_as_deletable(self):
        await self._post_tool_comment()
        self.plugin.app.activity_logs.proactive.rows[0]["own_rpid"] = "901"
        await self.layers._remember_own_comment(
            ActionRequest(key="proactive_comment:BV1abcdefghi", kind="proactive_comment"),
            PostedComment(11, 1, 901, "转场好棒"))
        by_text = {item["text"]: item for item in await self._comments()}
        self.assertEqual((by_text["QQ 让发的"]["rpid"], by_text["QQ 让发的"]["deletable"]), ("900", True))
        self.assertEqual((by_text["转场好棒"]["rpid"], by_text["转场好棒"]["deletable"]), ("901", True))
        # 日志里没记 rpid 的（更早的）删不了
        self.assertFalse(by_text["对吧"]["deletable"])
        self.assertNotIn("action_key", by_text["QQ 让发的"])

    async def test_delete_marks_row_and_counts_toward_global_budget(self):
        await self._post_tool_comment()
        response = await self._delete("900")
        self.assertEqual(response["data"]["delete_state"], "succeeded")
        self.assertEqual(self.deletes, [(11, 1, 900)])
        row = (await self.layers.own_comments(rpids=["900"]))["900"]
        self.assertIsNotNone(row["deleted_at"])  # 标已删除，行还在
        item = next(item for item in await self._comments() if item["rpid"] == "900")
        self.assertEqual(item["delete_state"], "succeeded")
        counters = {row["bucket"]: row["count"] for row in (await self.layers.snapshot())["behavior"]["current_counters"]}
        self.assertEqual(counters["behavior:global:day"], 2)  # 发一次、删一次
        # 再点一次不再调接口
        await self._delete("900")
        self.assertEqual(len(self.deletes), 1)

    async def test_unknown_result_is_not_retried(self):
        await self._post_tool_comment()
        self.delete_result = WriteResultUnknownError("timeout")
        response = await self._delete("900")
        self.assertEqual(response["data"]["delete_state"], "unknown")
        self.assertIn("B站确认", response["message"])
        self.delete_result = (True, "")
        self.assertEqual((await self._delete("900"))["data"]["delete_state"], "unknown")
        self.assertEqual(len(self.deletes), 1)
        item = next(item for item in await self._comments() if item["rpid"] == "900")
        self.assertEqual(item["delete_state"], "unknown")

    async def test_failed_delete_can_be_tried_again(self):
        await self._post_tool_comment()
        self.delete_result = (False, "cookie")
        response = await self._delete("900")
        self.assertEqual(response["status"], "error")
        self.assertIn("Cookie", response["message"])
        self.assertEqual((await self.layers.own_comments(rpids=["900"]))["900"]["delete_state"], "failed")
        self.delete_result = (True, "")
        self.assertEqual((await self._delete("900"))["data"]["delete_state"], "succeeded")
        self.assertEqual(len(self.deletes), 2)

    async def test_unknown_rpid_is_refused(self):
        self.assertEqual((await self._delete("12345"))["status"], "error")
        self.assertEqual((await self._delete("abc"))["status"], "error")
        self.assertEqual(self.deletes, [])


class TodayIdeaPoolTests(unittest.IsolatedAsyncioTestCase):
    """动态池子只读：那天进池或状态变了的；看今天时带上所有还在等的。"""

    def _pool(self):
        from core.domain.dynamic_ideas import DynamicIdeas
        pool = DynamicIdeas.in_memory()
        now = datetime.now().timestamp()
        day_ago = now - 86400
        old = pool.add(key="BV1abcdefghi", kind="分享", source="proactive", title="昨天看的", angle="开头", now=day_ago)
        pool.add(key="feed:x", kind="转述", source="feed", title="新版本", up_name="某项目", angle="能导出了",
                 link="https://example.com/v2", now=now)
        posted = pool.add(key="dyn:9", kind="转发", source="dynamic_watch", title="招募", now=day_ago)
        pool.mark_posted(posted, now=day_ago + 60)
        return pool, old

    async def test_today_lists_waiting_and_today(self):
        pool, _ = self._pool()
        _ask()
        data = (await today.handle_get_today(_pool_plugin(pool)))["data"]
        rows = {item["title"]: item for item in data["ideas"]}
        self.assertEqual(set(rows), {"新版本", "昨天看的"})  # 昨天发掉的那条今天不列
        feed = rows["新版本"]
        self.assertEqual((feed["kind"], feed["source"], feed["link"], feed["state"], feed["changed"]),
                         ("转述", "站外订阅", "https://example.com/v2", "pending", ""))
        self.assertEqual(len(feed["created"]), 5)  # 今天的只写时分
        self.assertEqual(rows["昨天看的"]["bvid"], "BV1abcdefghi")
        self.assertIn("-", rows["昨天看的"]["created"])  # 别的日子带月日

    async def test_other_day_has_no_waiting_extra(self):
        pool, _ = self._pool()
        _ask(YESTERDAY)
        data = (await today.handle_get_today(_pool_plugin(pool)))["data"]
        rows = {item["title"]: item for item in data["ideas"]}
        self.assertEqual(set(rows), {"昨天看的", "招募"})
        self.assertEqual((rows["招募"]["state"], rows["招募"]["source"], rows["招募"]["bvid"]), ("posted", "关注动态", ""))

    async def test_no_pool_no_ideas(self):
        _ask()
        self.assertEqual((await today.handle_get_today(_plugin()))["data"]["ideas"], [])


if __name__ == "__main__":
    unittest.main()
