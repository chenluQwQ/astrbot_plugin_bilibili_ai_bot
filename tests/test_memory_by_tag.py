"""B4d-4/5 按标签找记忆：按视频找走标签、这人在别处聊过的、看过这位 UP 的其他视频、查记忆工具带对象；
为视频发的动态挂视频标签、备忘录记 UP 时说看过他几个视频。"""
import math
import types
import unittest
from unittest.mock import AsyncMock

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.domain._memory_entry import MemoryEntry
from core.domain._memory_tags import POSTED_DYNAMIC
from core.domain.memory import Memories
from core.features._bili_lookups import BiliLookups
from core.features._memo_tool import MemoTool
from core.features.memory_recall import MemoryRecall
from core.features.reply_context import ReplyContext
from core.infra.tool_loop import ToolMiss
from core.settings import Settings

BV = "BV1xx411c7mD"
OTHER_BV = "BV1yy411c7mD"
CAT, DOG = [1.0, 0.0], [0.0, 1.0]


def _cosine(a, b):
    a, b = list(a), list(b)
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


class _Vision:
    async def _get_embedding(self, text):
        return CAT if "猫" in text else DOG

    _cosine_similarity = staticmethod(_cosine)


async def _host(records):
    entry = MemoryEntry(types.SimpleNamespace(settings=types.SimpleNamespace(int=lambda key: 0)))
    memories = Memories(
        prepare=entry._prepare_memory_entry, normalize=entry._normalize_memory_entry, layered=lambda: None,
        load_state=lambda: {}, save_state=lambda state: None, save_backup=lambda records: None,
    )
    for record in records:
        await memories.save(record)
    host = types.SimpleNamespace(memories=memories, memory_entry=entry, vision=_Vision())
    host.memory_recall = MemoryRecall(host)
    return host


def _chat(rpid, text, *, uid="42", thread="t-other", oid="900", emb=CAT, source="bilibili", **extra):
    return {"rpid": rpid, "text": text, "user_id": uid, "thread_id": thread, "oid": oid, "source": source,
            "embedding": emb, "time": f"2026-11-0{rpid[-1]} 10:00", **extra}


def _video(rpid, bvid, up="777", day=1):
    return {"rpid": rpid, "text": f"看了{bvid}", "memory_type": "video", "bvid": bvid, "owner_mid": up,
            "user_id": "self", "source": "proactive", "time": f"2026-11-0{day} 10:00"}


class RecallByTagTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_lookup_uses_the_video_tag(self):
        host = await _host([_video("v1", BV), _video("v2", OTHER_BV),
                            {"rpid": "w1", "text": "在视频页看过", "thread_id": f"video:{BV}", "user_id": "self",
                             "memory_type": "video", "source": "proactive"}])
        found = await host.memory_recall._get_bvid_memories(BV)
        self.assertEqual(len(found), 2)
        self.assertTrue(all(BV in row or "在视频页看过" in row for row in found))

    async def test_elsewhere_skips_this_thread_and_area_and_ranks_by_meaning(self):
        host = await _host([
            _chat("c1", "聊过猫", thread="t-here", oid="100"),
            _chat("c2", "在这个评论区聊过猫", thread="t-x", oid="100"),
            _chat("c3", "别处聊过猫"),
            _chat("c4", "别处聊过狗", emb=DOG),
            _chat("c5", "别人聊猫", uid="43"),
        ])
        found = await host.memory_recall._get_user_elsewhere_memories(
            "42", "猫怎么样", exclude_thread_id="t-here", exclude_oid="100")
        self.assertEqual([m["rpid"] for m in found], ["c3"])

    async def test_elsewhere_keeps_private_chats_out_of_comments(self):
        host = await _host([_chat("c1", "私信聊过猫", thread="private:42", oid="", source="bilibili_private")])
        recall = host.memory_recall
        self.assertEqual(await recall._get_user_elsewhere_memories("42", "猫", reader_scope="bili_comment"), [])
        self.assertEqual([m["rpid"] for m in await recall._get_user_elsewhere_memories("42", "猫", reader_scope="bili_dm")],
                         ["c1"])

    async def test_up_videos_are_newest_first_without_the_current_one(self):
        host = await _host([_video(f"v{i}", f"BV1{i}x411c7mD", day=i) for i in range(1, 6)] + [_video("v9", BV, day=9)])
        found = await host.memory_recall._get_up_video_memories("777", exclude_bvid=BV)
        self.assertEqual([m["rpid"] for m in found], ["v5", "v4", "v3"])


class ReplyContextBlockTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_blocks_show_up_and_global_does_not_repeat_them(self):
        recall = types.SimpleNamespace(
            _reader_scope_for_channel=lambda _: "bili_comment",
            _get_bvid_memories=AsyncMock(return_value=[]), _get_thread_memories=AsyncMock(return_value=[]),
            _get_oid_memories=AsyncMock(return_value=[]), _search_global_relevant=AsyncMock(return_value=[]),
            _get_user_elsewhere_memories=AsyncMock(return_value=[{"rpid": "c3", "text": "别处聊过猫"}]),
            _get_up_video_memories=AsyncMock(return_value=[{"rpid": "v5", "text": "看了UP的别的视频"}]),
            _format_memory_with_meta=MemoryRecall._format_memory_with_meta,
        )
        bot = types.SimpleNamespace(
            settings=Settings({"DEDE_USER_ID": "1"}), json_store=types.SimpleNamespace(_load_json=lambda *_: []),
            states=types.SimpleNamespace(permanent_memory=types.SimpleNamespace(read=lambda *_: [])),
            video_watch=types.SimpleNamespace(_get_comment_video_memory_context=AsyncMock(
                return_value=("视频记忆", {"bvid": BV, "owner_mid": "777"}))),
            memory_recall=recall, user_profiles=types.SimpleNamespace(context=lambda *a: ""),
        )
        text = await ReplyContext(bot)._build_memory_context("t-here", "42", "猫", oid=100)
        self.assertIn("【看过这位UP主的其他视频】", text)
        self.assertIn("看了UP的别的视频", text)
        self.assertIn("【这位用户以前在别处和你聊过的】", text)
        self.assertEqual(recall._get_up_video_memories.await_args.kwargs["exclude_bvid"], BV)
        self.assertEqual(recall._search_global_relevant.await_args.kwargs["exclude_rpids"], {"c3", "v5"})


class MemoryToolTargetTests(unittest.IsolatedAsyncioTestCase):
    async def lookups(self, records):
        host = await _host(records)
        host.bangumi_memory = types.SimpleNamespace(all=lambda: {"7": {"title": "某番", "season_id": 7, "total_watched": 2,
                                                                        "last_ep_index": 2, "last_score": 8, "episodes": []}})
        return BiliLookups(host)

    async def test_target_without_keyword_lists_the_newest(self):
        lookups = await self.lookups([_video("v1", BV), _video("v2", OTHER_BV, up="888")])
        text = await lookups.memory("video", "", reader_scope="bili_comment", target="777")
        self.assertIn(BV, text)
        self.assertNotIn(OTHER_BV, text)
        self.assertIn(OTHER_BV, await lookups.memory("video", "", reader_scope="bili_comment",
                                                     target=f"https://b23.tv/{OTHER_BV}"))

    async def test_followed_anime_title_or_ss_finds_its_memories(self):
        ep = {"rpid": "b1", "text": "Bot看了番剧《某番》第2话", "memory_type": "bangumi", "season_id": "7",
              "user_id": "self", "source": "bangumi"}
        lookups = await self.lookups([ep])
        for target in ("某番", "ss7", "7"):
            text = await lookups.memory("bangumi", "", reader_scope="bili_comment", target=target)
            self.assertIn("已看2集", text)
            self.assertIn("第2话", text)

    async def test_unknown_target_is_explained(self):
        lookups = await self.lookups([])
        with self.assertRaises(ToolMiss) as caught:
            await lookups.memory("video", "", reader_scope="bili_comment", target="某个昵称")
        self.assertIn("UID", caught.exception.detail)
        with self.assertRaises(ToolMiss):
            await lookups.memory("video", "", reader_scope="bili_comment")


class DynamicForVideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_dynamic_is_found_with_the_video_but_does_not_count_as_watching_it(self):
        host = await _host([
            _video("v1", BV),
            {"rpid": "d1", "text": "Bot发了一条动态：这个视频好好笑", "memory_type": "dynamic", "thread_id": "dynamic",
             "user_id": "self", "tags": [f"视频:{BV}"]},
        ])
        self.assertEqual(await host.memories.add_tag(POSTED_DYNAMIC, tag=f"视频:{BV}", types={"video"}), 1)
        self.assertEqual(await host.memories.add_tag(POSTED_DYNAMIC, tag=f"视频:{BV}", types={"video"}), 0)
        found = await host.memory_recall._get_bvid_memories(BV)
        self.assertTrue(any("好好笑" in row for row in found))
        self.assertEqual([m["rpid"] for m in await host.memories.find(tag=POSTED_DYNAMIC)], ["v1"])
        # 「看过这个视频」的判断按 bvid 字段，动态没写，不算
        self.assertEqual([m["rpid"] for m in await host.memories.find(bvid=BV)], ["v1"])


class MemoUpHintTests(unittest.IsolatedAsyncioTestCase):
    async def tool(self, records):
        host = await _host(records)
        host.settings = Settings({"ENABLE_MEMO": True})
        host.roles = types.SimpleNamespace(is_owner=lambda uid: False)
        host.affection = types.SimpleNamespace(score=lambda uid: 0)
        host.seen_videos = types.SimpleNamespace(has_seen=AsyncMock(return_value=False))
        return MemoTool(host)

    async def record(self, tool, target):
        return await tool.record(kind="up", target=target, note="", when="someday", channel="comment", uid="42", name="小柠")

    async def test_counts_distinct_videos_by_uid_or_known_nickname(self):
        rewatch = {**_video("v3", BV), "owner_name": "某UP"}
        tool = await self.tool([{**_video("v1", BV), "owner_name": "某UP"}, _video("v2", OTHER_BV), rewatch])
        self.assertIn("看过这位UP主 2 个视频", await self.record(tool, "777"))
        self.assertIn("看过这位UP主 2 个视频", await self.record(tool, "某up"))

    async def test_unknown_nickname_says_nothing_extra(self):
        tool = await self.tool([])
        self.assertEqual(await self.record(tool, "没见过的人"), "记下了（备忘 #1）。")
        self.assertIn("还没看过", await self.record(tool, "123"))


if __name__ == "__main__":
    unittest.main()
