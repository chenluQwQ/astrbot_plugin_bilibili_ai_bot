"""弹幕小结：去重（标点、叠字刷屏、笑声、字差不多的并一起）、多了先滤掉无关的、挑的段不相邻、写成「出现了60多条」。"""
import unittest

import test_autonomous_ranges  # noqa: F401  先装上假的 astrbot
from core.domain import danmaku as dm
from core.infra.bili.bangumi import parse_danmaku_xml


class DanmakuTests(unittest.TestCase):
    def test_parse_xml(self):
        xml = '<i><d p="12.5,1,25">好帅&amp;好看</d><d p="bad">x</d><d p="3.0,1">来了</d></i>'
        self.assertEqual(parse_danmaku_xml(xml), [(3.0, "来了"), (12.5, "好帅&好看")])

    def test_normalize_and_merge(self):
        self.assertEqual(dm.normalize("哈哈哈哈哈"), "哈哈哈")
        self.assertEqual(dm.normalize("hhhh"), "哈哈哈")
        self.assertEqual(dm.normalize("打铁～"), dm.normalize("打铁~"))
        self.assertEqual(dm.normalize("梦开始的地方梦开始的地方梦开始的地方"), "梦开始的地方")
        merged = dict(dm.merge(["梦开始的地方"] * 5 + ["梦的开始"] * 2 + ["打铁~", "打铁～", "打铁"]))
        self.assertEqual(merged, {"梦开始的地方": 7, "打铁": 3})

    def test_rough_counts(self):
        self.assertEqual([dm.rough(n) for n in (3, 60, 63, 148)], ["3条", "60条", "60多条", "140多条"])

    def test_useless_filtered_only_when_many(self):
        self.assertTrue(dm.useless("666") and dm.useless("打卡") and dm.useless("2024年还在看") and dm.useless("？？"))
        self.assertFalse(dm.useless("琴酒好帅"))
        few = [(i, "666") for i in range(30)]
        self.assertIn("666", dm.danmaku_highlights(few))  # 少的时候不滤
        many = [(i * 0.1, "666") for i in range(250)] + [(100 + i * 0.1, "琴酒好帅") for i in range(30)]
        out = dm.danmaku_highlights(many)
        self.assertNotIn("666", out.split("。", 1)[1])
        self.assertIn("「琴酒好帅」出现了30条", out)
        self.assertIn("去掉打卡、刷数字这些和剧情无关的 250 条", out)

    def test_hot_windows_not_adjacent_and_capped(self):
        items = [(s, f"片头{s % 3}") for s in range(0, 90)] * 3 + [(600 + s, "反转了") for s in range(20)]
        out = dm.danmaku_highlights(sorted(items))
        self.assertIn("10:00-10:30", out)
        self.assertNotIn("00:30-01:00", out)  # 挨着 00:00 那段，不选
        self.assertLessEqual(len(out), dm.MAX_CHARS)

    def test_too_few_is_empty(self):
        self.assertEqual(dm.danmaku_highlights([(1, "好")] * 5), "")


if __name__ == "__main__":
    unittest.main()
