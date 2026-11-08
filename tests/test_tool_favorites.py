"""search_bilibili(search_type=favorites)：按 UID 或名字看公开收藏夹。"""
import types
import unittest
from bili_parts import attach_bili_groups
from unittest.mock import AsyncMock

from core.settings import Settings
from core.tools import create_tools


FOLDERS = [{"id": 11, "title": "默认收藏夹", "count": 3}, {"id": 12, "title": "猫猫", "count": 1}]
VIDEOS = {
    11: [{"bvid": "BV1aa", "title": "做饭", "up_name": "厨子", "fav_time": 1700000000}],
    12: [{"bvid": "BV1bb", "title": "猫咪合集", "up_name": "猫奴", "fav_time": 1700000000}],
}


class FavoritesLookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = types.SimpleNamespace(config={"OWNER_MID": "42", "DEDE_USER_ID": "900"}, _load_json=lambda *_: {})
        self.plugin.settings = Settings(self.plugin.config)
        attach_bili_groups(self.plugin)
        self.plugin.bili_videos._get_public_fav_folders = AsyncMock(
            side_effect=lambda mid, limit=3: FOLDERS if str(mid) == "7" else [])
        self.plugin.bili_videos._get_fav_folder_videos = AsyncMock(side_effect=lambda media_id, ps=20: VIDEOS[media_id])
        self.plugin.bili_users.search_bilibili_users = AsyncMock(return_value=[{"uname": "小明", "mid": 7, "fans": 1, "videos": 0}])
        tool = {t.name: t for t in create_tools(self.plugin)}["search_bilibili"]
        # 绕过授权层，只测查询本身；授权由 test_tool_execution_security 守
        self.search = tool.__class__.call.__get__(tool)

    async def test_lists_folders_and_opens_the_first(self):
        text = await self.search(None, search_type="favorites", keyword="7")
        self.assertIn("「默认收藏夹」3个视频", text)
        self.assertIn("「猫猫」1个视频", text)
        self.assertIn("《做饭》by 厨子 | BV1aa", text)
        self.assertNotIn("猫咪合集", text)

    async def test_name_resolves_and_folder_picks_by_title(self):
        text = await self.search(None, search_type="favorites", keyword="小明", folder="猫")
        self.assertIn("《猫咪合集》by 猫奴 | BV1bb", text)
        self.plugin.bili_videos._get_public_fav_folders.assert_awaited_with("7", limit=10)

    async def test_unknown_folder_lists_what_exists(self):
        text = await self.search(None, search_type="favorites", keyword="7", folder="游戏")
        self.assertIn("没有「游戏」", text)
        self.assertIn("「默认收藏夹」、「猫猫」", text)

    async def test_no_public_folders(self):
        text = await self.search(None, search_type="favorites", keyword="8")
        self.assertIn("没有公开的收藏夹", text)


if __name__ == "__main__":
    unittest.main()
