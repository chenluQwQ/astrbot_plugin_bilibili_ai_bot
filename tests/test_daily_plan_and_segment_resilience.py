"""Tests for DailyPlan async grounding_context awaiting, segment schema diagnostics/retry,
and PrivatePoll shared -509 cooldown enforcement.
"""
from __future__ import annotations

import json
import tempfile
import types
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import ScheduleProbe, _install_astrbot_stub

_install_astrbot_stub()

import core.features.private_poll as private_poll_module
from base_parts import BaseParts
from core.domain.private_inbox import PrivateInbox
from core.infra.bili.http import RATE_LIMIT_BURST_WINDOW
from core.settings import Settings
from core.video_timeline import VideoTimelineError, parse_segment_watch
from private_parts import PrivateParts
from reply_owners import HostFile, attach_reply_owners
from reply_parts import ReplyParts
from video_parts import VideoParts


def _valid_segment_json(text: str = "画面内容", at: int = 2) -> str:
    return json.dumps(
        {
            "timeline": [{"at": at, "text": text}],
            "impression": "整体印象清晰",
            "audio": "有人讲话",
            "sensitive": False,
        },
        ensure_ascii=False,
    )


class DailyPlanAsyncGroundingTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_plan_awaits_async_grounding_context_without_runtime_warning(self):
        captured_prompts: list[str] = []

        class _AsyncDynamicPost:
            def post_due(self):
                return True

            async def grounding_context(self):
                return ["[2026-11-26] 视频《猫猫日常》：很治愈"]

        class _PlanProbe(ScheduleProbe, BaseParts):
            def __init__(self):
                super().__init__({
                    "ENABLE_AUTONOMOUS_DAILY_PLAN": True,
                    "AUTONOMOUS_ACTIVITY_LEVEL": 60,
                    "ENABLE_PROACTIVE": True,
                    "PROACTIVE_TIMES_COUNT": 1,
                    "AUTONOMOUS_PROACTIVE_DAILY_MAX": 2,
                })
                self.dynamic_post = _AsyncDynamicPost()

            async def _get_system_prompt(self):
                return "测试人设"

            async def _llm_call(self, prompt, **_kwargs):
                captured_prompts.append(prompt)
                return json.dumps(
                    {
                        "proactive_windows": ["14:00-15:00"],
                        "dynamic_times": [],
                        "dynamic_watch_times": [],
                        "bangumi_times": [],
                        "special_follow_times": [],
                        "rationale": "下午轻松看个视频",
                    },
                    ensure_ascii=False,
                )

            async def _generation_llm_call(self, prompt, **_kwargs):
                return await self._llm_call(prompt, **_kwargs)

        probe = _PlanProbe()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            plan = await probe.daily_plan._ensure_autonomous_daily_plan(force=True)

        runtime_warnings = [w for w in caught if issubclass(w.category, RuntimeWarning)]
        self.assertEqual(runtime_warnings, [])
        self.assertEqual(plan["generation_status"], "success")
        self.assertEqual(len(captured_prompts), 1)
        self.assertIn("视频《猫猫日常》：很治愈", captured_prompts[0])
        self.assertNotIn("<coroutine object", captured_prompts[0])

    async def test_daily_plan_still_accepts_sync_grounding_context_stub(self):
        captured_prompts: list[str] = []

        class _SyncDynamicPost:
            def post_due(self):
                return True

            def grounding_context(self):
                return ["同步上下文片段"]

        class _PlanProbe(ScheduleProbe, BaseParts):
            def __init__(self):
                super().__init__({"ENABLE_AUTONOMOUS_DAILY_PLAN": True})
                self.dynamic_post = _SyncDynamicPost()

            async def _get_system_prompt(self):
                return "测试人设"

            async def _llm_call(self, prompt, **_kwargs):
                captured_prompts.append(prompt)
                return json.dumps({"proactive_windows": [], "rationale": "ok"})

            async def _generation_llm_call(self, prompt, **_kwargs):
                return await self._llm_call(prompt, **_kwargs)

        probe = _PlanProbe()
        plan = await probe.daily_plan._ensure_autonomous_daily_plan(force=True)
        self.assertEqual(plan["generation_status"], "success")
        self.assertIn("同步上下文片段", captured_prompts[0])


class SegmentSchemaDiagnosticsAndRetryTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_segment_watch_accepts_prose_wrapped_strict_json(self):
        wrapped = "好的，这是本段记录：\n```json\n" + _valid_segment_json("开场画面") + "\n```\n以上。"
        out = parse_segment_watch(wrapped, offset=60, length=120)
        self.assertEqual(out["timeline"], [(62.0, "开场画面")])
        self.assertEqual(out["audio"], "有人讲话")

        bare_wrapped = "记录如下：" + _valid_segment_json("无围栏") + " 完毕"
        out2 = parse_segment_watch(bare_wrapped, offset=0, length=120)
        self.assertEqual(out2["timeline"], [(2.0, "无围栏")])

    def test_parse_segment_watch_reports_missing_and_extra_keys_without_prose(self):
        raw = json.dumps(
            {
                "timeline": [{"at": 1, "text": "SECRET_PROSE"}],
                "impression": "SECRET_IMPRESSION",
                "unexpected_key": "SECRET_VALUE",
            },
            ensure_ascii=False,
        )
        with self.assertRaises(VideoTimelineError) as ctx:
            parse_segment_watch(raw)
        self.assertEqual(ctx.exception.code, "segment_schema_mismatch")
        msg = str(ctx.exception)
        self.assertIn("missing=audio,sensitive", msg)
        self.assertIn("extra=unexpected_key", msg)
        self.assertNotIn("SECRET_", msg)

        entry_bad = json.dumps(
            {
                "timeline": [{"time": 1, "text": "a"}],
                "impression": "x",
                "audio": "y",
                "sensitive": False,
            },
            ensure_ascii=False,
        )
        with self.assertRaises(VideoTimelineError) as entry_ctx:
            parse_segment_watch(entry_bad)
        self.assertEqual(entry_ctx.exception.code, "timeline_entry_mismatch")
        self.assertIn("missing=at", str(entry_ctx.exception))
        self.assertIn("extra=time", str(entry_ctx.exception))

    async def test_direct_mode_retries_once_on_segment_schema_mismatch_and_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            seg_path = Path(folder) / "seg0.mp4"
            seg_path.write_bytes(b"video")
            replies = [
                json.dumps({"timeline": [{"at": 1, "text": "x"}], "impression": "少字段"}),
                _valid_segment_json("重试成功"),
            ]
            prompts: list[str] = []

            class _WatchBot(VideoParts):
                def __init__(self):
                    self.config = {"VIDEO_VISION_FORMAT": "openai", "VIDEO_VISION_AUDIO": True}
                    self.settings = Settings(self.config)
                    self.vision = types.SimpleNamespace(
                        _astrbot_multimodal_generate=self._gen,
                        _vision_call=None,
                    )

                async def _gen(self, _provider_id, content, **_kwargs):
                    for part in content:
                        if part.get("type") == "text":
                            prompts.append(part["text"])
                    return replies.pop(0)

            bot = _WatchBot()
            info = {"bvid": "BV1TEST12345", "title": "测试", "tname": "生活", "desc": ""}
            result = await bot.video_watch._analyze_single_segment(
                str(seg_path), info, "openai", "vp", None, "vm", "第1/2段", offset=0.0, length=60.0
            )
            self.assertIsNotNone(result)
            self.assertEqual(result["mode"], "direct")
            self.assertEqual(result["timeline"], [(2.0, "重试成功")])
            self.assertEqual(len(prompts), 2)
            self.assertIn("missing=audio,sensitive", prompts[1])

    async def test_frames_mode_retries_once_and_records_clear_detail_on_persistent_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            seg_path = Path(folder) / "seg0.mp4"
            seg_path.write_bytes(b"video")
            frame_path = Path(folder) / "frame0.jpg"
            frame_path.write_bytes(b"jpg")
            calls = []

            class _FrameBot(VideoParts):
                def __init__(self):
                    self.config = {"VIDEO_VISION_FORMAT": "none"}
                    self.settings = Settings(self.config)
                    self.vision = types.SimpleNamespace(
                        _astrbot_multimodal_generate=self._gen,
                        _vision_call=None,
                    )

                async def _extract_video_frames(self, _path, count=5):
                    return [str(frame_path)]

                async def _get_video_subtitle_lines(self, _bvid, _cid):
                    return []

                def _cleanup_video_artifacts(self, _path, _frames=None):
                    pass

                async def _gen(self, _provider_id, content, **_kwargs):
                    calls.append(content[-1]["text"])
                    return json.dumps({"timeline": [{"at": 1, "text": "x"}], "impression": "缺字段"})

            bot = _FrameBot()
            bot.bili_videos = types.SimpleNamespace(_get_video_subtitle_lines=bot._get_video_subtitle_lines)
            bot.video_files = types.SimpleNamespace(
                _extract_video_frames=bot._extract_video_frames,
                _cleanup_video_artifacts=bot._cleanup_video_artifacts,
            )
            info = {"bvid": "BV1TEST12345", "title": "测试", "tname": "生活", "desc": ""}
            result = await bot.video_watch._analyze_single_segment(
                str(seg_path), info, "none", "vp", None, "vm", offset=0.0, length=60.0
            )
            self.assertIsNone(result)
            # Initial attempt + 1 targeted schema-correction retry, then stop.
            self.assertEqual(len(calls), 2)
            self.assertIn("missing=audio,sensitive", calls[1])
            self.assertIn("missing=audio,sensitive", info["_media_diagnostics"][-1]["reason"])


class PrivatePollRateLimitCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_poll_honors_extended_cooldown_even_when_seq_unchanged(self):
        class _Clock:
            def __init__(self, start=1000.0):
                self.now = float(start)

            def __call__(self):
                return self.now

        class _PollBot(ReplyParts, PrivateParts):
            def __init__(self, state_file):
                self.config = {
                    "DEDE_USER_ID": "999",
                    "ENABLE_PRIVATE_MESSAGES": True,
                    "PRIVATE_MESSAGE_POLL_INTERVAL": 120,
                    "PRIVATE_MESSAGE_IDLE_POLL_INTERVAL": 180,
                    "PRIVATE_MESSAGE_ACTIVE_WINDOW": 600,
                }
                self.files = {}
                self.private_inbox = PrivateInbox(HostFile(self, state_file))
                attach_reply_owners(self)

            def _load_json(self, path, default=None):
                return self.files.get(path, [] if default is None else default)

            def _save_json(self, path, data):
                self.files[path] = data

        clock = _Clock(5000.0)
        with tempfile.TemporaryDirectory() as folder:
            bot = _PollBot(str(Path(folder) / "private_state.json"))
            inbox_hits = 0

            async def fake_inbox():
                nonlocal inbox_hits
                inbox_hits += 1
                return []

            bot.private_poll._poll_private_inbox = fake_inbox

            with patch("core.infra.bili.http.time.monotonic", clock), \
                    patch.object(private_poll_module.time, "monotonic", clock), \
                    patch.object(private_poll_module.random, "uniform", return_value=0.0):
                # 1) Initial -509 sets 300s shared cooldown (seq=1); private_poll observes it.
                bot.bili_http.note_rate_limit("/x/msgfeed/reply")
                await bot.private_poll._poll_private_messages()
                self.assertEqual(inbox_hits, 0)
                self.assertEqual(bot.private_poll._private_message_seen_rate_limit_seq, 1)

                # 2) Burst-window call extends shared_until to +600s WITHOUT incrementing seq.
                clock.now += RATE_LIMIT_BURST_WINDOW / 2
                bot.bili_http.note_rate_limit("burst_endpoint", min_seconds=600)
                self.assertEqual(bot.bili_http.rate_limit_seq, 1)

                # 3) At +350s (past old 300s next_poll_at, but still inside extended 600s shared_until):
                # private_poll must STILL refrain from hitting Bilibili!
                clock.now += 350.0
                await bot.private_poll._poll_private_messages()
                self.assertEqual(inbox_hits, 0)
                self.assertGreaterEqual(
                    bot.private_poll._private_message_next_poll_at,
                    bot.bili_http.rate_limit_until,
                )

                # 4) Once extended 600s cooldown expires, polling resumes normally.
                clock.now = bot.bili_http.rate_limit_until + 1.0
                await bot.private_poll._poll_private_messages()
                self.assertEqual(inbox_hits, 1)

    async def test_single_message_retry_never_undercuts_extended_shared_cooldown(self):
        class _Clock:
            def __init__(self, start=5000.0):
                self.now = float(start)

            def __call__(self):
                return self.now

        class _PollBot(ReplyParts, PrivateParts):
            def __init__(self, state_file):
                self.config = {
                    "DEDE_USER_ID": "999",
                    "ENABLE_PRIVATE_MESSAGES": True,
                    "PRIVATE_MESSAGE_POLL_INTERVAL": 120,
                    "PRIVATE_MESSAGE_IDLE_POLL_INTERVAL": 180,
                    "PRIVATE_MESSAGE_ACTIVE_WINDOW": 600,
                }
                self.files = {}
                self.private_inbox = PrivateInbox(HostFile(self, state_file))
                attach_reply_owners(self)

            def _load_json(self, path, default=None):
                return self.files.get(path, [] if default is None else default)

            def _save_json(self, path, data):
                self.files[path] = data

        clock = _Clock(5000.0)
        with tempfile.TemporaryDirectory() as folder:
            bot = _PollBot(str(Path(folder) / "private_state.json"))
            msg = {
                "msg_key": 778899,
                "sender_uid": 10001,
                "receiver_id": 999,
                "msg_type": 1,
                "content": json.dumps({"content": "hello"}),
                "timestamp": 1700000000,
            }

            async def fake_inbox():
                return [msg]

            async def fail_with_rate_limit(_message, _trusted_domains):
                # Trigger -509 cooldown longer than the initial normal_delay (180s)
                # set before the message loop began.
                bot.bili_http.note_rate_limit("/web_im/v1/web_im/send_msg", min_seconds=600)
                raise RuntimeError("http -509 rate limit")

            bot.private_poll._poll_private_inbox = fake_inbox
            bot.private_reply._handle_private_message = fail_with_rate_limit

            with patch("core.infra.bili.http.time.monotonic", clock), \
                    patch.object(private_poll_module.time, "monotonic", clock), \
                    patch.object(private_poll_module.random, "uniform", return_value=0.0):
                await bot.private_poll._poll_private_messages()
                self.assertGreaterEqual(
                    bot.private_poll._private_message_next_poll_at,
                    bot.bili_http.rate_limit_until,
                )


if __name__ == "__main__":
    unittest.main()
