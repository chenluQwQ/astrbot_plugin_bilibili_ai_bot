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
        "signals": {
            "interaction_type": "normal",
            "feedback_type": "none",
            "attack_level": 0,
            "feedback_topic": None,
            "reflection_candidate": None,
            "confidence": 0.9,
        },
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

    def test_reflection_candidate_must_be_short_structured_conclusion(self):
        payload = valid_payload()
        payload["signals"] = {
            **payload["signals"],
            "interaction_type": "correction",
            "feedback_type": "correction",
            "feedback_topic": "回复事实错误",
            "reflection_candidate": {
                "event": "用户指出视频日期说错了",
                "possible_mistake": "没有核对查询结果中的日期",
                "next_time": "涉及最新投稿时先核对发布日期",
            },
        }
        result = self.parse(payload)
        self.assertEqual(
            result["signals"]["reflection_candidate"]["next_time"],
            "涉及最新投稿时先核对发布日期",
        )

    def test_teasing_is_not_allowed_to_smuggle_in_a_reflection(self):
        payload = valid_payload()
        payload["signals"] = {
            **payload["signals"],
            "interaction_type": "teasing",
            "feedback_type": "teasing",
            "reflection_candidate": None,
        }
        self.assertEqual(
            self.parse(payload)["signals"]["interaction_type"], "teasing"
        )
        payload["signals"]["reflection_candidate"] = {
            "event": "熟人开玩笑", "possible_mistake": "被调侃",
            "next_time": "立刻改人格",
        }
        with self.assertRaises(ReplyProtocolError):
            self.parse(payload)


if __name__ == "__main__":
    unittest.main()
