"""bili_recall 按昵称找用户：原来 ``profiles`` 没定义，按昵称查一律报错。"""
import types
import unittest

from core.settings import Settings
from core.tools import recall
from core.tools._kit import ToolKit


class Profiles:
    def __init__(self, data):
        self.data = data

    def __contains__(self, uid):
        return uid in self.data

    def raw(self, uid):
        return dict(self.data.get(uid, {}))

    def all(self):
        return self.data


class RecallUserByNameTests(unittest.IsolatedAsyncioTestCase):
    def tool(self):
        plugin = types.SimpleNamespace(
            settings=Settings({}),
            user_profiles=Profiles({
                "42": {"username": "小明同学", "impression": "爱看木工"},
                "43": {"username": "别人"},
            }),
            affection=types.SimpleNamespace(known=lambda uid: False, score=lambda uid: 12),
        )
        RecallBiliTool, = recall.build(ToolKit(plugin))
        return RecallBiliTool()

    async def test_name_search_lists_matching_users(self):
        result = await self.tool().call(None, memory_type="user", query="小明")
        self.assertIn("找到1个匹配", result)
        self.assertIn("UID:42 小明同学 12分 爱看木工", result)

    async def test_unknown_name_says_not_found(self):
        result = await self.tool().call(None, memory_type="user", query="没有这人")
        self.assertIn("没有找到用户「没有这人」的记录", result)


if __name__ == "__main__":
    unittest.main()
