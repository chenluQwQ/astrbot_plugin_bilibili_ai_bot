"""Regression tests for Bilibili -509 shared rate-limit backoff, comment poll throttling,
yt-dlp remux/timeout resilience, AstrBot LLM fallback providers, and timeline schema resilience.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

import core.features.comment_reply as comment_reply_module
import core.features.private_poll as private_poll_module
from base_parts import BaseParts
from core.config import BILI_AT_NOTIFY_URL, BILI_NOTIFY_URL
from core.domain.private_inbox import PrivateInbox
from core.infra.bili.http import (
    RATE_LIMIT_BASE_SECONDS,
    RATE_LIMIT_BURST_WINDOW,
    RATE_LIMIT_RESET_WINDOW,
)
from core.video_timeline import (
    DEFAULT_SEGMENT_AUDIO,
    VideoTimelineError,
    heard_nothing,
    parse_segment_watch,
)
from llm_fakes import LegacyModels
from private_parts import PrivateParts
from reply_owners import HostFile, attach_reply_owners
from reply_parts import ReplyParts
from video_parts import VideoParts


class _FakeClock:
    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, **kwargs):
        return _FakeResponse(self.payload)

    def post(self, url, **kwargs):
        return _FakeResponse(self.payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _HttpBot(BaseParts):
    def __init__(self):
        self.config = {"SESSDATA": "s", "BILI_JCT": "j", "DEDE_USER_ID": "1"}


class _CombinedPollBot(ReplyParts, PrivateParts):
    def __init__(self, state_file, config=None):
        self.config = {
            "DEDE_USER_ID": "999",
            "POLL_INTERVAL": 20,
            "ENABLE_REPLY": True,
            "ENABLE_PRIVATE_MESSAGES": True,
            "PRIVATE_MESSAGE_POLL_INTERVAL": 120,
            "PRIVATE_MESSAGE_IDLE_POLL_INTERVAL": 180,
            "PRIVATE_MESSAGE_ACTIVE_WINDOW": 600,
            **(config or {}),
        }
        self.files = {}
        self.private_inbox = PrivateInbox(HostFile(self, state_file))
        attach_reply_owners(self)
        self.comment_reply._first_poll = False

    def _load_json(self, path, default=None):
        return self.files.get(path, [] if default is None else default)

    def _save_json(self, path, data):
        self.files[path] = data


class SharedRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_get_509_sets_shared_cooldown_burst_dedup_escalation_and_reset(self):
        clock = _FakeClock(1000.0)
        bot = _HttpBot()
        with patch("core.infra.bili.http.time.monotonic", clock), \
                patch("core.infra.bili.http.plugin_http_session", lambda _cfg: _FakeSession({"code": -509})):
            payload, _ = await bot.bili_http._http_get(BILI_NOTIFY_URL)
            self.assertEqual(payload, {"code": -509})
            self.assertEqual(bot.bili_http.rate_limit_seq, 1)
            self.assertAlmostEqual(bot.bili_http.rate_limit_remaining(clock.now), RATE_LIMIT_BASE_SECONDS)

            # Burst call within RATE_LIMIT_BURST_WINDOW extends without double-escalating or incrementing seq.
            clock.now += RATE_LIMIT_BURST_WINDOW / 2
            bot.bili_http.note_rate_limit("private_messages", min_seconds=600)
            self.assertEqual(bot.bili_http.rate_limit_seq, 1)
            self.assertAlmostEqual(bot.bili_http.rate_limit_remaining(clock.now), 600.0)

            # Repeat -509 after burst window doubles shared backoff.
            clock.now += 610.0
            await bot.bili_http._http_get(BILI_AT_NOTIFY_URL)
            self.assertEqual(bot.bili_http.rate_limit_seq, 2)
            self.assertAlmostEqual(bot.bili_http.rate_limit_remaining(clock.now), 1200.0)

            # After a long quiet period (>= RATE_LIMIT_RESET_WINDOW), backoff resets to base.
            clock.now += RATE_LIMIT_RESET_WINDOW + 10.0
            await bot.bili_http._http_get(BILI_NOTIFY_URL)
            self.assertEqual(bot.bili_http.rate_limit_seq, 3)
            self.assertAlmostEqual(bot.bili_http.rate_limit_remaining(clock.now), RATE_LIMIT_BASE_SECONDS)

    async def test_http_post_509_triggers_shared_cooldown(self):
        clock = _FakeClock(2000.0)
        bot = _HttpBot()
        with patch("core.infra.bili.http.time.monotonic", clock), \
                patch("core.infra.bili.http.plugin_http_session", lambda _cfg: _FakeSession({"code": -509})):
            payload, _ = await bot.bili_http._http_post("https://api.bilibili.com/x/v2/reply/add", data={"x": 1})
            self.assertEqual(payload, {"code": -509})
            self.assertEqual(bot.bili_http.rate_limit_seq, 1)
            self.assertGreaterEqual(bot.bili_http.rate_limit_remaining(clock.now), RATE_LIMIT_BASE_SECONDS)

    async def test_private_poll_509_pauses_comment_reply_immediately(self):
        clock = _FakeClock(5000.0)
        with tempfile.TemporaryDirectory() as folder:
            bot = _CombinedPollBot(str(Path(folder) / "private_state.json"))
            comment_hits = []

            async def fake_inbox():
                raise RuntimeError("获取私信会话失败: code=-509 请求过于频繁")

            async def fake_comment_get(url, **_kwargs):
                comment_hits.append(url)
                return {"code": 0, "data": {"items": []}}, None

            bot.private_poll._poll_private_inbox = fake_inbox
            bot.bili_http._http_get = fake_comment_get

            with patch("core.infra.bili.http.time.monotonic", clock), \
                    patch.object(private_poll_module.time, "monotonic", clock), \
                    patch.object(private_poll_module.random, "uniform", return_value=0.0), \
                    patch.object(comment_reply_module.time, "monotonic", clock), \
                    patch.object(comment_reply_module.random, "uniform", return_value=0.0):
                await bot.private_poll._poll_private_messages()
                self.assertEqual(bot.bili_http.rate_limit_seq, 1)
                self.assertAlmostEqual(bot.bili_http.rate_limit_remaining(clock.now), 600.0)

                # Comment polling ticks at +20s, +120s, +500s must all pause with zero HTTP hits.
                for step in (20.0, 100.0, 380.0):
                    clock.now += step
                    await bot.comment_reply._poll_unified()
                self.assertEqual(comment_hits, [])

                # Once 600s shared cooldown expires, comment polling resumes cleanly.
                clock.now += 101.0
                await bot.comment_reply._poll_unified()
                self.assertEqual(comment_hits, [BILI_NOTIFY_URL, BILI_AT_NOTIFY_URL])

    async def test_comment_509_short_circuits_at_endpoint_and_pauses_private_poll(self):
        clock = _FakeClock(8000.0)
        with tempfile.TemporaryDirectory() as folder:
            bot = _CombinedPollBot(str(Path(folder) / "private_state.json"))
            bot.comment_reply._first_poll = True
            comment_hits = []
            private_hits = 0

            async def fake_comment_get(url, **_kwargs):
                comment_hits.append(url)
                return {"code": -509, "message": "请求过于频繁"}, None

            async def fake_inbox():
                nonlocal private_hits
                private_hits += 1
                return []

            bot.bili_http._http_get = fake_comment_get
            bot.private_poll._poll_private_inbox = fake_inbox

            with patch("core.infra.bili.http.time.monotonic", clock), \
                    patch.object(private_poll_module.time, "monotonic", clock), \
                    patch.object(private_poll_module.random, "uniform", return_value=0.0), \
                    patch.object(comment_reply_module.time, "monotonic", clock), \
                    patch.object(comment_reply_module.random, "uniform", return_value=0.0):
                await bot.comment_reply._poll_unified()
                # Stopped right after reply endpoint without hammering the @ endpoint.
                self.assertEqual(comment_hits, [BILI_NOTIFY_URL])
                # First poll must NOT be marked done when rate-limited before seeing backlog.
                self.assertTrue(bot.comment_reply._first_poll)
                self.assertEqual(bot.bili_http.rate_limit_seq, 1)

                # Private poll immediately honors the shared -509 cooldown without hitting Bilibili.
                clock.now += 10.0
                await bot.private_poll._poll_private_messages()
                self.assertEqual(private_hits, 0)
                self.assertGreaterEqual(bot.private_poll._private_message_next_poll_at, bot.bili_http.rate_limit_until)

    async def test_comment_poll_throttles_idle_and_active_independently_of_20s_tick(self):
        clock = _FakeClock(10000.0)
        with tempfile.TemporaryDirectory() as folder:
            bot = _CombinedPollBot(str(Path(folder) / "private_state.json"), config={"POLL_INTERVAL": 20})
            hits = []

            async def fake_get(url, **_kwargs):
                hits.append((clock.now, url))
                return {"code": 0, "data": {"items": []}}, None

            bot.bili_http._http_get = fake_get
            with patch.object(comment_reply_module.time, "monotonic", clock), \
                    patch.object(comment_reply_module.random, "uniform", return_value=0.0):
                # Simulate 15 main-loop ticks at 20s intervals (= 5 minutes).
                for _ in range(15):
                    await bot.comment_reply._poll_unified()
                    clock.now += 20.0
            # Idle interval >= 120s -> at most 3 polls (6 requests) in 5 minutes instead of 30!
            self.assertEqual(len(hits), 6)
            self.assertEqual([t for t, _ in hits], [10000.0, 10000.0, 10120.0, 10120.0, 10240.0, 10240.0])

            # When active (recent comment activity), interval drops to >= 60s (still not 20s).
            hits.clear()
            bot.comment_reply._comment_last_activity_at = clock.now
            bot.comment_reply._comment_next_poll_at = 0.0
            with patch.object(comment_reply_module.time, "monotonic", clock), \
                    patch.object(comment_reply_module.random, "uniform", return_value=0.0):
                await bot.comment_reply._poll_unified()  # t=10300 -> active next=10360
                clock.now += 20.0
                await bot.comment_reply._poll_unified()  # skipped
                clock.now += 20.0
                await bot.comment_reply._poll_unified()  # skipped
                clock.now += 20.0
                await bot.comment_reply._poll_unified()  # t=10360 -> polls again
            self.assertEqual(len(hits), 4)

            # Respects user POLL_INTERVAL when configured longer than 120s.
            bot.config["POLL_INTERVAL"] = 180
            bot.comment_reply._comment_last_activity_at = 0.0
            bot.comment_reply._comment_next_poll_at = 0.0
            with patch.object(comment_reply_module.time, "monotonic", clock), \
                    patch.object(comment_reply_module.random, "uniform", return_value=0.0):
                before = clock.now
                await bot.comment_reply._poll_unified()
                self.assertAlmostEqual(bot.comment_reply._comment_next_poll_at - before, 180.0)


class _DownloadProbe(VideoParts):
    def __init__(self, folder, results, *, mirror_info=None):
        self.config = {"SESSDATA": "s", "BILI_JCT": "j", "DEDE_USER_ID": "1"}
        self.folder = folder
        self.results = list(results)
        self.mirror_info = mirror_info
        self.commands = []

    async def _run_process(self, *command, timeout=None):
        if "-J" in command:
            if self.mirror_info is None:
                return 1, "", "probe unavailable"
            return 0, json.dumps(self.mirror_info), ""
        self.commands.append(list(command))
        step = self.results.pop(0) if self.results else (1, "", "failed")
        code, stdout, stderr = step[:3]
        if code == 0:
            (Path(self.folder) / "BV1RESILIENCE.mp4").write_bytes(b"mp4data")
        return code, stdout, stderr


class VideoDownloadResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_uses_merge_output_format_without_recode_video(self):
        with tempfile.TemporaryDirectory() as folder:
            probe = _DownloadProbe(folder, [(0, "", "")])
            with patch("core.infra.video_files.TEMP_VIDEO_DIR", folder):
                fp = await probe.video_files._download_video("BV1RESILIENCE", duration=60)
            self.assertTrue(fp and fp.endswith("BV1RESILIENCE.mp4"))
            cmd = probe.commands[0]
            self.assertIn("--merge-output-format", cmd)
            self.assertNotIn("--recode-video", cmd)

    async def test_repeated_timeouts_skip_heavy_formats_and_reach_worst_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            # max_height=720 yields 8 formats; timeouts on #2 and #4 must skip #5/#6/#7 and jump to #8 (worst).
            results = [
                (1, "", "no 360 combined"),
                (124, "", "timeout 1"),
                (1, "", "no 480 combined"),
                (124, "", "timeout 2"),
                (0, "", ""),  # worst fallback succeeds
            ]
            probe = _DownloadProbe(folder, results)
            formats = probe.video_files._format_fallbacks(720)
            self.assertEqual(len(formats), 8)
            with patch("core.infra.video_files.TEMP_VIDEO_DIR", folder):
                fp = await probe.video_files._download_video("BV1RESILIENCE", max_height=720, duration=120)
            self.assertTrue(fp and fp.endswith("BV1RESILIENCE.mp4"))
            attempted_formats = [cmd[cmd.index("--format") + 1] for cmd in probe.commands]
            self.assertEqual(
                attempted_formats,
                [formats[0], formats[1], formats[2], formats[3], formats[-1]],
            )

    async def test_repeated_timeouts_stop_cleanly_when_worst_also_times_out(self):
        with tempfile.TemporaryDirectory() as folder:
            probe = _DownloadProbe(folder, [(124, "", "timeout")] * 10)
            formats = probe.video_files._format_fallbacks(480)
            with patch("core.infra.video_files.TEMP_VIDEO_DIR", folder):
                fp = await probe.video_files._download_video("BV1RESILIENCE", max_height=480, duration=60)
            self.assertIsNone(fp)
            attempted_formats = [cmd[cmd.index("--format") + 1] for cmd in probe.commands]
            # Format 1 timeout, format 2 timeout -> jump straight to final fallback -> timeout -> stop.
            self.assertEqual(attempted_formats, [formats[0], formats[1], formats[-1]])

    async def test_mirror_download_omits_recode_video_and_skips_after_repeated_timeouts(self):
        origin = "cn-xx-01-13.bilivideo.com"
        mirror = "upos-sz-mirrorcos.bilivideo.com"
        mirror_info = {"formats": [{"url": f"https://{origin}/stream.m4s"}]}

        async def fake_reachable(hosts):
            return mirror if mirror in hosts else ""

        with tempfile.TemporaryDirectory() as folder:
            probe = _DownloadProbe(
                folder,
                [(124, "", "timeout"), (124, "", "timeout"), (0, "", "")],
                mirror_info=mirror_info,
            )
            formats = probe.video_files._format_fallbacks(480)
            cookie_file = Path(folder) / "BV1RESILIENCE_cookies.txt"
            cookie_file.write_text("# cookie", encoding="utf-8")
            with patch("core.infra.video_files.TEMP_VIDEO_DIR", folder), \
                    patch("core.infra.video_files._reachable_host", fake_reachable):
                fp = await probe.video_files._download_via_mirror(
                    "BV1RESILIENCE", 480, str(cookie_file), duration=60,
                )
            self.assertTrue(fp and fp.endswith("BV1RESILIENCE.mp4"))
            self.assertTrue(all("--recode-video" not in cmd for cmd in probe.commands))
            self.assertTrue(all("--merge-output-format" in cmd for cmd in probe.commands))
            attempted_formats = [cmd[cmd.index("--format") + 1] for cmd in probe.commands]
            self.assertEqual(attempted_formats, [formats[0], formats[1], formats[-1]])


class _HttpStatusError(RuntimeError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class _FallbackContext:
    def __init__(self, primary_id, *, astrbot_config=None, available_ids=None, outcomes=None):
        self.primary_id = primary_id
        self.astrbot_config = astrbot_config or {}
        self.available_ids = list(available_ids) if available_ids is not None else [primary_id]
        self.outcomes = dict(outcomes or {})
        self.calls = []
        self.tool_calls = []

    def get_using_provider(self):
        return types.SimpleNamespace(meta=lambda: types.SimpleNamespace(id=self.primary_id))

    def get_all_providers(self):
        return [types.SimpleNamespace(meta=lambda pid=pid: types.SimpleNamespace(id=pid)) for pid in self.available_ids]

    def get_config(self):
        return self.astrbot_config

    def _consume(self, pid):
        queue = self.outcomes.get(pid)
        if isinstance(queue, list):
            result = queue.pop(0) if queue else "ok"
        else:
            result = queue if queue is not None else "ok"
        if isinstance(result, Exception):
            raise result
        return types.SimpleNamespace(completion_text=result)

    async def llm_generate(self, **kwargs):
        pid = kwargs["chat_provider_id"]
        self.calls.append(pid)
        return self._consume(pid)

    async def tool_loop_agent(self, **kwargs):
        pid = kwargs["chat_provider_id"]
        self.tool_calls.append(pid)
        return self._consume(pid)


class _LLMBot(LegacyModels):
    def __init__(self, context, **config):
        self.config = {"LLM_CIRCUIT_FAILURE_THRESHOLD": 2, "LLM_CIRCUIT_COOLDOWN_SECONDS": 120, **config}
        self.context = context


class LLMFallbackResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_provider_falls_back_on_500_without_tripping_circuit(self):
        ctx = _FallbackContext(
            "provider/primary",
            astrbot_config={"agent_runner": {"config": {"model": {"fallback_provider_ids": ["provider/backup"]}}}},
            available_ids=["provider/primary", "provider/backup"],
            outcomes={
                "provider/primary": [_HttpStatusError("Internal Server Error", 500)],
                "provider/backup": ["备用模型回复成功"],
            },
        )
        bot = _LLMBot(ctx, LLM_PROVIDER_ID="")
        reply = await bot._llm_call("你好")
        self.assertEqual(reply, "备用模型回复成功")
        self.assertEqual(ctx.calls, ["provider/primary", "provider/backup"])
        self.assertEqual(bot._llm_gateway().circuit.failures, 0)

    async def test_default_provider_falls_back_on_connection_error_and_legacy_config(self):
        ctx = _FallbackContext(
            "primary",
            astrbot_config={"provider_settings": {"fallback_chat_models": ["backup"]}},
            available_ids=["primary", "backup"],
            outcomes={
                "primary": [ConnectionResetError("connection reset by peer")],
                "backup": ["ok from backup"],
            },
        )
        bot = _LLMBot(ctx, LLM_PROVIDER_ID="default")
        self.assertEqual(await bot._llm_call("hi"), "ok from backup")
        self.assertEqual(ctx.calls, ["primary", "backup"])

    async def test_non_transient_400_does_not_try_fallback(self):
        ctx = _FallbackContext(
            "primary",
            astrbot_config={"agent_runner": {"config": {"model": {"fallback_provider_ids": ["backup"]}}}},
            available_ids=["primary", "backup"],
            outcomes={
                "primary": [_HttpStatusError("Bad Request", 400)],
                "backup": ["should not be called"],
            },
        )
        bot = _LLMBot(ctx, LLM_PROVIDER_ID="")
        self.assertIsNone(await bot._llm_call("bad prompt"))
        self.assertEqual(ctx.calls, ["primary"])
        self.assertEqual(bot._llm_gateway().circuit.failures, 1)

    async def test_explicit_llm_provider_id_preserves_contract_without_default_fallback(self):
        ctx = _FallbackContext(
            "primary",
            astrbot_config={"agent_runner": {"config": {"model": {"fallback_provider_ids": ["backup"]}}}},
            available_ids=["primary", "pinned", "backup"],
            outcomes={
                "pinned": [_HttpStatusError("500 upstream", 500)],
                "backup": ["should not run"],
            },
        )
        bot = _LLMBot(ctx, LLM_PROVIDER_ID="pinned")
        self.assertIsNone(await bot._llm_call("hi"))
        self.assertEqual(ctx.calls, ["pinned"])

    async def test_all_fallbacks_failing_counts_as_single_circuit_failure(self):
        ctx = _FallbackContext(
            "primary",
            astrbot_config={"agent_runner": {"config": {"model": {"fallback_provider_ids": ["backup"]}}}},
            available_ids=["primary", "backup"],
            outcomes={
                "primary": [_HttpStatusError("500 a", 500), _HttpStatusError("500 b", 500)],
                "backup": [_HttpStatusError("503 a", 503), _HttpStatusError("503 b", 503)],
            },
        )
        bot = _LLMBot(ctx, LLM_PROVIDER_ID="")
        self.assertIsNone(await bot._llm_call("first"))
        self.assertEqual(ctx.calls, ["primary", "backup"])
        self.assertEqual(bot._llm_gateway().circuit.failures, 1)
        # Second call opens the circuit (threshold=2); third call is skipped by circuit breaker.
        self.assertIsNone(await bot._llm_call("second"))
        self.assertIsNone(await bot._llm_call("third"))
        self.assertEqual(len(ctx.calls), 4)

    async def test_fallback_filters_unknown_duplicates_and_primary(self):
        ctx = _FallbackContext(
            "primary",
            astrbot_config={
                "agent_runner": {
                    "config": {
                        "model": {
                            "fallback_provider_ids": [
                                "primary", "", "missing-id", "backup-1", "backup-1", "backup-2",
                            ]
                        }
                    }
                }
            },
            available_ids=["primary", "backup-1", "backup-2"],
        )
        bot = _LLMBot(ctx, LLM_PROVIDER_ID="")
        self.assertEqual(bot._llm_gateway().resolve_fallback_providers("primary"), ["backup-1", "backup-2"])

    async def test_tool_loop_falls_back_on_transient_500(self):
        ctx = _FallbackContext(
            "provider/primary",
            astrbot_config={"agent_runner": {"config": {"model": {"fallback_provider_ids": ["provider/backup"]}}}},
            available_ids=["provider/primary", "provider/backup"],
            outcomes={
                "provider/primary": [_HttpStatusError("502 Bad Gateway", 502)],
                "provider/backup": ["工具循环备用成功"],
            },
        )
        bot = _LLMBot(ctx, LLM_PROVIDER_ID="")
        fake_tool_mod = types.ModuleType("astrbot.core.agent.tool")
        fake_tool_mod.ToolSet = lambda tools: list(tools)
        with patch.dict(sys.modules, {"astrbot.core.agent.tool": fake_tool_mod}):
            result = await bot._llm_gateway().tool_loop(
                "查一下", event=object(), tools=[object()], max_steps=2,
            )
        self.assertEqual(result, "工具循环备用成功")
        self.assertEqual(ctx.tool_calls, ["provider/primary", "provider/backup"])


class TimelineSchemaResilienceTests(unittest.TestCase):
    def test_missing_optional_audio_and_sensitive_default_cleanly(self):
        raw_text = json.dumps(
            {
                "timeline": [{"at": 5, "text": "开场展示零件"}],
                "impression": "一段手工拼装教程",
            },
            ensure_ascii=False,
        )
        parsed = parse_segment_watch(raw_text, offset=0, length=60)
        self.assertEqual(parsed["timeline"], [(5.0, "开场展示零件")])
        self.assertEqual(parsed["impression"], "一段手工拼装教程")
        self.assertEqual(parsed["audio"], DEFAULT_SEGMENT_AUDIO)
        self.assertTrue(heard_nothing(parsed["audio"]))
        self.assertIs(parsed["sensitive"], False)

        # Explicit nulls for optional fields also default cleanly.
        raw_nulls = json.dumps(
            {
                "timeline": [{"at": 10, "text": "继续焊接"}],
                "impression": "焊接细节",
                "audio": None,
                "sensitive": None,
            },
            ensure_ascii=False,
        )
        parsed_nulls = parse_segment_watch(raw_nulls, offset=60, length=60)
        self.assertEqual(parsed_nulls["timeline"], [(70.0, "继续焊接")])
        self.assertEqual(parsed_nulls["audio"], DEFAULT_SEGMENT_AUDIO)
        self.assertIs(parsed_nulls["sensitive"], False)

    def test_harmless_extra_keys_in_segment_and_entries_are_ignored(self):
        raw_text = json.dumps(
            {
                "timeline": [
                    {"at": 2, "text": "人物出场", "speaker": "UP主", "confidence": 0.95},
                    {"at": 18, "text": "展示成品", "extra_tag": "craft"},
                ],
                "impression": "过程清晰",
                "audio": "只有音乐",
                "sensitive": False,
                "comment_hint": "可以夸夸细节",
                "summary": "多余字段",
                "comment": {"decision": "comment", "text": "未请求评论时的多余字段"},
            },
            ensure_ascii=False,
        )
        parsed = parse_segment_watch(raw_text, offset=100, length=60, with_comment=False)
        self.assertEqual(set(parsed), {"timeline", "impression", "audio", "sensitive"})
        self.assertEqual(parsed["timeline"], [(102.0, "人物出场"), (118.0, "展示成品")])
        self.assertEqual(parsed["audio"], "只有音乐")
        self.assertIs(parsed["sensitive"], False)

    def test_missing_required_keys_invalid_types_and_fabricated_timestamps_still_rejected(self):
        for reason, payload in (
            ("segment_schema_mismatch", json.dumps(["not", "a", "dict"])),
            ("segment_schema_mismatch", json.dumps({"impression": "缺时间线"})),
            ("segment_schema_mismatch", json.dumps({"timeline": [{"at": 1, "text": "缺印象"}]})),
            ("timeline_entry_mismatch", json.dumps({"timeline": [{"at": 1}], "impression": "缺text"})),
            ("audio_invalid", json.dumps({"timeline": [{"at": 1, "text": "a"}], "impression": "ok", "audio": ""})),
            ("sensitive_must_be_bool", json.dumps({"timeline": [{"at": 1, "text": "a"}], "impression": "ok", "sensitive": "false"})),
        ):
            with self.subTest(reason=reason, payload=payload):
                with self.assertRaisesRegex(VideoTimelineError, reason):
                    parse_segment_watch(payload, length=60)

        fabricated = json.dumps(
            {"timeline": [{"at": 500, "text": "编造的时间点"}], "impression": "胡编"},
            ensure_ascii=False,
        )
        with self.assertRaisesRegex(VideoTimelineError, "timeline_at_outside_segment"):
            parse_segment_watch(fabricated, length=60)


if __name__ == "__main__":
    unittest.main()
