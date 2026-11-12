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

    def test_dynamic_wish_defaults_off_and_needs_angle(self):
        result = self.parse(payload())
        self.assertFalse(result["want_dynamic"])
        self.assertEqual(result["dynamic_angle"], "")
        result = self.parse(payload(want_dynamic=True, dynamic_angle="结尾那个停顿其实开头就埋了"))
        self.assertTrue(result["want_dynamic"])
        self.assertEqual(result["dynamic_angle"], "结尾那个停顿其实开头就埋了")
        for bad in (
            payload(want_dynamic=True, dynamic_angle=""),
            payload(want_dynamic=False, dynamic_angle="想说说"),
            payload(want_dynamic="true", dynamic_angle="想说说"),
        ):
            with self.assertRaises(VideoEvaluationError):
                self.parse(bad)

    def test_interactions_are_chosen_and_a_bad_choice_only_drops_itself(self):
        self.assertEqual(self.parse(payload())["interactions"], [])
        result = self.parse(payload(interactions=["comment", "like"]))
        self.assertEqual((result["interactions"], result["interactions_error"]), (["comment", "like"], ""))
        for bad, reason in ((["share"], "invalid_interaction"), (["like", "like"], "duplicate_interaction"),
                            ("like", "interactions_must_be_list")):
            result = self.parse(payload(interactions=bad))
            self.assertEqual((result["interactions"], result["interactions_error"]), ([], reason))
            self.assertEqual(result["score"], 8.6)

    def test_impression_is_optional_and_short(self):
        self.assertEqual(self.parse(payload())["impression"], "")
        result = self.parse(payload(impression=" 守塔人父子和解的那一集 "))
        self.assertEqual(result["impression"], "守塔人父子和解的那一集")
        for bad in (payload(impression="长" * 61), payload(impression=None)):
            with self.assertRaises(VideoEvaluationError):
                self.parse(bad)

    def test_dynamic_kind(self):
        result = self.parse(payload(want_dynamic=True, dynamic_angle="想说说"))
        self.assertEqual(result["dynamic_kind"], "分享")  # 没写类型按分享（旧输出）
        self.assertEqual(self.parse(payload())["dynamic_kind"], "")
        for kind in ("吐槽", "段子", "分享", "转发"):
            result = self.parse(payload(want_dynamic=True, dynamic_angle="想说说", dynamic_kind=kind))
            self.assertEqual(result["dynamic_kind"], kind)
        for bad in (
            payload(want_dynamic=True, dynamic_angle="想说说", dynamic_kind="日常"),
            payload(want_dynamic=True, dynamic_angle="想说说", dynamic_kind=1),
            payload(want_dynamic=False, dynamic_angle="", dynamic_kind="吐槽"),
        ):
            with self.assertRaises(VideoEvaluationError):
                self.parse(bad)

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

    def test_self_enjoyment_is_enough_to_express_a_share_intent(self):
        result = self.parse(payload(
            score=9,
            recommend_owner=True, recommend_reason="实拍机关的做法让我很想分享",
        ))
        self.assertEqual(result["score"], 9)
        self.assertTrue(result["recommend_owner"])
        self.assertEqual(result["recommend_reason"], "实拍机关的做法让我很想分享")

    def test_personal_score_rejects_non_numeric_and_non_finite_values(self):
        for score in (True, "9", -1, 11, float("nan"), float("inf")):
            with self.subTest(score=score), self.assertRaises(VideoEvaluationError):
                self.parse(payload(score=score))


if __name__ == "__main__":
    unittest.main()
