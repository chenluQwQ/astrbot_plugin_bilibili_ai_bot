"""回复前查资料的共用查询（A1-5）：查 UP 的几种看法、按 BV 查视频、番剧搜索、Bot 自己的记忆和读的人所在的域。"""
import types
import unittest
from unittest.mock import AsyncMock

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.features._bili_lookups import BiliLookups
from core.infra.tool_loop import ToolMiss
from core.settings import Settings


def host(**overrides):
    users = types.SimpleNamespace(
        search_bilibili_users=AsyncMock(return_value=[
            {"uname": "泛式", "mid": 1, "fans": 10, "videos": 2},
            {"uname": "泛式本人", "mid": 2, "fans": 1, "videos": 1},
        ]),
        get_up_info=AsyncMock(return_value={"sign": "签名", "official_title": "", "live_status": 1,
                                            "live_title": "杂谈", "live_url": "https://live.bilibili.com/1"}),
        get_up_recent_videos=AsyncMock(return_value=[{"bvid": "BV1xx411c7mD", "title": "新视频", "play": 9, "created": 0}]),
    )
    fake = types.SimpleNamespace(
        settings=Settings({}),
        bili_users=users,
        bili_dynamics=types.SimpleNamespace(get_up_recent_dynamics=AsyncMock(return_value=[{"pub_time": "昨天", "text": "动态正文"}])),
        bili_videos=types.SimpleNamespace(
            _get_video_oid=AsyncMock(return_value=123),
            _get_video_info=AsyncMock(return_value={"bvid": "BV1xx411c7mD", "title": "标题", "owner_name": "UP", "desc": "简介"}),
        ),
        bili_comments=types.SimpleNamespace(_get_hot_comments=AsyncMock(return_value=["热评一"])),
        bili_bangumi=types.SimpleNamespace(search_bilibili_bangumi=AsyncMock(return_value=[
            {"title": "某番", "season_id": 7, "score": 9.5, "ep_size": 12, "desc": "介绍"}])),
        bangumi_memory=types.SimpleNamespace(all=lambda: {"7": {"title": "某番", "total_watched": 3, "last_ep_index": 3,
                                                                 "last_score": 8, "episodes": [{"ep_index": 3, "review": "好看"}]}}),
        memory_recall=types.SimpleNamespace(_search_memories=AsyncMock(return_value=["[10-01] 看过某番的视频"])),
    )
    vars(fake).update(overrides)
    return fake


class UpTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_name_wins_and_profile_lists_recent_uploads(self):
        fake = host()
        text = await BiliLookups(fake).up("泛式")
        self.assertIn("UID：1", text)
        self.assertIn("签名", text)
        self.assertIn("最近投稿", text)
        fake.bili_users.get_up_recent_videos.assert_awaited_once_with("1", ps=5, order="pubdate")
        self.assertNotIn("候选", text)

    async def test_ambiguous_name_lists_candidates(self):
        text = await BiliLookups(host()).up("泛")
        self.assertIn("其他候选：泛式本人(UID:2)", text)

    async def test_uid_skips_search(self):
        fake = host()
        await BiliLookups(fake).up("12345", "热门")
        fake.bili_users.search_bilibili_users.assert_not_awaited()
        fake.bili_users.get_up_recent_videos.assert_awaited_once_with("12345", ps=5, order="click")
        fake.bili_users.get_up_info.assert_not_awaited()

    async def test_dynamics_and_live(self):
        lookups = BiliLookups(host())
        self.assertIn("动态正文", await lookups.up("1", "动态"))
        self.assertIn("正在直播：《杂谈》", await lookups.up("1", "直播"))

    async def test_not_found_is_a_miss_with_a_reason(self):
        fake = host()
        fake.bili_users.search_bilibili_users.return_value = []
        with self.assertRaises(ToolMiss) as caught:
            await BiliLookups(fake).up("不存在")
        self.assertIn("没有找到", caught.exception.detail)


class VideoAndBangumiTests(unittest.IsolatedAsyncioTestCase):
    async def test_bv_inside_chinese_text(self):
        fake = host()
        text = await BiliLookups(fake).video("帮我看看BV1xx411c7mD讲了啥")
        fake.bili_videos._get_video_oid.assert_awaited_once_with("BV1xx411c7mD")
        fake.bili_comments._get_hot_comments.assert_awaited_once_with(123, limit=5)
        self.assertIn("热评一", text)
        self.assertIn("没看过不要说看过", text)

    async def test_bad_bv_is_a_miss(self):
        with self.assertRaises(ToolMiss):
            await BiliLookups(host()).video("BV123")

    async def test_bangumi_search_gives_season_id(self):
        self.assertIn("season_id 7", await BiliLookups(host()).bangumi_search("某番"))


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_scope_is_passed_through(self):
        fake = host()
        await BiliLookups(fake).memory("video", "手绘", reader_scope="bili_comment")
        kwargs = fake.memory_recall._search_memories.await_args.kwargs
        self.assertEqual((kwargs["reader_scope"], kwargs["memory_types"]), ("bili_comment", {"video"}))

    async def test_bangumi_reads_the_watch_record_too(self):
        text = await BiliLookups(host()).memory("bangumi", "某番", reader_scope="bili_dm")
        self.assertIn("已看3集", text)
        self.assertIn("好看", text)

    async def test_only_own_experience_kinds(self):
        fake = host()
        await BiliLookups(fake).memory("chat", "x", reader_scope="bili_comment")
        self.assertEqual(fake.memory_recall._search_memories.await_args.kwargs["memory_types"], {"video"})

    async def test_nothing_found_is_a_miss(self):
        fake = host()
        fake.memory_recall._search_memories.return_value = []
        with self.assertRaises(ToolMiss) as caught:
            await BiliLookups(fake).memory("dynamic", "没有的", reader_scope="bili_comment")
        self.assertIn("动态", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
