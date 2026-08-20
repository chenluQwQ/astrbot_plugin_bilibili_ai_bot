import json
import unittest

from core.video_evaluation import VideoEvaluationError, parse_video_evaluation


def payload(**overrides):
    value = {
        "score": 8.6,
        "score_reason": "喜欢人物之间克制但持续变化的关系",
        "comment": "最后那个停顿比台词还狠",
        "mood": "感动",
        "review": "没有硬煽情，反倒被最后一个小动作击中了。",
        "want_follow": True,
        "recommend_owner": False,
        "recommend_reason": "",
        "partition": "动画",
        "preference_signals": [
            {
                "type": "work", "value": "守塔人", "polarity": "like",
                "strength": 0.85, "evidence": "喜欢人物关系和克制叙事",
            },
            {
                "type": "character", "value": "守塔人父子", "polarity": "curious",
                "strength": 0.7, "evidence": "想继续了解人物关系",
            },
        ],
        "search_keywords": ["守塔人 人物解析", "克制叙事 动画"],
    }
    value.update(overrides)
    return value


class VideoEvaluationSchemaTests(unittest.TestCase):
    def parse(self, value):
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return parse_video_evaluation(raw)

    def test_preserves_decimal_score_and_concrete_signals(self):
        result = self.parse(payload())
        self.assertEqual(result["score"], 8.6)
        self.assertEqual(result["preference_signals"][0]["value"], "守塔人")
        self.assertEqual(result["search_keywords"][0], "守塔人 人物解析")

    def test_plain_text_partial_json_and_extra_fields_are_rejected(self):
        for value in ("挺好看的，8分", '{"score":8', payload(analysis="内部分析")):
            with self.subTest(value=value):
                with self.assertRaises(VideoEvaluationError):
                    self.parse(value)

    def test_signal_count_and_types_are_bounded(self):
        too_many = payload(
            preference_signals=[payload()["preference_signals"][0]] * 6
        )
        with self.assertRaises(VideoEvaluationError):
            self.parse(too_many)
        bad = payload()
        bad["preference_signals"][0]["type"] = "everything"
        with self.assertRaises(VideoEvaluationError):
            self.parse(bad)

    def test_owner_recommendation_requires_concrete_reason(self):
        with self.assertRaises(VideoEvaluationError):
            self.parse(payload(recommend_owner=True, recommend_reason=""))


if __name__ == "__main__":
    unittest.main()
