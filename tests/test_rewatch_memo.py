"""没看成的视频（直读、截图都失败，也没字幕）：记一条备忘，第二天补看一次，只补这一次。"""
import asyncio
import types
import unittest

from test_autonomous_ranges import ScheduleProbe  # noqa: F401  先装上假的 astrbot
from core.domain.memos import REWATCH_NOTE, Memos
from core.features._watch_video import WatchVideo
from core.features.memo_watch import MemoWatch

BV = "BV1REWATCH01"


class _MemoTool:
    def __init__(self, on=True):
        self.on, self.pool = on, Memos.in_memory()

    def enabled(self):
        return self.on

    def _memos(self):
        return self.pool


class RewatchMemoTests(unittest.TestCase):
    def test_failed_watch_adds_one_memo_for_tomorrow(self):
        tool = _MemoTool()
        watch = WatchVideo(types.SimpleNamespace(memo_tool=tool))
        watch._schedule_rewatch({"bvid": BV}, {"bvid": BV})
        watch._schedule_rewatch({"bvid": BV}, {"bvid": BV})  # 同一个视频不重复记
        memos = tool.pool.pending()
        self.assertEqual(len(memos), 1)
        self.assertEqual(memos[0]["note"], REWATCH_NOTE)
        self.assertEqual(memos[0]["source"], "self")

    def test_rewatch_itself_or_memo_off_adds_nothing(self):
        tool = _MemoTool()
        WatchVideo(types.SimpleNamespace(memo_tool=tool))._schedule_rewatch({"bvid": BV, "_rewatch_retry": True}, {"bvid": BV})
        off = _MemoTool(on=False)
        WatchVideo(types.SimpleNamespace(memo_tool=off))._schedule_rewatch({"bvid": BV}, {"bvid": BV})
        WatchVideo(types.SimpleNamespace())._schedule_rewatch({"bvid": BV}, {"bvid": BV})  # 没有备忘录也不报错
        self.assertEqual(tool.pool.pending(), [])
        self.assertEqual(off.pool.pending(), [])

    def test_rewatch_memo_bypasses_seen_check_and_cache(self):
        tool = _MemoTool()
        memo_id, _ = tool.pool.add(kind="video", target=BV, note=REWATCH_NOTE, source="self", unlimited=True)

        async def oid(_bvid):
            return 1

        async def info(_oid):
            return {"bvid": BV, "title": "没看成的", "desc": "", "owner_name": "UP", "owner_mid": "9"}

        host = types.SimpleNamespace(
            memo_tool=tool,
            bili_videos=types.SimpleNamespace(_get_video_oid=oid, _get_video_info=info),
            video_discovery=types.SimpleNamespace(_tag_video_source=lambda video, source, detail: {**video, "_source": source}),
        )
        videos = asyncio.run(MemoWatch(host).video_candidates(memo_id, {BV}, 3))
        self.assertEqual(len(videos), 1)
        self.assertTrue(videos[0]["_force_rewatch"])
        self.assertTrue(videos[0]["_rewatch_retry"])
        self.assertEqual(tool.pool.get(memo_id)["state"], "pending")

    def test_ordinary_memo_for_seen_video_still_finishes(self):
        tool = _MemoTool()
        memo_id, _ = tool.pool.add(kind="video", target=BV, note="别人推荐的", source="self", unlimited=True)
        host = types.SimpleNamespace(memo_tool=tool)
        self.assertEqual(asyncio.run(MemoWatch(host).video_candidates(memo_id, {BV}, 3)), [])
        self.assertNotEqual(tool.pool.get(memo_id)["state"], "pending")


if __name__ == "__main__":
    unittest.main()
