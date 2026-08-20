import unittest

from core.content_protocol import (
    ContentProtocolError,
    parse_bangumi_evaluation,
    parse_dynamic_content,
    parse_proactive_comment,
    parse_recommendation,
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


if __name__ == "__main__":
    unittest.main()
