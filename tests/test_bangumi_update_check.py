"""每天第一个番剧时刻：查追番更新，没有更新就照常主动看番，不让这个时刻落空。"""
from base_parts import BaseParts
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from core.domain.bangumi_memory import BangumiMemory
from core.features.bangumi import Bangumi
from itself import itself


class MemoryFile:
    def __init__(self, data):
        self.data = data

    def read(self, default=None):
        return self.data


class UpdateBot(BaseParts):
    bili_bangumi = itself

    def __init__(self, followed, memory=None, detail=None, enabled=True, cookie=True):
        self.settings = {"ENABLE_BANGUMI": enabled, "BANGUMI_EPISODE_COUNT": 2}
        self.bangumi_memory = BangumiMemory(MemoryFile(memory or {}))
        self._get_followed_bangumi = AsyncMock(return_value=followed)
        self.get_bangumi_detail = AsyncMock(return_value=detail)
        self.bili_http._has_cookie = MagicMock(return_value=cookie)


WATCHED = {"7": {"episodes": [{"ep_id": 101}]}}
SEASON = {"season_id": 7, "title": "测试番"}


class BangumiUpdateCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sleeper = patch("core.features.bangumi.asyncio.sleep", AsyncMock())
        sleeper.start()
        self.addCleanup(sleeper.stop)
        runner = patch.object(Bangumi, "run", AsyncMock())
        self.run_bangumi = runner.start()
        self.addCleanup(runner.stop)

    async def test_nothing_followed_falls_back_to_proactive_watch(self):
        await Bangumi(UpdateBot([])).check_updates()
        self.run_bangumi.assert_awaited_once_with()

    async def test_all_up_to_date_falls_back_to_proactive_watch(self):
        await Bangumi(UpdateBot([SEASON], memory=WATCHED, detail={"episodes": [{"ep_id": 101}]})).check_updates()
        self.run_bangumi.assert_awaited_once_with()

    async def test_update_watches_that_season(self):
        await Bangumi(UpdateBot([SEASON], memory=WATCHED, detail={"episodes": [{"ep_id": 101}, {"ep_id": 102}]})).check_updates()
        self.run_bangumi.assert_awaited_once_with(season_id=7, max_episodes=2)

    async def test_disabled_or_logged_out_watches_nothing(self):
        for kwargs in ({"enabled": False}, {"cookie": False}):
            with self.subTest(**kwargs):
                await Bangumi(UpdateBot([], **kwargs)).check_updates()
                self.run_bangumi.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
