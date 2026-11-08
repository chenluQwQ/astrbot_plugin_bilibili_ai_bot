"""首页推荐（rcmd）视频池的接口参数回归测试。

B站 ``/x/web-interface/index/top/rcmd`` 对 ``ps`` 有上限：
``ps`` 超过 14 时该接口直接返回 ``-400 请求错误``，于是 rcmd 池永远拿不到视频，
且每轮主动看片都会多一条 WARN。这里把这个上限锁住，避免再次被调大。

实测（同一账号 Cookie，2026-09-27）：
    ps=12/13/14 -> code=0
    ps=15/18/20/24/30 -> code=-400
``fresh_type`` 0-4、``fresh_idx``、``fresh_idx_1h``、``version=1`` 均不影响。
"""

from base_parts import BaseParts
import ast
import asyncio
import logging
import random
import unittest
from pathlib import Path

# Load the production method without importing the AstrBot runtime. This keeps
# this regression test runnable on its own, not dependent on other tests' stubs.
_source = Path(__file__).resolve().parents[1] / "core" / "infra" / "bili" / "video_pools.py"
_tree = ast.parse(_source.read_text(encoding="utf-8"))
_class = next(node for node in _tree.body if isinstance(node, ast.ClassDef) and node.name == "BiliVideoPools")
_class.body = [node for node in _class.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_get_rcmd_videos"]
_namespace = {"random": random, "logger": logging.getLogger(__name__), "BiliApi": object}
exec(compile(ast.Module(body=[_class], type_ignores=[]), str(_source), "exec"), _namespace)
ProactiveMixin = _namespace["BiliVideoPools"]

RCMD_URL = "https://api.bilibili.com/x/web-interface/index/top/rcmd"
MAX_ACCEPTED_PS = 14


class RcmdProbe(ProactiveMixin, BaseParts):
    def __init__(self):
        self.config = {}
        self.calls = []

    async def _http_get(self, url, params=None, **_kwargs):
        self.calls.append((url, dict(params or {})))
        return {
            "code": 0,
            "data": {
                "item": [
                    {
                        "goto": "av",
                        "bvid": "BV1",
                        "title": "正常视频",
                        "desc": "",
                        "owner": {"name": "UP", "mid": 1},
                        "pubdate": 0,
                        "pic": "",
                        "stat": {"view": 1},
                        "tname": "动画",
                    },
                    {
                        "goto": "live",
                        "bvid": "BV2",
                        "title": "直播推荐位",
                        "owner": {"name": "主播", "mid": 2},
                        "stat": {"view": 0},
                    },
                ],
            },
        }, None


class RcmdPoolTests(unittest.TestCase):
    def test_page_size_stays_within_api_limit(self):
        probe = RcmdProbe()
        asyncio.run(probe._get_rcmd_videos())

        self.assertEqual(len(probe.calls), 1)
        url, params = probe.calls[0]
        self.assertEqual(url, RCMD_URL)
        self.assertIn("ps", params)
        self.assertGreater(params["ps"], 0)
        self.assertLessEqual(
            params["ps"],
            MAX_ACCEPTED_PS,
            "rcmd 接口 ps 超过 14 会返回 -400，视频池将永远为空",
        )

    def test_non_video_entries_are_filtered_out(self):
        probe = RcmdProbe()
        videos = asyncio.run(probe._get_rcmd_videos())
        self.assertEqual([video["bvid"] for video in videos], ["BV1"])


if __name__ == "__main__":
    unittest.main()
