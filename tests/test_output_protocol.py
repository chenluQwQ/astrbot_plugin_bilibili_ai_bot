import json
import unittest

from core.output_protocol import ReplyProtocolError, parse_reply_envelope


def valid_payload(**overrides):
    payload = {
        "decision": "reply",
        "reply": "这句可以发出去。",
        "score_delta": 1,
        "impression": "",
        "user_facts": [],
        "feedback": None,
        "tool_request": {"name": "none", "query": ""},
    }
    payload.update(overrides)
    return payload


class ReplyOutputProtocolTests(unittest.TestCase):
    def parse(self, payload, **kwargs):
        raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return parse_reply_envelope(raw, channel="comment", **kwargs)

    def test_valid_reply_exposes_only_validated_reply_text(self):
        result = self.parse(valid_payload())
        self.assertTrue(result["_protocol_validated"])
        self.assertEqual(result["decision"], "reply")
        self.assertEqual(result["reply"], "这句可以发出去。")
        self.assertEqual(result["signals"]["feedback_type"], "none")

    def test_plain_text_and_truncated_json_are_rejected(self):
        for raw in ("直接发这句", '{"decision":"reply","reply":"残缺'):
            with self.subTest(raw=raw):
                with self.assertRaises(ReplyProtocolError):
                    self.parse(raw)

    def test_duplicate_fields_are_rejected_not_resolved(self):
        raw = json.dumps(valid_payload(), ensure_ascii=False)
        for duplicated in (raw[:-1] + ', "reply": "第二句"}',
                           raw.replace('"feedback"', '"decision": "reply", "feedback"')):
            with self.subTest(raw=duplicated[-40:]):
                with self.assertRaises(ReplyProtocolError) as caught:
                    self.parse(duplicated)
                self.assertEqual(str(caught.exception), "duplicate_keys")

    def test_unknown_fields_and_internal_field_leak_are_rejected(self):
        extra = valid_payload(secret_analysis="不要泄漏")
        with self.assertRaises(ReplyProtocolError):
            self.parse(extra)
        leaked = valid_payload(reply='"signals": {"attack_level": 0}')
        with self.assertRaises(ReplyProtocolError):
            self.parse(leaked)

    def test_silent_decisions_are_explicit_and_side_effect_free(self):
        silent = valid_payload(
            decision="observe", reply="", score_delta=0, impression="",
            user_facts=[],
        )
        result = self.parse(silent)
        self.assertEqual(result["decision"], "observe")
        self.assertEqual(result["reply"], "")

        silent["score_delta"] = 1
        with self.assertRaises(ReplyProtocolError):
            self.parse(silent)

    def test_tool_call_requires_explicit_allowlist(self):
        payload = valid_payload(
            tool_request={"name": "watch_video", "query": "BV1234567890"}
        )
        with self.assertRaises(ReplyProtocolError):
            self.parse(payload)
        result = self.parse(
            payload,
            allowed_tools={"watch_video"},
            allow_tool_request=True,
        )
        self.assertEqual(result["tool_request"]["name"], "watch_video")

    def test_feedback_becomes_reflection_candidate(self):
        payload = valid_payload(feedback={
            "kind": "纠正", "about": "视频日期说错了", "next_time": "涉及最新投稿时先核对发布日期",
        })
        signals = self.parse(payload)["signals"]
        self.assertEqual(signals["feedback_type"], "correction")
        self.assertEqual(signals["feedback_topic"], "视频日期说错了")
        self.assertEqual(signals["reflection_candidate"]["next_time"], "涉及最新投稿时先核对发布日期")

    def test_feedback_kind_and_shape_are_strict(self):
        for bad in ({"kind": "开玩笑", "about": "调侃", "next_time": "改人格"},
                    {"kind": "建议", "about": "多聊游戏"},
                    "建议多聊游戏"):
            with self.subTest(bad=bad):
                with self.assertRaises(ReplyProtocolError):
                    self.parse(valid_payload(feedback=bad))

    def test_attack_level_is_derived_from_score(self):
        signals = self.parse(valid_payload(score_delta=-5))["signals"]
        self.assertEqual(signals["attack_level"], 3)
        self.assertEqual(signals["feedback_type"], "none")
        self.assertEqual(self.parse(valid_payload())["signals"]["attack_level"], 0)

if __name__ == "__main__":
    unittest.main()
