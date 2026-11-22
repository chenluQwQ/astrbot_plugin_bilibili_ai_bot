"""视频模型按时间记：格式校验、秒数换算、存档文本和日志摘要。"""
import json
import unittest

from core.video_timeline import (
    PARTIAL_MARK, SEGMENT_SCHEMA_PROMPT, SEGMENT_TEXT_HINT, SEGMENT_TEXT_LIMIT, TIMELINE_MARK, VideoTimelineError, clock, format_subtitle_lines, format_watch, heard_nothing,
    parse_segment_watch, watch_digest,
)


def raw(timeline, impression="印象", audio="有人讲话", sensitive=False):
    return json.dumps({"timeline": timeline, "impression": impression, "audio": audio,
                       "sensitive": sensitive}, ensure_ascii=False)


class ParseTests(unittest.TestCase):
    def test_offset_added_and_sorted(self):
        out = parse_segment_watch(raw([{"at": 20, "text": "后"}, {"at": 3, "text": "前"}]), offset=300, length=300)
        self.assertEqual(out["timeline"], [(303.0, "前"), (320.0, "后")])
        self.assertEqual(out["impression"], "印象")

    def test_code_fence_accepted(self):
        out = parse_segment_watch("```json\n" + raw([{"at": 1, "text": "a"}]) + "\n```")
        self.assertEqual(out["timeline"], [(1.0, "a")])

    def test_rejects(self):
        cases = {
            "invalid_json": "随便写的一段话",
            "segment_schema_mismatch": json.dumps({"timeline": [{"at": 1, "text": "a"}], "impression": "x"}),
            "timeline_empty": raw([]),
            "timeline_entry_mismatch": raw([{"time": 1, "text": "a"}]),
            "timeline_at_invalid": raw([{"at": "00:01", "text": "a"}]),
            "timeline_text_invalid": raw([{"at": 1, "text": "  "}]),
            "impression_invalid": raw([{"at": 1, "text": "a"}], impression=""),
            "audio_invalid": raw([{"at": 1, "text": "a"}], audio=""),
            "sensitive_must_be_bool": raw([{"at": 1, "text": "a"}], sensitive="否"),
        }
        for reason, text in cases.items():
            with self.subTest(reason):
                with self.assertRaisesRegex(VideoTimelineError, reason):
                    parse_segment_watch(text)

    def test_over_hint_kept_as_is(self):
        """提示词说 1500 字，模型写到 2400 也照收，一个字不动。"""
        timeline = [{"at": i, "text": "字" * 240} for i in range(10)]
        out = parse_segment_watch(raw(timeline))
        self.assertEqual(len(out["timeline"]), 10)
        self.assertEqual(sum(len(t) for _, t in out["timeline"]), 2400)

    def test_over_limit_thinned_keeping_ends(self):
        out = parse_segment_watch(raw([{"at": i, "text": f"{i:03d}" + "字" * 97} for i in range(60)]))
        texts = [t for _, t in out["timeline"]]
        self.assertLessEqual(sum(map(len, texts)), SEGMENT_TEXT_LIMIT)
        self.assertTrue(texts[0].startswith("000"))
        self.assertTrue(texts[-1].startswith("059"))

    def test_one_huge_entry_clipped_not_rejected(self):
        out = parse_segment_watch(raw([{"at": 1, "text": "字" * 5000}], impression="印" * 1500, audio="声" * 500))
        self.assertLessEqual(len(out["timeline"][0][1]), SEGMENT_TEXT_LIMIT + 1)
        self.assertLessEqual(len(out["impression"]), 801)
        self.assertLessEqual(len(out["audio"]), 301)

    def test_prompt_states_the_smaller_number(self):
        self.assertIn(str(SEGMENT_TEXT_HINT), SEGMENT_SCHEMA_PROMPT)
        self.assertNotIn(str(SEGMENT_TEXT_LIMIT), SEGMENT_SCHEMA_PROMPT)
        self.assertLess(SEGMENT_TEXT_HINT, SEGMENT_TEXT_LIMIT)

    def test_bool_is_not_a_second(self):
        with self.assertRaises(VideoTimelineError):
            parse_segment_watch(raw([{"at": True, "text": "a"}]))

    def test_time_outside_segment_rejected_with_slack(self):
        parse_segment_watch(raw([{"at": 64, "text": "a"}]), length=60)
        with self.assertRaisesRegex(VideoTimelineError, "outside"):
            parse_segment_watch(raw([{"at": 70, "text": "a"}]), length=60)


class FormatTests(unittest.TestCase):
    def test_clock(self):
        self.assertEqual(clock(5), "00:05")
        self.assertEqual(clock(301.7), "05:01")
        self.assertEqual(clock(3725), "1:02:05")

    def test_watch_text_puts_impression_first(self):
        text = format_watch("看完觉得很治愈", [(1, "开场"), (65, "上色")], partial=True)
        self.assertTrue(text.startswith(PARTIAL_MARK + "看完觉得很治愈"))
        self.assertIn(f"{TIMELINE_MARK}\n[00:01] 开场\n[01:05] 上色", text)

    def test_digest_takes_impression_only(self):
        text = format_watch("看完觉得很治愈", [(1, "开场")])
        self.assertEqual(watch_digest(text, 100), "看完觉得很治愈")
        self.assertEqual(watch_digest("旧的概括\n第二行", 100), "旧的概括 第二行")
        self.assertEqual(watch_digest("一二三四五", 3), "一二三…")

    def test_heard_nothing(self):
        self.assertTrue(heard_nothing("听不到"))
        self.assertFalse(heard_nothing("只有音乐"))

    def test_subtitle_lines_window_and_limit(self):
        lines = [{"from": 1, "content": "一"}, {"from": 61, "content": "二"}, {"from": 125, "content": "三"}]
        self.assertEqual(format_subtitle_lines(lines, 60, 120), "[01:01] 二")
        self.assertEqual(format_subtitle_lines(lines), "[00:01] 一\n[01:01] 二\n[02:05] 三")
        self.assertIn("后面还有", format_subtitle_lines(lines, limit=12))


class ProactiveCommentRulesTests(unittest.TestCase):
    def test_long_comment_keeps_paragraphs_up_to_limit(self):
        from core.content_protocol import ContentProtocolError, PROACTIVE_COMMENT_LIMIT, parse_proactive_comment
        body = "第一段" + "字" * 200 + "\r\n\n\n\n第二段"
        out = parse_proactive_comment(json.dumps({"decision": "comment", "text": body}, ensure_ascii=False))
        self.assertIn("\n\n第二段", out["text"])
        self.assertNotIn("\n\n\n", out["text"])
        too_long = json.dumps({"decision": "comment", "text": "字" * (PROACTIVE_COMMENT_LIMIT + 1)}, ensure_ascii=False)
        with self.assertRaises(ContentProtocolError):
            parse_proactive_comment(too_long)


if __name__ == "__main__":
    unittest.main()
