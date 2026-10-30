"""Offline end-to-end checks of the comment model's bounded read-only tool flow."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_autonomous_ranges import _install_astrbot_stub
from test_output_protocol import valid_payload
from core.reply import ReplyMixin
from core.memory import MemoryMixin
from core.video import VideoMixin
from core.utils import UtilsMixin
from core.output_protocol import parse_reply_envelope, ReplyProtocolError
from core.video_inspection import inspection_intervals, parse_inspection
import core.video_inspection as inspection_module

BV = "BV1xx411c7mD"


def response(decision="reply", name="none", query="", **extra):
    payload = valid_payload(decision=decision, tool_request={"name": name, "query": query, **extra})
    if decision != "reply":
        payload.update(reply="", score_delta=0, impression="", user_facts=[])
    return json.dumps(payload, ensure_ascii=False)


class Bot(ReplyMixin):
    def __init__(self, responses):
        self.config = {"ENABLE_WEB_SEARCH": True, "WEB_SEARCH_API_KEY": "fake"}
        self._llm_call = AsyncMock(side_effect=responses)
        self._get_video_info = AsyncMock(return_value={"bvid": BV, "cid": 2, "title": "作品", "desc": "介绍", "duration": 300})
        self._get_video_subtitles = AsyncMock(return_value="相关字幕")
        self._get_hot_comments = AsyncMock(return_value=["外部热评"])
        self._web_search = AsyncMock(return_value="外部搜索结果")
        self._inspect_comment_video = AsyncMock(return_value={"status": "ok", "segments": [{"finding": "uncertain"}]})
        self.logs = []
        self._append_json_list = lambda path, row, cap: self.logs.append(row)

    async def flow(self, **kw):
        return await self._run_comment_reply_flow("评论和已有记忆", "personality", 123, 1, "thread", **kw)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_reply_or_silence_does_not_fetch(self):
        for decision in ("reply", "ignore", "observe"):
            bot = Bot([response(decision)])
            result = await bot.flow()
            self.assertEqual(result["decision"], decision)
            self.assertEqual(bot._llm_call.await_count, 1)
            bot._get_video_info.assert_not_awaited()
            bot._web_search.assert_not_awaited()

    async def test_subtitles_then_targeted_inspection_then_final_reply(self):
        bot = Bot([response("tool", "video_subtitles"),
                   response("tool", "video_inspect", "最后是否出现猫？", start_seconds=260, end_seconds=290),
                   response()])
        result = await bot.flow()
        self.assertEqual(result["decision"], "reply")
        self.assertEqual([x["tool"] for x in result["_context_trace"]], ["video_subtitles", "video_inspect"])
        bot._inspect_comment_video.assert_awaited_once_with(bot._get_video_info.return_value, "最后是否出现猫？", 260.0, 290.0)
        self.assertEqual(bot._get_video_info.await_count, 1)
        final_prompt = bot._llm_call.call_args.args[0]
        self.assertIn("相关字幕", final_prompt)
        self.assertIn('trust="untrusted"', final_prompt)
        self.assertIn("uncertain", final_prompt)

    async def test_search_then_ignore_does_not_publish_intermediate_reply(self):
        bot = Bot([response("tool", "web_search", "公开事实"), response("ignore")])
        result = await bot.flow()
        self.assertEqual(result["reply"], "")
        self.assertEqual(result["score_delta"], 0)
        self.assertEqual(bot.logs[-1]["decision"], "ignore")
        self.assertFalse(await bot._apply_reply_result(mid="1", username="u", content="c", oid=123,
            rpid=1, comment_type=1, thread_id="t", result=result))

    async def test_repeated_request_executes_only_once_and_budget_is_bounded(self):
        bot = Bot([response("tool", "web_search", "公开事实")] * 4 + [response()])
        result = await bot.flow()
        self.assertEqual(result["decision"], "reply")
        self.assertEqual(bot._web_search.await_count, 1)
        self.assertEqual(bot._llm_call.await_count, 5)
        self.assertIn("本轮不再提供查询", bot._llm_call.call_args.args[0])

    async def test_requests_after_budget_fail_without_execution(self):
        bot = Bot([response("tool", "web_search", "one"), response("tool", "web_search", "two")])
        bot.config["COMMENT_CONTEXT_MAX_CALLS"] = 1
        result = await bot.flow()
        self.assertEqual(result["decision"], "error")
        self.assertEqual(bot._web_search.await_count, 1)

    async def test_video_inspection_is_limited_to_two_distinct_requests(self):
        bot = Bot([response("tool", "video_inspect", question)
                   for question in ("是否有猫？", "是否有狗？", "是否有鸟？")] + [response("ignore")])
        result = await bot.flow()
        self.assertEqual(result["decision"], "ignore")
        self.assertEqual(bot._inspect_comment_video.await_count, 2)
        self.assertEqual(result["_context_trace"][2]["status"], "unavailable")
        self.assertIn("已达视频检查次数上限", bot._llm_call.call_args.args[0])

    async def test_unavailable_material_can_be_followed_by_silence(self):
        bot = Bot([response("tool", "video_subtitles"), response("ignore")])
        bot._get_video_subtitles.return_value = ""
        result = await bot.flow()
        self.assertEqual(result["decision"], "ignore")
        self.assertEqual(result["_context_trace"][0]["status"], "unavailable")

    async def test_tool_timeout_cancels_work_then_allows_final_decision(self):
        bot = Bot([response("tool", "web_search", "公开事实"), response("ignore")])
        cancelled = asyncio.Event()
        async def search(_):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        bot._web_search.side_effect = search
        bot._COMMENT_CONTEXT_TOOL_TIMEOUT = 0.01
        result = await bot.flow()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result["decision"], "ignore")
        self.assertIn("超时", bot._llm_call.call_args.args[0])

    async def test_malformed_output_never_executes_tool(self):
        bot = Bot(['{"decision":"tool"'])
        self.assertEqual((await bot.flow())["decision"], "error")
        bot._get_video_info.assert_not_awaited()

    async def test_disabled_or_suspicious_or_wrong_comment_type_blocks_tools(self):
        for mode in ("disabled", "suspicious", "dynamic", "web_disabled"):
            name = "web_search" if mode == "web_disabled" else "video_inspect"
            bot = Bot([response("tool", name, "question")])
            if mode == "disabled": bot.config["COMMENT_CONTEXT_ENABLED"] = False
            if mode == "web_disabled": bot.config["ENABLE_WEB_SEARCH"] = False
            result = await bot._run_comment_reply_flow("p", "s", 123, 17 if mode == "dynamic" else 1, "t", suspicious=mode == "suspicious")
            self.assertEqual(result["decision"], "error", mode)
            bot._inspect_comment_video.assert_not_awaited()
            bot._web_search.assert_not_awaited()

    async def test_single_switch_replaces_legacy_video_switch(self):
        bot = Bot([response("tool", "video_inspect", "是否有猫？"), response()])
        bot.config["COMMENT_VIDEO_INSPECTION_ENABLED"] = False
        self.assertEqual((await bot.flow())["decision"], "reply")
        bot._inspect_comment_video.assert_awaited_once()
        bot.config["COMMENT_CONTEXT_ENABLED"] = False
        self.assertEqual(bot._comment_context_tools(123, 1), [])

    async def test_manual_diagnostic_uses_real_flow_but_never_applies_reply(self):
        bot = Bot([response("tool", "video_description"), response()])
        bot.config["BILI_TOOL_AUDIT_ENABLED"] = False
        bot._get_video_oid = AsyncMock(return_value=123)
        bot._get_system_prompt = AsyncMock(return_value="persona")
        bot._apply_reply_result = AsyncMock()
        text = await bot._test_comment_context(BV)
        self.assertIn("简介：成功", text)
        self.assertIn("回复预览（未发布）", text)
        self.assertEqual(bot.logs[-1]["mode"], "test")
        bot._apply_reply_result.assert_not_awaited()
        bot._load_json = lambda *args: bot.logs
        self.assertIn("只读实测", bot._render_comment_context_log(bot.logs[-1]["time"][:10]))

    async def test_manual_diagnostic_respects_switch_and_validation(self):
        bot = Bot([])
        bot._get_video_oid = AsyncMock(return_value=123)
        self.assertIn("完整 BV", await bot._test_comment_context("invalid"))
        bot.config["COMMENT_CONTEXT_ENABLED"] = False
        self.assertIn("已关闭", await bot._test_comment_context(BV))
        bot._get_video_oid.assert_not_awaited()
        bot._llm_call.assert_not_awaited()

    async def test_model_error_produces_readable_failure_log(self):
        bot = Bot([])
        bot._llm_call.side_effect = RuntimeError("provider failed")
        result = await bot.flow()
        self.assertEqual(result["error"], "reply_model_failed")
        text = bot._format_comment_context_record(bot.logs[-1])
        self.assertIn("回复模型调用异常", text)
        self.assertIn("不能据此确认查询能力正常", text)

    async def test_timeout_log_preserves_failure_when_final_decision_succeeds(self):
        bot = Bot([response("tool", "video_subtitles"), response("ignore")])
        bot._get_video_subtitles.side_effect = asyncio.TimeoutError
        self.assertEqual((await bot.flow())["decision"], "ignore")
        text = bot._format_comment_context_record(bot.logs[-1])
        self.assertIn("未取得结果", text)
        self.assertIn("查询超时", text)
        self.assertIn("最终决定：不回复", text)

    async def test_unknown_admin_tools_are_never_exposed(self):
        bot = Bot([response("tool", "shell", "rm")])
        self.assertEqual((await bot.flow())["decision"], "error")
        bot._get_video_info.assert_not_awaited()

    async def test_actual_generate_reply_uses_flow_not_old_search_judge(self):
        bot = Bot([response("tool", "video_description"), response()])
        bot._get_system_prompt = AsyncMock(return_value="persona")
        bot._is_owner = lambda _: False
        bot._affection = {}
        bot._get_level = lambda *args: "normal"
        bot._get_level_prompts = lambda: {"normal": "normal"}
        bot._sanitize_user_input = lambda content, *args: (content, False, "")
        bot._build_memory_context = AsyncMock(return_value="已有记忆")
        bot._relevant_feedback_context = AsyncMock(return_value="")
        bot._get_today_mood = lambda: ("平静", "")
        bot._get_festival_prompt = lambda: ""
        bot._get_personality_prompt = lambda _: ""
        bot._wrap_user_content = lambda s: s
        bot._should_search_for_reply = AsyncMock()
        result = await bot._generate_reply("具体问题", "1", "昵称", "t", 123, 1)
        self.assertEqual(result["decision"], "reply")
        bot._should_search_for_reply.assert_not_awaited()
        self.assertEqual(bot._get_video_info.await_count, 1)


class ProtocolTests(unittest.TestCase):
    def test_tool_decision_requires_comment_mode_and_no_side_effects(self):
        raw = response("tool", "video_inspect", "what", start_seconds=0, end_seconds=30)
        self.assertEqual(parse_reply_envelope(raw, channel="comment", allow_context_request=True,
            allow_tool_request=True, allowed_tools={"video_inspect"})["decision"], "tool")
        for channel in ("private", "live"):
            with self.assertRaises(ReplyProtocolError):
                parse_reply_envelope(raw, channel=channel, allow_context_request=True, allow_tool_request=True, allowed_tools={"video_inspect"})
        value = json.loads(raw); value["reply"] = "我去看一看"
        with self.assertRaises(ReplyProtocolError):
            parse_reply_envelope(json.dumps(value), channel="comment", allow_context_request=True, allow_tool_request=True, allowed_tools={"video_inspect"})

    def test_invalid_ranges_and_unrelated_video_parameters_are_rejected(self):
        for start, end in ((-1, 1), (10, 1), (0, 181), (True, 4), (0, float("inf"))):
            with self.assertRaises(ReplyProtocolError):
                parse_reply_envelope(response("tool", "video_inspect", "q", start_seconds=start, end_seconds=end),
                    channel="comment", allow_context_request=True, allow_tool_request=True, allowed_tools={"video_inspect"})
        with self.assertRaises(ReplyProtocolError):
            parse_reply_envelope(response("tool", "video_description", "another BV"), channel="comment",
                allow_context_request=True, allow_tool_request=True, allowed_tools={"video_description"})


class InspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_builder_uses_light_comment_path(self):
        bot = types.SimpleNamespace(config={},
            _reader_scope_for_channel=lambda _: "bili_comment", _load_json=lambda _, default: default,
            _get_comment_video_memory_context=AsyncMock(return_value=("已有视频记忆", {"bvid": BV})),
            _get_video_context=AsyncMock(side_effect=AssertionError("must not auto-watch")),
            _get_bvid_memories=lambda *a, **k: [], _get_thread_memories=lambda _: [],
            _get_oid_memories=lambda *a, **k: [], _get_user_profile_context=lambda *a: "",
            _search_global_relevant=AsyncMock(return_value=[]))
        result = await MemoryMixin._build_memory_context(bot, "thread", "user", "问题", oid=123)
        self.assertIn("已有视频记忆", result)
        bot._get_video_context.assert_not_awaited()

    async def test_identity_context_uses_memory_without_new_watch_or_hot_comments(self):
        bot = types.SimpleNamespace(_get_video_info=AsyncMock(return_value={"bvid": BV, "title": "测试", "desc": "不应自动插入"}),
            _cached_video_memory=lambda _: {"summary": "旧视频总结", "review": "当时感想"},
            _clip_media_text=lambda s, limit: s[:limit])
        context, _ = await VideoMixin._get_comment_video_memory_context(bot, 1)
        self.assertIn("没有重新观看", context)
        self.assertNotIn("不应自动插入", context)

    async def test_inspection_cache_ranges_audio_and_cleanup(self):
        class Inspector(VideoMixin):
            pass
        bot = Inspector(); bot.config = {"VIDEO_VISION_PROVIDER_ID": "fake"}
        files = {}
        bot._load_json = lambda key, default: copy.deepcopy(files.get(key, default))
        bot._append_json_list = lambda key, row, cap: files.setdefault(key, []).append(copy.deepcopy(row))
        bot._get_video_vision_client = lambda: None
        directories = []
        async def download(bvid, *, output_dir, duration=None):
            directories.append(output_dir)
            file = Path(output_dir) / (bvid + ".mp4"); file.write_bytes(b"test")
            return str(file)
        bot._download_video = AsyncMock(side_effect=download)
        bot._get_video_duration = AsyncMock(return_value=300)
        commands = []
        async def run(*args, **kwargs):
            commands.append(args); Path(args[-1]).write_bytes(b"clip")
            return 0, "", ""
        bot._run_process = run
        bot._inspect_video_clip = AsyncMock(side_effect=lambda *args: {"finding": "found", "evidence": "看到猫", "moments": [{"start_seconds": 2, "end_seconds": 3}], "mode": "video"})
        info = {"bvid": BV, "duration": 300}
        with tempfile.TemporaryDirectory() as directory, patch.object(inspection_module, "TEMP_VIDEO_DIR", directory):
            result = await bot._inspect_comment_video(info, "有没有猫", 120, 150)
            again = await bot._inspect_comment_video(info, "有没有猫", 120, 150)
        self.assertTrue(again["from_cache"])
        self.assertEqual(result["segments"][0]["moments"][0]["start_seconds"], 122)
        self.assertEqual(bot._download_video.await_count, 1)
        self.assertNotIn("-an", commands[0])
        self.assertIn("0:a?", commands[0])
        self.assertTrue(all(not Path(directory).exists() for directory in directories))
        self.assertIn("不证明整条视频没有", result["limitation"])

    async def test_video_question_is_sent_and_frame_fallback_has_no_audio_claim(self):
        class Inspector(VideoMixin):
            pass
        bot = Inspector(); bot.config = {"VIDEO_VISION_FORMAT": "gemini"}
        answer = json.dumps({"finding": "uncertain", "evidence": "无法判断声音", "moments": []})
        bot._astrbot_multimodal_generate = AsyncMock(side_effect=["not JSON", answer])
        with tempfile.TemporaryDirectory() as directory:
            clip = Path(directory) / "clip.mp4"; clip.write_bytes(b"video")
            frame = Path(directory) / "frame_0.jpg"; frame.write_bytes(b"frame")
            bot._extract_video_frames = AsyncMock(return_value=[str(frame)])
            result = await bot._inspect_video_clip(str(clip), "是否说了谢谢", 30, "provider", None, "gemini")
        self.assertEqual(result["mode"], "frames")
        self.assertIn("是否说了谢谢", str(bot._astrbot_multimodal_generate.call_args_list[0]))
        self.assertIn("没有音频", str(bot._astrbot_multimodal_generate.call_args))

    async def test_private_download_directory_does_not_delete_shared_video(self):
        bot = VideoMixin(); bot.config = {}
        bot._format_fallbacks = lambda _: ["first", "second"]
        calls = 0
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / (BV + ".mp4"); shared.write_bytes(b"shared")
            output = Path(directory) / "inspection_test"; output.mkdir()
            async def run(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    (output / (BV + ".mp4.part")).write_bytes(b"partial")
                    return 1, "", "format failed"
                self.assertFalse((output / (BV + ".mp4.part")).exists())
                (output / (BV + ".mp4")).write_bytes(b"inspection")
                return 0, "", ""
            bot._run_process = run
            result = await bot._download_video(BV, output_dir=str(output))
            self.assertEqual(Path(result).read_bytes(), b"inspection")
            self.assertEqual(shared.read_bytes(), b"shared")
            self.assertFalse((output / (BV + "_cookies.txt")).exists())

    def test_selection_spreads_long_video_and_inspection_rejects_invented_times(self):
        self.assertEqual(inspection_intervals(600), [(0, 60), (270, 330), (540, 600)])
        self.assertEqual(inspection_intervals(100, 90, 120), [(90, 100)])
        for raw in ({"finding": "absent", "evidence": "none", "moments": []},
                    {"finding": "found", "evidence": "yes", "moments": [{"start_seconds": 0, "end_seconds": 99}]}):
            with self.assertRaises(ValueError): parse_inspection(json.dumps(raw), 60)

    async def test_cancelling_external_process_reaps_it(self):
        entered = asyncio.Event()
        count = 0
        async def communicate():
            nonlocal count
            count += 1
            if count == 1:
                entered.set(); await asyncio.sleep(20)
            return b"", b""
        proc = types.SimpleNamespace(returncode=None, communicate=communicate, kill=Mock())
        bot = UtilsMixin(); bot._find_command = lambda _: "ffmpeg"
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            task = asyncio.create_task(bot._run_process("ffmpeg"))
            await entered.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        proc.kill.assert_called_once()
        self.assertEqual(count, 2)
