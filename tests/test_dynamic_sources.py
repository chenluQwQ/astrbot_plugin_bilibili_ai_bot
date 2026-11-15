"""站外来源：解析订阅、挑选结果严格校验、一天最多刷两次、看过的不再给、挑中的进池子。"""
import asyncio
import json
import unittest
from datetime import datetime

import test_autonomous_ranges  # noqa: F401  先装上假的 astrbot
from core.domain.dynamic_ideas import DynamicIdeas
from core.features import dynamic_sources as ds

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Some Project</title>
<item><title>v2.1 released</title><link>https://example.com/v21</link><description>&lt;p&gt;Adds export&lt;/p&gt;</description><guid>v21</guid></item>
<item><title>v2.0 released</title><link>https://example.com/v20</link><description>Big rewrite</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>News</title>
<entry><title>Hello</title><id>tag:1</id><link rel="alternate" href="https://news.example/1"/><summary>World</summary></entry>
</feed>"""


class ParseFeedTests(unittest.TestCase):
    def test_rss(self):
        name, items = ds.parse_feed(RSS)
        self.assertEqual(name, "Some Project")
        self.assertEqual([(i["title"], i["link"], i["summary"]) for i in items],
                         [("v2.1 released", "https://example.com/v21", "Adds export"),
                          ("v2.0 released", "https://example.com/v20", "Big rewrite")])

    def test_atom(self):
        name, items = ds.parse_feed(ATOM)
        self.assertEqual((name, items[0]["link"], items[0]["guid"]), ("News", "https://news.example/1", "tag:1"))

    def test_limit(self):
        self.assertEqual(len(ds.parse_feed(RSS, limit=1)[1]), 1)

    def test_refuses_doctype_and_junk(self):
        for raw in (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss><channel/></rss>',
                    b"not xml", b"<html><body/></html>"):
            with self.assertRaises(ValueError):
                ds.parse_feed(raw)

    def test_stops_after_limit_even_if_rest_is_cut_off(self):
        entry = b'<entry><title>E%d</title><id>%d</id><link href="https://x.example/%d"/><content>' + b"x" * 1000 + b"</content></entry>"
        raw = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Big</title>' + b"".join(entry % (n, n, n) for n in range(8))
        reader = ds.FeedReader(limit=5)
        for start in range(0, len(raw), 333):
            if reader.feed(raw[start:start + 333]):
                break
        name, items = reader.result()
        self.assertEqual((name, [i["title"] for i in items]), ("Big", ["E0", "E1", "E2", "E3", "E4"]))
        # 截断在半路、已经有几条了：照样用
        name, items = ds.parse_feed(raw[:len(raw) // 2] + b"<<<", limit=10)
        self.assertGreater(len(items), 0)

    def test_non_http_link_dropped(self):
        raw = RSS.replace(b"https://example.com/v21", b"javascript:alert(1)")
        self.assertEqual(ds.parse_feed(raw)[1][0]["link"], "")


CANDIDATES = [
    {"key": "feed:1", "source": "feed", "from": "P", "title": "t1", "summary": "s1", "link": "https://a.example/1"},
    {"key": "search:2", "source": "search", "from": "搜索「k」", "title": "k", "summary": "看 https://b.example/2 这个", "link": ""},
]


def _picks(*items):
    return json.dumps({"picks": [dict(zip(("id", "angle", "link"), item)) for item in items]}, ensure_ascii=False)


class ParsePicksTests(unittest.TestCase):
    def test_empty_is_fine(self):
        self.assertEqual(ds.parse_picks('{"picks": []}', CANDIDATES), [])

    def test_feed_link_comes_from_the_feed(self):
        [(item, angle, link)] = ds.parse_picks(_picks((1, "好", "https://evil.example")), CANDIDATES)
        self.assertEqual((item["key"], angle, link), ("feed:1", "好", "https://a.example/1"))

    def test_search_link_must_be_in_material(self):
        self.assertEqual(ds.parse_picks(_picks((2, "好", "https://b.example/2")), CANDIDATES)[0][2], "https://b.example/2")
        self.assertEqual(ds.parse_picks(_picks((2, "好", "")), CANDIDATES)[0][2], "")
        with self.assertRaises(ValueError):
            ds.parse_picks(_picks((2, "好", "https://made.up/x")), CANDIDATES)

    def test_strict(self):
        bad = [
            '{"picks": [], "x": 1}', '[]', _picks((3, "好", "")), _picks((1, "好", ""), (1, "又", "")),
            _picks((1, "", "")), _picks((1, "长" * 51, "")), _picks((True, "好", "")),
            '{"picks": [{"id": 1, "angle": "好"}]}', _picks((1, "a", ""), (2, "b", ""), (1, "c", "")),
        ]
        for raw in bad:
            with self.assertRaises((ValueError, TypeError), msg=raw):
                ds.parse_picks(raw, CANDIDATES)


class _Db:
    is_open = True

    def __init__(self):
        self.kv = {}

    async def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    async def kv_set(self, key, value, ttl=None):
        self.kv[key] = json.loads(json.dumps(value))


class _Models:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    async def _llm_call(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else '{"picks": []}'

    async def _get_system_prompt(self):
        return "人设"


class _Search:
    def __init__(self):
        self.queries = []

    async def _web_search(self, query):
        self.queries.append(query)
        return f"关于{query}的结果 https://r.example/{len(self.queries)}"


class _Post:
    def __init__(self):
        self.pool = DynamicIdeas.in_memory()

    def collect(self, idea):
        return self.pool.add(**idea)


class _Host:
    def __init__(self, settings, replies=()):
        self.settings = {"ENABLE_DYNAMIC": True, **settings}
        self.config = {}
        self.layered_runtime = type("L", (), {"db": _Db()})()
        self.models = _Models(replies)
        self.web_search = _Search()
        self.dynamic_post = _Post()


class _Sources(ds.DynamicSources):
    feeds = {"https://feed.example/rss": RSS}

    async def _chunks(self, url):
        if url not in self.feeds:
            raise ValueError("HTTP 404")
        body = self.feeds[url]
        for start in range(0, len(body), 50):  # 切碎了喂，测边下边解析
            yield body[start:start + 50]


def _run(host, when):
    asyncio.run(_Sources(host).maybe_refresh(when))


class RefreshTests(unittest.TestCase):
    MORNING = datetime(2026, 11, 12, 9, 0)

    def test_nothing_configured_does_nothing(self):
        host = _Host({})
        _run(host, self.MORNING)
        self.assertEqual(host.models.prompts, [])
        self.assertEqual(host.layered_runtime.db.kv, {})

    def test_feed_pick_goes_into_pool_as_retelling(self):
        host = _Host({"DYNAMIC_FEED_URLS": ["https://feed.example/rss"]}, [_picks((1, "导出终于来了", ""))])
        _run(host, self.MORNING)
        self.assertIn('trust="untrusted"', host.models.prompts[0])
        self.assertIn("v2.1 released", host.models.prompts[0])
        [idea] = host.dynamic_post.pool.pending()
        self.assertEqual((idea["kind"], idea["source"], idea["link"], idea["up_name"], idea["angle"]),
                         ("转述", "feed", "https://example.com/v21", "Some Project", "导出终于来了"))

    def test_seen_items_are_not_offered_again(self):
        host = _Host({"DYNAMIC_FEED_URLS": ["https://feed.example/rss"]})
        _run(host, self.MORNING)
        _run(host, self.MORNING.replace(hour=15))
        self.assertEqual(len(host.models.prompts), 1)  # 第二次没有新条目，不调模型

    def test_at_most_two_refreshes_a_day_with_a_gap(self):
        host = _Host({"DYNAMIC_SEARCH_KEYWORDS": ["AI 新闻"], "ENABLE_WEB_SEARCH": True, "WEB_SEARCH_API_KEY": "k",
                      "DYNAMIC_FEED_URLS": ["https://feed.example/rss"]})
        runs = lambda: host.layered_runtime.db.kv["dynamic_sources:runs"]["count"]
        _run(host, self.MORNING)
        _run(host, self.MORNING.replace(hour=10))  # 不到 4 小时
        self.assertEqual(runs(), 1)
        _run(host, self.MORNING.replace(hour=14))
        _run(host, self.MORNING.replace(hour=20))
        self.assertEqual(runs(), 2)
        self.assertEqual(host.web_search.queries, ["AI 新闻"])  # 关键词一天只搜一次
        _run(host, datetime(2026, 11, 13, 9, 0))
        self.assertEqual(host.web_search.queries, ["AI 新闻", "AI 新闻"])

    def test_keywords_need_web_search(self):
        host = _Host({"DYNAMIC_SEARCH_KEYWORDS": ["AI 新闻"]})
        _run(host, self.MORNING)
        self.assertEqual((host.web_search.queries, host.models.prompts), ([], []))

    def test_search_pick_keeps_link_from_results(self):
        host = _Host({"DYNAMIC_SEARCH_KEYWORDS": ["AI 新闻"], "ENABLE_WEB_SEARCH": True, "WEB_SEARCH_API_KEY": "k"},
                     [_picks((1, "这个有意思", "https://r.example/1"))])
        _run(host, self.MORNING)
        [idea] = host.dynamic_post.pool.pending()
        self.assertEqual((idea["source"], idea["link"]), ("search", "https://r.example/1"))

    def test_bad_reply_adds_nothing_but_marks_seen(self):
        host = _Host({"DYNAMIC_FEED_URLS": ["https://feed.example/rss"]}, ["不是JSON"])
        _run(host, self.MORNING)
        self.assertEqual(host.dynamic_post.pool.pending(), [])
        self.assertEqual(len(host.layered_runtime.db.kv["dynamic_sources:seen"]), 2)

    def test_bad_feed_url_is_skipped(self):
        host = _Host({"DYNAMIC_FEED_URLS": ["file:///etc/passwd", "https://feed.example/missing"]})
        _run(host, self.MORNING)
        self.assertEqual(host.models.prompts, [])

    def test_dynamic_off_does_nothing(self):
        host = _Host({"ENABLE_DYNAMIC": False, "DYNAMIC_FEED_URLS": ["https://feed.example/rss"]})
        _run(host, self.MORNING)
        self.assertEqual(host.models.prompts, [])


if __name__ == "__main__":
    unittest.main()
