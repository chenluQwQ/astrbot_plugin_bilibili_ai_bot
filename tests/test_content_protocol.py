import unittest

from core.content_protocol import (
    ContentProtocolError,
    parse_bangumi_evaluation,
    parse_dynamic_content,
    parse_proactive_comment,
    parse_recommendation,
    parse_share_reaction,
    parse_share_watch_decision,
)


class ContentProtocolTests(unittest.TestCase):
    def test_proactive_comment_can_be_short_or_silent(self):
        self.assertEqual(
            parse_proactive_comment('{"decision":"comment","text":"这个转场像踩空了一拍"}')["text"],
            "这个转场像踩空了一拍",
        )
        self.assertEqual(
            parse_proactive_comment('{"decision":"skip","text":""}')["decision"],
            "skip",
        )

    def test_recommendation_rejects_partial_and_internal_output(self):
        with self.assertRaises(ContentProtocolError):
            parse_recommendation("顺手给你看看")
        with self.assertRaises(ContentProtocolError):
            parse_recommendation(
                '{"decision":"share","text":"signals: 这个不错"}'
            )

    def test_dynamic_skip_is_side_effect_free(self):
        result = parse_dynamic_content(
            '{"decision":"skip","text":"","need_image":false,"image_prompt":""}'
        )
        self.assertEqual(result["decision"], "skip")
        with self.assertRaises(ContentProtocolError):
            parse_dynamic_content(
                '{"decision":"skip","text":"还是发吧","need_image":false,"image_prompt":""}'
            )

    def test_dynamic_image_prompt_matches_boolean(self):
        with self.assertRaises(ContentProtocolError):
            parse_dynamic_content(
                '{"decision":"post","text":"今天看到一段雾里的灯塔。",'
                '"need_image":false,"image_prompt":"a lighthouse"}'
            )

    def test_dynamic_search_requires_permission_and_query(self):
        raw = ('{"decision":"search","text":"","search_query":"某作品 最新一集 播出时间",'
               '"image":"none","image_prompt":""}')
        with self.assertRaises(ContentProtocolError):
            parse_dynamic_content(raw)
        result = parse_dynamic_content(raw, allow_search=True)
        self.assertEqual(result["decision"], "search")
        self.assertEqual(result["search_query"], "某作品 最新一集 播出时间")
        with self.assertRaises(ContentProtocolError):
            parse_dynamic_content(
                '{"decision":"search","text":"顺便先写好","search_query":"x",'
                '"image":"none","image_prompt":""}', allow_search=True,
            )
        with self.assertRaises(ContentProtocolError):
            parse_dynamic_content(
                '{"decision":"post","text":"今天也很平静。","search_query":"x",'
                '"image":"none","image_prompt":""}', allow_search=True,
            )

    def test_dynamic_video_cover_only_with_an_idea_that_has_one(self):
        raw = ('{"decision":"post","text":"这期结尾的反转其实第一分钟就埋了。",'
               '"search_query":"","image":"video_cover","image_prompt":""}')
        with self.assertRaises(ContentProtocolError):
            parse_dynamic_content(raw)
        with_idea = raw[:-1] + ',"idea":2}'
        with self.assertRaises(ContentProtocolError):
            parse_dynamic_content(with_idea, idea_ids=[2, 3], cover_idea_ids=[3])
        result = parse_dynamic_content(with_idea, idea_ids=[2, 3], cover_idea_ids=[2])
        self.assertEqual(result["image"], "video_cover")
        self.assertEqual(result["idea"], 2)
        self.assertTrue(result["need_image"])

    def test_dynamic_idea_must_be_offered(self):
        raw = ('{"decision":"post","text":"这期结尾的反转其实第一分钟就埋了。",'
               '"search_query":"","image":"none","image_prompt":"","idea":%s}')
        self.assertEqual(parse_dynamic_content(raw % 0, idea_ids=[2])["idea"], 0)
        for bad in ("9", "true", '"2"'):
            with self.assertRaises(ContentProtocolError):
                parse_dynamic_content(raw % bad, idea_ids=[2])
        with self.assertRaises(ContentProtocolError):  # 给了想法就必须带 idea
            parse_dynamic_content(raw.replace(',"idea":%s', ""), idea_ids=[2])
        with self.assertRaises(ContentProtocolError):  # 没给想法就不能带
            parse_dynamic_content(raw % 0)
        skip = '{"decision":"skip","text":"","search_query":"","image":"none","image_prompt":"","idea":2}'
        with self.assertRaises(ContentProtocolError):
            parse_dynamic_content(skip, idea_ids=[2])

    def test_dynamic_generate_image_needs_prompt(self):
        with self.assertRaises(ContentProtocolError):
            parse_dynamic_content(
                '{"decision":"post","text":"雾里的灯塔。","search_query":"",'
                '"image":"generate","image_prompt":""}'
            )

    def test_dynamic_legacy_envelope_still_accepted(self):
        result = parse_dynamic_content(
            '{"decision":"post","text":"雾里的灯塔。","need_image":true,"image_prompt":"a lighthouse"}'
        )
        self.assertEqual(result["image"], "generate")
        self.assertEqual(result["image_prompt"], "a lighthouse")

    def test_bangumi_comment_cannot_leak_internal_fields(self):
        valid = parse_bangumi_evaluation(
            '{"score":7,"comment":"这段停顿比台词还扎心",'
            '"mood":"感动","review":"节奏收得很稳","want_continue":true}'
        )
        self.assertEqual(valid["score"], 7)
        with self.assertRaises(ContentProtocolError):
            parse_bangumi_evaluation(
                '{"score":7,"comment":"decision: post",'
                '"mood":"感动","review":"不错","want_continue":true}'
            )


    def test_bangumi_dynamic_wish(self):
        base = '{"score":7,"comment":"","mood":"感动","review":"不错","want_continue":true%s}'
        self.assertFalse(parse_bangumi_evaluation(base % "")["want_dynamic"])
        wish = parse_bangumi_evaluation(base % ',"want_dynamic":true,"dynamic_angle":"这集的停顿","dynamic_kind":"吐槽"')
        self.assertEqual((wish["dynamic_angle"], wish["dynamic_kind"]), ("这集的停顿", "吐槽"))
        for bad in (
            ',"want_dynamic":true,"dynamic_angle":"","dynamic_kind":"吐槽"',
            ',"want_dynamic":true,"dynamic_angle":"x","dynamic_kind":"转发"',  # 番剧不能转发
            ',"want_dynamic":false,"dynamic_angle":"x","dynamic_kind":""',
            ',"want_dynamic":true',  # 三个字段要么都有要么都没有
        ):
            with self.subTest(bad=bad), self.assertRaises(ContentProtocolError):
                parse_bangumi_evaluation(base % bad)

class ShareProtocolTests(unittest.TestCase):
    def test_watch_decision_is_watch_or_skip_only(self):
        self.assertEqual(parse_share_watch_decision('{"decision":"watch"}')["decision"], "watch")
        self.assertEqual(parse_share_watch_decision('{"decision":"SKIP"}')["decision"], "skip")
        for raw in ('看看吧', '{"decision":"maybe"}', '{"decision":"watch","why":"好奇"}'):
            with self.subTest(raw=raw), self.assertRaises(ContentProtocolError):
                parse_share_watch_decision(raw)

    def test_reaction_is_one_short_public_line(self):
        self.assertEqual(
            parse_share_reaction('{"decision":"comment","text":"这猫弹得比我好"}')["text"],
            "这猫弹得比我好",
        )
        self.assertEqual(parse_share_reaction('{"decision":"skip","text":""}')["text"], "")
        for raw in (
            '{"decision":"comment","text":""}',
            '{"decision":"skip","text":"其实想说"}',
            '{"decision":"comment","text":"' + "长" * 61 + '"}',
            '{"decision":"comment","text":"score_delta: 1"}',
        ):
            with self.subTest(raw=raw), self.assertRaises(ContentProtocolError):
                parse_share_reaction(raw)


if __name__ == "__main__":
    unittest.main()
