"""Offline end-to-end checks of the comment model's bounded read-only tool flow."""
import asyncio
import copy
from activity_parts import ListLog
from datetime import datetime
import json
from pathlib import Path
import shutil
import tempfile
import types
import unittest

from bili_parts import attach_bili_groups
from unittest.mock import AsyncMock, Mock, patch

from test_autonomous_ranges import _install_astrbot_stub
from test_output_protocol import valid_payload
from reply_parts import ReplyParts
from vision_fakes import vision_on
from core.features.comment_context import CommentContext
from core import config
from core.settings import Settings
from core.domain.affection import Affection
from core.storage.json_file import JsonFile
from core.features.reply_context import ReplyContext
from video_parts import VideoParts
from base_parts import BaseParts, attach_base
from core.domain.video_watch import VideoWatch, inspection_intervals, parse_inspection
import core.domain.video_watch as inspection_module
from itself import itself
from tool_fakes import ScriptedToolLoop, WithToolFakes, call

BV = "BV1xx411c7mD"


def response(decision="reply", name="none", query="", **extra):
    payload = valid_payload(decision=decision, tool_request={"name": name, "query": query, **extra})
    if decision != "reply":
        payload.update(reply="", score_delta=0, impression="", user_facts=[])
    return json.dumps(payload, ensure_ascii=False)


class Bot(ReplyParts):
    models = itself
    web_search = itself
    search_judge = itself
    vision = property(vision_on)

    def __init__(self, script):
        self.config = {"ENABLE_WEB_SEARCH": True, "WEB_SEARCH_API_KEY": "fake"}
        self.tool_loop = ScriptedToolLoop(script)
        self.bili_videos._get_video_info = AsyncMock(return_value={"bvid": BV, "cid": 2, "title": "作品", "desc": "介绍", "duration": 300})
        self.bili_videos._get_video_subtitles = AsyncMock(return_value="相关字幕")
        self.bili_comments._get_hot_comments = AsyncMock(return_value=["外部热评"])
        self._web_search = AsyncMock(return_value="外部搜索结果")
        self.video_watch._inspect_comment_video = AsyncMock(return_value={"status": "ok", "segments": [{"finding": "uncertain"}]})
        self.logs = []
        self.activity_logs.comment_context = ListLog(self.logs)
        self.comment_context = CommentContext(self)

    async def flow(self, **kw):
        return await self.comment_context._run_comment_reply_flow("评论和已有记忆", "personality", 123, 1, "thread", uid="1", **kw)

    def answers(self):
        """每次工具调用交回模型的文字。"""
        return [text for _name, text in self.tool_loop.results]


class FlowTests(WithToolFakes, unittest.IsolatedAsyncioTestCase):
    async def test_direct_reply_or_silence_does_not_fetch(self):
        for decision in ("reply", "ignore", "observe"):
            bot = Bot([response(decision)])
            result = await bot.flow()
            self.assertEqual(result["decision"], decision)
            self.assertEqual(len(bot.tool_loop.calls), 1)
            bot.bili_videos._get_video_info.assert_not_awaited()
            bot._web_search.assert_not_awaited()

    async def test_offers_real_tools_with_a_budget_and_a_comment_event(self):
        bot = Bot([response()])
        await bot.flow()
        call_args = bot.tool_loop.calls[0]
        self.assertEqual(call_args["tools"], ["video_description", "video_subtitles", "video_hot_comments",
                                              "video_inspect", "bili_up_info", "bili_video_info",
                                              "bili_bangumi_search", "bili_memory", "watch_video", "bili_memo",
                                              "web_search"])
        self.assertEqual(call_args["max_steps"], 5)
        self.assertEqual(call_args["system_prompt"], "personality")
        self.assertEqual(call_args["event"].session_id, "comment:1")
        self.assertIn("最多还能查4次", call_args["prompt"])
        self.assertNotIn("decision=tool", call_args["prompt"])
        self.assertNotIn("|tool", call_args["prompt"])
        # 总时限：4 次查询（2 次核查、1 次看推荐视频）都用满，再加 5 轮模型调用。
        self.assertEqual(call_args["timeout"], 2 * 240 + 900 + 25 + 5 * 90)
        self.assertEqual(call_args["tool_call_timeout"], 910)

    async def test_memory_tool_reads_as_the_comment_domain(self):
        bot = Bot([call("bili_memory", kind="video", query="手绘"), response()])
        bot.memory_recall = types.SimpleNamespace(_search_memories=AsyncMock(return_value=["看过手绘视频"]))
        await bot.flow()
        self.assertEqual(bot.memory_recall._search_memories.await_args.kwargs["reader_scope"], "bili_comment")
        self.assertIn("看过手绘视频", bot.answers()[0])

    async def test_subtitles_then_targeted_inspection_then_final_reply(self):
        bot = Bot([call("video_subtitles"),
                   call("video_inspect", query="最后是否出现猫？", start_seconds=260, end_seconds=290),
                   response()])
        result = await bot.flow()
        self.assertEqual(result["decision"], "reply")
        self.assertEqual([x["tool"] for x in result["_context_trace"]], ["video_subtitles", "video_inspect"])
        bot.video_watch._inspect_comment_video.assert_awaited_once_with(bot.bili_videos._get_video_info.return_value, "最后是否出现猫？", 260.0, 290.0)
        self.assertEqual(bot.bili_videos._get_video_info.await_count, 1)
        subtitles, inspection = bot.answers()
        self.assertIn("相关字幕", subtitles)
        self.assertIn('trust="untrusted"', subtitles)
        self.assertIn("uncertain", inspection)

    async def test_search_then_ignore_does_not_publish_intermediate_reply(self):
        bot = Bot([call("web_search", query="公开事实"), response("ignore")])
        result = await bot.flow()
        self.assertEqual(result["reply"], "")
        self.assertEqual(result["score_delta"], 0)
        self.assertEqual(bot.logs[-1]["decision"], "ignore")
        self.assertFalse(await bot.reply_aftermath._apply_reply_result(mid="1", username="u", content="c", oid=123,
            rpid=1, comment_type=1, thread_id="t", result=result))

    async def test_repeated_request_executes_only_once(self):
        bot = Bot([call("web_search", query="公开事实")] * 4 + [response()])
        result = await bot.flow()
        self.assertEqual(result["decision"], "reply")
        self.assertEqual(bot._web_search.await_count, 1)
        self.assertEqual([row["status"] for row in result["_context_trace"]], ["ok", "duplicate", "duplicate", "duplicate"])

    async def test_requests_after_budget_are_refused_without_execution(self):
        bot = Bot([call("web_search", query="one"), call("web_search", query="two"), response()])
        bot.config["COMMENT_CONTEXT_MAX_CALLS"] = 1
        result = await bot.flow()
        self.assertEqual(result["decision"], "reply")
        self.assertEqual(bot._web_search.await_count, 1)
        self.assertEqual(result["_context_trace"][-1]["status"], "budget")
        self.assertEqual(bot.tool_loop.calls[0]["max_steps"], 2)

    async def test_zero_budget_offers_no_tools(self):
        bot = Bot([response()])
        bot.config["COMMENT_CONTEXT_MAX_CALLS"] = 0
        await bot.flow()
        self.assertEqual(bot.tool_loop.calls[0]["tools"], [])
        self.assertIsNone(bot.tool_loop.calls[0]["event"])
        self.assertEqual(bot.tool_loop.calls[0]["timeout"], 90)

    async def test_video_inspection_is_limited_to_two_distinct_requests(self):
        bot = Bot([call("video_inspect", query=question) for question in ("是否有猫？", "是否有狗？", "是否有鸟？")]
                  + [response("ignore")])
        result = await bot.flow()
        self.assertEqual(result["decision"], "ignore")
        self.assertEqual(bot.video_watch._inspect_comment_video.await_count, 2)
        self.assertEqual(result["_context_trace"][2]["status"], "limit")
        self.assertIn("次数用完", bot.answers()[2])

    async def test_bad_inspection_range_is_explained_and_not_run(self):
        for start, end in ((-1, 1), (10, 1), (0, 181), (0, None), (0, float("inf"))):
            bot = Bot([call("video_inspect", query="q", start_seconds=start, end_seconds=end), response("ignore")])
            await bot.flow()
            bot.video_watch._inspect_comment_video.assert_not_awaited()
            self.assertIn("时间段", bot.answers()[0])

    async def test_query_tools_need_a_query(self):
        bot = Bot([call("web_search"), response("ignore")])
        await bot.flow()
        bot._web_search.assert_not_awaited()
        self.assertIn("query", bot.answers()[0])

    async def test_unavailable_material_can_be_followed_by_silence(self):
        bot = Bot([call("video_subtitles"), response("ignore")])
        bot.bili_videos._get_video_subtitles.return_value = ""
        result = await bot.flow()
        self.assertEqual(result["decision"], "ignore")
        self.assertEqual(result["_context_trace"][0]["status"], "unavailable")
        self.assertEqual(bot.answers(), ["无可用字幕"])

    async def test_tool_timeout_cancels_work_then_allows_final_decision(self):
        bot = Bot([call("web_search", query="公开事实"), response("ignore")])
        cancelled = asyncio.Event()
        async def search(_):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        bot._web_search.side_effect = search
        bot.comment_context._COMMENT_CONTEXT_TOOL_TIMEOUT = 0.01
        result = await bot.flow()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result["decision"], "ignore")
        self.assertIn("超时", bot.answers()[0])

    async def test_malformed_output_is_an_error(self):
        bot = Bot(['{"decision":"tool"'])
        self.assertEqual((await bot.flow())["error"], "invalid_model_output")
        bot.bili_videos._get_video_info.assert_not_awaited()

    async def test_old_json_tool_requests_are_no_longer_accepted(self):
        bot = Bot([response("tool", "video_description")])
        self.assertEqual((await bot.flow())["error"], "invalid_model_output")
        bot.bili_videos._get_video_info.assert_not_awaited()

    async def test_disabled_or_suspicious_or_wrong_comment_type_blocks_tools(self):
        for mode in ("disabled", "suspicious", "dynamic", "web_disabled"):
            name = "web_search" if mode == "web_disabled" else "video_inspect"
            bot = Bot([call(name, query="question"), response()])
            if mode == "disabled": bot.config["COMMENT_CONTEXT_ENABLED"] = False
            if mode == "web_disabled": bot.config["ENABLE_WEB_SEARCH"] = False
            await bot.comment_context._run_comment_reply_flow("p", "s", 123, 17 if mode == "dynamic" else 1, "t", suspicious=mode == "suspicious")
            self.assertNotIn(name, bot.tool_loop.calls[0]["tools"], mode)
            bot.video_watch._inspect_comment_video.assert_not_awaited()
            bot._web_search.assert_not_awaited()

    async def test_single_switch_replaces_legacy_video_switch(self):
        bot = Bot([call("video_inspect", query="是否有猫？"), response()])
        bot.config["COMMENT_VIDEO_INSPECTION_ENABLED"] = False
        self.assertEqual((await bot.flow())["decision"], "reply")
        bot.video_watch._inspect_comment_video.assert_awaited_once()
        bot.config["COMMENT_CONTEXT_ENABLED"] = False
        self.assertEqual(bot.comment_context._comment_context_tools(123, 1), [])

    async def test_manual_diagnostic_uses_real_flow_but_never_applies_reply(self):
        bot = Bot([call("video_description"), response()])
        bot.config["BILI_TOOL_AUDIT_ENABLED"] = False
        bot.bili_videos._get_video_oid = AsyncMock(return_value=123)
        bot._get_system_prompt = AsyncMock(return_value="persona")
        bot.reply_aftermath._apply_reply_result = AsyncMock()
        text = await bot.comment_context._test_comment_context(BV)
        self.assertIn("简介：成功", text)
        self.assertIn("回复预览（未发布）", text)
        self.assertEqual(bot.logs[-1]["mode"], "test")
        bot.reply_aftermath._apply_reply_result.assert_not_awaited()
        bot.json_store._load_json = lambda *args: bot.logs
        self.assertIn("只读实测", bot.comment_context._render_comment_context_log(bot.logs[-1]["time"][:10]))

    async def test_manual_diagnostic_respects_switch_and_validation(self):
        bot = Bot([])
        bot.bili_videos._get_video_oid = AsyncMock(return_value=123)
        self.assertIn("完整 BV", await bot.comment_context._test_comment_context("invalid"))
        bot.config["COMMENT_CONTEXT_ENABLED"] = False
        self.assertIn("已关闭", await bot.comment_context._test_comment_context(BV))
        bot.bili_videos._get_video_oid.assert_not_awaited()
        self.assertEqual(bot.tool_loop.calls, [])

    async def test_model_error_produces_readable_failure_log(self):
        bot = Bot([RuntimeError("provider failed")])
        result = await bot.flow()
        self.assertEqual(result["error"], "reply_model_failed")
        text = bot.comment_context._format_comment_context_record(bot.logs[-1])
        self.assertIn("回复模型调用异常", text)
        self.assertIn("不能据此确认查询能力正常", text)

    async def test_no_answer_from_the_model_is_an_error(self):
        bot = Bot([None])
        self.assertEqual((await bot.flow())["error"], "model_unavailable")

    async def test_timeout_log_preserves_failure_when_final_decision_succeeds(self):
        bot = Bot([call("video_subtitles"), response("ignore")])
        bot.bili_videos._get_video_subtitles.side_effect = asyncio.TimeoutError
        self.assertEqual((await bot.flow())["decision"], "ignore")
        text = bot.comment_context._format_comment_context_record(bot.logs[-1])
        self.assertIn("未取得结果", text)
        self.assertIn("查询超时", text)
        self.assertIn("最终决定：不回复", text)

    def test_old_log_records_still_render(self):
        bot = Bot([])
        text = bot.comment_context._format_comment_context_record({
            "time": "2026-11-01T10:00:00", "mode": "comment", "decision": "reply",
            "calls": [{"tool": "video_inspect", "status": "ok", "cached": True, "seconds": 3, "reason": ""},
                      {"tool": "web_search", "status": "unavailable", "seconds": 1, "reason": "timeout"}],
        })
        self.assertIn("视频核查：成功，读取缓存", text)
        self.assertIn("联网搜索：未取得结果，1秒；查询超时", text)

    async def test_unknown_admin_tools_are_never_exposed(self):
        bot = Bot([call("shell", query="rm"), response("ignore")])
        self.assertEqual((await bot.flow())["decision"], "ignore")
        self.assertNotIn("shell", bot.tool_loop.calls[0]["tools"])
        self.assertIn("未找到工具", bot.answers()[0])
        bot.bili_videos._get_video_info.assert_not_awaited()

    async def test_actual_generate_reply_uses_flow_not_old_search_judge(self):
        bot = Bot([call("video_description"), response()])
        bot._get_system_prompt = AsyncMock(return_value="persona")
        bot.roles = types.SimpleNamespace(is_owner=lambda _: False)
        affection_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, affection_dir, True)
        bot.affection = Affection(JsonFile(Path(affection_dir) / "affection.json"), lambda _uid: False)
        bot.reply_writing._level_prompts = lambda: {"stranger": "stranger"}
        bot.inbound_guard = types.SimpleNamespace(sanitize=lambda content, *args: (content, False, ""))
        bot.reply_context = types.SimpleNamespace(_build_memory_context=AsyncMock(return_value="已有记忆"))
        bot.reply_writing._relevant_feedback_context = AsyncMock(return_value="")
        bot.mood = types.SimpleNamespace(today=lambda: ("平静", ""), festival=lambda: "")
        bot.personality = types.SimpleNamespace(prompt=lambda _: "")
        bot._should_search_for_reply = AsyncMock()
        result = await bot.reply_writing._generate_reply("具体问题", "1", "昵称", "t", 123, 1)
        self.assertEqual(result["decision"], "reply")
        bot._should_search_for_reply.assert_not_awaited()
        self.assertEqual(bot.bili_videos._get_video_info.await_count, 1)
        self.assertEqual(bot.tool_loop.calls[0]["event"].session_id, "comment:1")


class InspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_builder_uses_light_comment_path(self):
        bot = attach_base(types.SimpleNamespace(config={}, settings=Settings({}),
            _load_json=lambda _, default: default,
            video_watch=types.SimpleNamespace(_get_comment_video_memory_context=AsyncMock(return_value=("已有视频记忆", {"bvid": BV}))),
            video_context=types.SimpleNamespace(_get_video_context=AsyncMock(side_effect=AssertionError("must not auto-watch"))),
            memory_recall=types.SimpleNamespace(
                _reader_scope_for_channel=lambda _: "bili_comment",
                _get_bvid_memories=AsyncMock(return_value=[]), _get_thread_memories=AsyncMock(return_value=[]),
                _get_oid_memories=AsyncMock(return_value=[]), _search_global_relevant=AsyncMock(return_value=[]),
                _get_user_elsewhere_memories=AsyncMock(return_value=[]), _get_up_video_memories=AsyncMock(return_value=[])),
            user_profiles=types.SimpleNamespace(context=lambda *a: "")))
        result = await ReplyContext(bot)._build_memory_context("thread", "user", "问题", oid=123)
        self.assertIn("已有视频记忆", result)
        bot.video_context._get_video_context.assert_not_awaited()

    async def test_identity_context_uses_memory_without_new_watch_or_hot_comments(self):
        bot = types.SimpleNamespace(_get_video_info=AsyncMock(return_value={"bvid": BV, "title": "测试", "desc": "不应自动插入"}),
            _load_json=lambda path, default=None: {
                BV: {"summary": "旧视频总结", "review": "当时感想", "time": datetime.now().strftime("%Y-%m-%d %H:%M")},
            } if path == config.VIDEO_MEMORY_FILE else default,
            _save_json=lambda *_: None, settings=Settings({}))
        attach_bili_groups(bot)
        context, _ = await VideoWatch(bot)._get_comment_video_memory_context(1)
        self.assertIn("没有重新观看", context)
        self.assertNotIn("不应自动插入", context)

    async def test_inspection_cache_ranges_audio_and_cleanup(self):
        class Inspector(VideoParts):
            models = itself
            vision = property(vision_on)
        bot = Inspector(); bot.config = {"VIDEO_VISION_PROVIDER_ID": "fake"}
        files = {}
        bot.json_store._load_json = lambda key, default: copy.deepcopy(files.get(key, default))
        bot.activity_logs.video_inspection = ListLog(files.setdefault("video_inspection", []))
        bot._get_video_vision_client = lambda: None
        directories = []
        async def download(bvid, *, output_dir, duration=None):
            directories.append(output_dir)
            file = Path(output_dir) / (bvid + ".mp4"); file.write_bytes(b"test")
            return str(file)
        bot.video_files._download_video = AsyncMock(side_effect=download)
        bot.video_files._get_video_duration = AsyncMock(return_value=300)
        commands = []
        async def run(*args, **kwargs):
            commands.append(args); Path(args[-1]).write_bytes(b"clip")
            return 0, "", ""
        bot.processes._run_process = run
        bot.video_watch._inspect_video_clip = AsyncMock(side_effect=lambda *args: {"finding": "found", "evidence": "看到猫", "moments": [{"start_seconds": 2, "end_seconds": 3}], "mode": "video"})
        info = {"bvid": BV, "duration": 300}
        with tempfile.TemporaryDirectory() as directory, patch.object(inspection_module, "TEMP_VIDEO_DIR", directory):
            result = await bot.video_watch._inspect_comment_video(info, "有没有猫", 120, 150)
            again = await bot.video_watch._inspect_comment_video(info, "有没有猫", 120, 150)
        self.assertTrue(again["from_cache"])
        self.assertEqual(result["segments"][0]["moments"][0]["start_seconds"], 122)
        self.assertEqual(bot.video_files._download_video.await_count, 1)
        self.assertNotIn("-an", commands[0])
        self.assertIn("0:a?", commands[0])
        self.assertTrue(all(not Path(directory).exists() for directory in directories))
        self.assertIn("不证明整条视频没有", result["limitation"])

    async def test_video_question_is_sent_and_frame_fallback_has_no_audio_claim(self):
        class Inspector(VideoParts):
            models = itself
            vision = property(vision_on)
        bot = Inspector(); bot.config = {"VIDEO_VISION_FORMAT": "gemini"}
        answer = json.dumps({"finding": "uncertain", "evidence": "无法判断声音", "moments": []})
        bot._astrbot_multimodal_generate = AsyncMock(side_effect=["not JSON", answer])
        with tempfile.TemporaryDirectory() as directory:
            clip = Path(directory) / "clip.mp4"; clip.write_bytes(b"video")
            frame = Path(directory) / "frame_0.jpg"; frame.write_bytes(b"frame")
            bot.video_files._extract_video_frames = AsyncMock(return_value=[str(frame)])
            result = await bot.video_watch._inspect_video_clip(str(clip), "是否说了谢谢", 30, "provider", None, "gemini")
        self.assertEqual(result["mode"], "frames")
        self.assertIn("是否说了谢谢", str(bot._astrbot_multimodal_generate.call_args_list[0]))
        self.assertIn("没有音频", str(bot._astrbot_multimodal_generate.call_args))

    async def test_private_download_directory_does_not_delete_shared_video(self):
        bot = VideoParts(); bot.config = {}
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
            bot.processes._run_process = run
            result = await bot.video_files._download_video(BV, output_dir=str(output))
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
        bot = BaseParts(); bot.processes._find_command = lambda _: "ffmpeg"
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            task = asyncio.create_task(bot.processes._run_process("ffmpeg"))
            await entered.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        proc.kill.assert_called_once()
        self.assertEqual(count, 2)
