"""动态池子：进池、去重、3 天过期、最多 10 条、状态只往前走；发动态时挑想法、转发类走转发接口。"""
import asyncio
import types
import unittest

from bili_parts import BiliParts
from memory_parts import MemoryParts
from parts import part
from schedule_parts import LegacyNowDoing, ScheduleParts
from people_fakes import LegacyMood

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from core.domain.dynamic_ideas import DynamicIdeas, EXPIRE_SECONDS, MAX_PENDING
from core.domain.dynamic_log import DynamicLog
from core.storage.activity_log import ActivityLog
from core.features.dynamic_post import DynamicPost
from core.runtime import ActionOutcome
from core.settings import Settings
from itself import itself

DAY = 24 * 3600


def _add(pool, key, now, kind="吐槽", **extra):
    return pool.add(key=key, kind=kind, source="proactive", title=f"视频{key}", angle="想说的", now=now, **extra)


class DynamicIdeasTests(unittest.TestCase):
    def setUp(self):
        self.pool = DynamicIdeas.in_memory()

    def test_newest_first_and_one_pending_per_key(self):
        first = _add(self.pool, "BV1", 100)
        second = _add(self.pool, "BV2", 200)
        self.assertIsNone(_add(self.pool, "BV1", 300))
        self.assertEqual([item["id"] for item in self.pool.pending(now=300)], [second, first])

    def test_expires_after_three_days(self):
        _add(self.pool, "BV1", 0)
        self.assertEqual(len(self.pool.pending(now=EXPIRE_SECONDS - 1)), 1)
        self.assertEqual(self.pool.pending(now=EXPIRE_SECONDS + 1), [])
        self.assertEqual(self.pool.all()[0]["state"], "expired")
        # 过期的不挡同一个视频重新进池
        self.assertIsNotNone(_add(self.pool, "BV1", EXPIRE_SECONDS + 2))

    def test_full_pool_pushes_out_the_oldest(self):
        for n in range(MAX_PENDING + 2):
            _add(self.pool, f"BV{n}", n)
        keys = [item["key"] for item in self.pool.pending(now=100)]
        self.assertEqual(len(keys), MAX_PENDING)
        self.assertNotIn("BV0", keys)
        self.assertNotIn("BV1", keys)

    def test_states_only_move_forward(self):
        idea = _add(self.pool, "BV1", 0)
        self.pool.mark_posted(idea, now=1)
        self.pool.drop(idea, "晚了", now=2)
        self.assertEqual(self.pool.all()[0]["state"], "posted")

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            _add(self.pool, "BV1", 0, kind="日常")

    def test_survives_reopen(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "bilibot.sqlite3")
            pool = DynamicIdeas.open(path)
            _add(pool, "BV1", 0)
            pool.close()
            self.assertEqual(len(DynamicIdeas.open(path).pending(now=1)), 1)


class _Log:
    def __init__(self):
        self.data = []

    def read(self, default):
        return self.data

    def update(self, fn, default):
        self.data = fn(self.data)


class PostHost(BiliParts, MemoryParts):
    """真的 DynamicPost 流程；假的只有模型、B站和事件运行时。"""
    schedule_rules = ScheduleParts.schedule_rules
    daily_plan = ScheduleParts.daily_plan
    models = itself

    def __init__(self, replies, config=None):
        self.config = {"ENABLE_DYNAMIC": True, "DYNAMIC_FROM_VIDEO": True, "DYNAMIC_DAILY_COUNT": 3,
                       "DYNAMIC_POST_CHANCE": 0, **(config or {})}
        self.settings = Settings(self.config)
        self.replies = list(replies)
        self.prompts, self.sent, self.saved_memories = [], [], []
        self._memory = []
        self.pool = DynamicIdeas.in_memory()
        self.dynamic_log = DynamicLog(ActivityLog("dynamic", file=_Log()))
        self.event_runtime = types.SimpleNamespace(execute=self._execute)
        self.repost_result = True

    async def _execute(self, request, handler):
        ok = await handler()
        return ActionOutcome(success=bool(ok), action_key=request.key, value=ok, reason="" if ok else "rejected")

    @property
    def dynamic_ideas(self):
        return self.pool

    def _load_json(self, _path, default=None):
        return default

    mood = part(LegacyMood)

    def _get_today_mood(self):
        return "平静", ""

    def _autonomous_limit_max(self, _kind):
        return 0

    def _autonomous_plan_for_today(self):
        return {}

    async def _get_system_prompt(self):
        return "人设"

    async def _generation_llm_call(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        return self.replies.pop(0)

    async def _post_dynamic_text(self, text):
        self.sent.append(("text", text))
        return True

    async def _get_video_oid(self, bvid):
        return 1234 if bvid.startswith("BV") else None

    async def _repost_video(self, aid, text):
        self.sent.append(("repost", aid, text))
        return self.repost_result

    async def _repost_dynamic(self, dynamic_id, text):
        self.sent.append(("repost_dynamic", dynamic_id, text))
        return self.repost_result

    async def _save_self_memory_record(self, *args, **kwargs):
        self.saved_memories.append(args)

    now_doing = part(LegacyNowDoing)


    def _set_cross_platform_activity(self, *_args, **_kwargs):
        pass

    def _clear_cross_platform_activity(self, *_args):
        pass


def _reply(idea, text="想说的话", decision="post"):
    return ('{"decision":"%s","text":"%s","search_query":"","image":"none","image_prompt":"","idea":%d}'
            % (decision, text if decision == "post" else "", idea))


class DynamicPoolPostTests(unittest.TestCase):
    def _run(self, host, **kwargs):
        post = DynamicPost(host)
        asyncio.run(post.run(**kwargs))
        return post

    def test_after_round_posts_the_chosen_idea_and_keeps_the_rest(self):
        host = PostHost([])
        old = _add(host.pool, "BV_OLD", None, kind="分享")
        new = _add(host.pool, "BV_NEW", None, kind="段子")
        host.replies.append(_reply(old, "那个开头还是好笑"))
        self._run(host, fresh=(new,))
        # 刚看完的排在前面，但模型可以挑旧的
        self.assertLess(host.prompts[0].index(f"#{new}（刚看完"), host.prompts[0].index(f"#{old}（"))
        self.assertEqual(host.sent, [("text", "那个开头还是好笑")])
        entry = host.dynamic_log.entries()[0]
        self.assertEqual((entry["bvid"], entry["kind"], entry["idea_key"]), ("BV_OLD", "分享", "BV_OLD"))
        self.assertEqual([item["key"] for item in host.pool.pending()], ["BV_NEW"])

    def test_repost_idea_goes_out_as_a_repost(self):
        host = PostHost([])
        idea = _add(host.pool, "BV_R", None, kind="转发")
        host.replies.append(_reply(idea, "这个一定要看到最后"))
        self._run(host, fresh=(idea,))
        self.assertEqual(host.sent, [("repost", 1234, "这个一定要看到最后")])
        self.assertTrue(host.dynamic_log.entries()[0]["repost"])
        self.assertEqual(host.pool.pending(), [])

    def test_rejected_repost_drops_the_idea_without_text_fallback(self):
        host = PostHost([])
        host.repost_result = False
        idea = _add(host.pool, "BV_R", None, kind="转发")
        host.replies.append(_reply(idea, "这个一定要看"))
        self._run(host, fresh=(idea,))
        self.assertEqual(host.sent, [("repost", 1234, "这个一定要看")])
        self.assertEqual(host.dynamic_log.entries(), [])
        self.assertEqual(host.pool.all()[0]["state"], "dropped")

    def test_skip_keeps_ideas_in_the_pool(self):
        host = PostHost([])
        idea = _add(host.pool, "BV1", None)
        host.replies.append(_reply(0, decision="skip"))
        self._run(host, fresh=(idea,))
        self.assertEqual(host.sent, [])
        self.assertEqual(len(host.pool.pending()), 1)

    def test_daily_limit_keeps_ideas_for_later(self):
        host = PostHost([], {"DYNAMIC_DAILY_COUNT": 1})
        host.dynamic_log.record({"time": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"), "text": "早上那条"})
        idea = _add(host.pool, "BV1", None)
        self._run(host, fresh=(idea,))
        self.assertEqual(host.prompts, [])
        self.assertEqual(len(host.pool.pending()), 1)

    def test_scheduled_slot_skips_the_chance_roll_when_the_pool_has_ideas(self):
        host = PostHost([], {"DYNAMIC_POST_CHANCE": 0})
        self._run(host)
        self.assertEqual(host.prompts, [])  # 池子空：发布意愿 0 就不想
        idea = _add(host.pool, "BV1", None)
        host.replies.append(_reply(idea))
        self._run(host)
        # 发出去以后还有一次给这条动态记忆打分的调用，不算在「想动态」里
        self.assertEqual(len([p for p in host.prompts if "记进自己的记忆" not in p]), 1)
        self.assertEqual(host.sent, [("text", "想说的话")])

    def test_already_posted_video_is_dropped_not_offered(self):
        host = PostHost([])
        host.dynamic_log.record({"time": "2020-01-01 00:00", "text": "旧", "bvid": "BV1"})
        _add(host.pool, "BV1", None)
        post = DynamicPost(host)
        self.assertEqual(post._offered_ideas(), [])
        self.assertEqual(host.pool.all()[0]["state"], "dropped")
        self.assertIsNone(post.collect({"key": "BV1", "kind": "吐槽", "source": "proactive"}))


if __name__ == "__main__":
    unittest.main()


class OldKindNameTests(unittest.TestCase):
    """旧版本的「搬运」就是转发：表里的打开时改名，模型写旧名也认。"""

    def test_old_rows_renamed_on_open(self):
        import sqlite3
        from core.domain.dynamic_ideas import DynamicIdeas
        conn = sqlite3.connect(":memory:")
        pool = DynamicIdeas(conn)
        conn.execute(
            "INSERT INTO dynamic_ideas (key, kind, source, state, created_at, state_at)"
            " VALUES ('BV1', '搬运', 'video', 'pending', 0, 0)")
        conn.commit()
        DynamicIdeas(conn)
        self.assertEqual([row["kind"] for row in pool.all()], ["转发"])

    def test_evaluation_accepts_old_name(self):
        from core.video_evaluation import parse_dynamic_kind
        self.assertEqual(parse_dynamic_kind({"dynamic_kind": "搬运"}, True, ("分享", "转发")), "转发")
        with self.assertRaises(ValueError):
            parse_dynamic_kind({"dynamic_kind": "转述"}, True, ("分享", "转发"))


class ExternalIdeaPostTests(unittest.TestCase):
    """站外来的转述：提示词里写成网上看到的，发的时候程序补原链接，不当成视频记。"""
    _run = DynamicPoolPostTests._run

    def _external(self, pool, link="https://example.com/post/1"):
        return pool.add(key="feed:abc", kind="转述", source="feed", title="新版本发布", up_name="某项目更新",
                        angle="终于能导出了", summary="支持导出", link=link)

    def test_link_is_appended_and_not_logged_as_video(self):
        host = PostHost([])
        idea = self._external(host.pool)
        host.replies.append(_reply(idea, "等了好久的导出终于来了"))
        self._run(host, fresh=(idea,))
        self.assertIn("网上看到的", host.prompts[0])
        self.assertIn("原链接：https://example.com/post/1", host.prompts[0])
        self.assertEqual(host.sent, [("text", "等了好久的导出终于来了\nhttps://example.com/post/1")])
        entry = host.dynamic_log.entries()[0]
        self.assertEqual((entry["source"], entry["kind"], entry["link"]), ("external", "转述", "https://example.com/post/1"))
        self.assertNotIn("bvid", entry)

    def test_link_not_doubled_when_model_wrote_it(self):
        host = PostHost([])
        idea = self._external(host.pool)
        host.replies.append(_reply(idea, "看这个 https://example.com/post/1"))
        self._run(host, fresh=(idea,))
        self.assertEqual(host.sent, [("text", "看这个 https://example.com/post/1")])

    def test_followed_dynamic_goes_out_as_dynamic_repost(self):
        host = PostHost([])
        idea = host.pool.add(key="dyn:900", kind="转发", source="dynamic_watch", title="招募新成员",
                             up_name="某UP", angle="有兴趣的去试试", summary="某UP在招募新成员")
        host.replies.append(_reply(idea, "有兴趣的可以去看看"))
        self._run(host, fresh=(idea,))
        self.assertIn("关注的人发的动态", host.prompts[0])
        self.assertEqual(host.sent, [("repost_dynamic", "900", "有兴趣的可以去看看")])
        entry = host.dynamic_log.entries()[0]
        self.assertEqual((entry["source"], entry["dynamic_id"], entry["repost"]), ("followed_dynamic", "900", True))
        self.assertNotIn("bvid", entry)
        self.assertTrue(host.dynamic_log.has_key("dyn:900"))

    def test_old_table_gets_link_column(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE dynamic_ideas (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, kind TEXT NOT NULL,"
            " source TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', up_name TEXT NOT NULL DEFAULT '',"
            " pic TEXT NOT NULL DEFAULT '', score REAL, review TEXT NOT NULL DEFAULT '', angle TEXT NOT NULL DEFAULT '',"
            " summary TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',"
            " created_at REAL NOT NULL, state_at REAL NOT NULL)")
        conn.execute("INSERT INTO dynamic_ideas (key, kind, source, state, created_at, state_at)"
                     " VALUES ('BV1', '分享', 'proactive', 'pending', 0, 0)")
        pool = DynamicIdeas(conn)
        self.assertEqual(pool.all()[0]["link"], "")
        pool.add(key="feed:x", kind="转述", source="feed", link="https://a.b/c")
        self.assertEqual(pool.all()[1]["link"], "https://a.b/c")
