"""写评论：两个模型不同时可以先查（问视频模型、拉字幕、联网）；同一个时看视频那次直接写，草稿过门槛才发。"""
import json
import types
import unittest
from unittest.mock import AsyncMock

from test_video_behavior_upgrade import BV, Bot
from tool_fakes import ScriptedToolLoop, WithToolFakes, call

from core.features._video_evaluate import VideoEvaluate
from core.features._watch_video import WatchVideo
from core.infra.llm import LLMGateway


INFO = {"bvid": BV, "title": "宋朝的茶", "up_name": "UP", "tname": "人文历史", "cid": 22, "duration": 300}
COMMENT = '{"decision":"comment","text":"3:10 那句「茶百戏」原来是这个意思"}'


class SameModelRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_model_gets_persona_guide_and_hot_comments(self):
        bot = Bot()
        bot.config.update(VIDEO_VISION_PROVIDER_ID="vp")
        bot.generation_route = lambda: ("provider", "vp")
        bot._get_system_prompt = AsyncMock(return_value="人设")
        request = await VideoEvaluate(bot).comment_watch_request(INFO, ["热评一"], ["宋朝"])
        self.assertEqual(request["system_prompt"], "人设")
        self.assertIn("看完这个视频，你还要想想要不要去评论区说点什么", request["prompt"])
        self.assertIn("段评", request["prompt"])
        self.assertIn("- 热评一", request["prompt"])
        self.assertIn("标签：宋朝", request["prompt"])
        self.assertNotIn("你看视频时记下的", request["prompt"])  # 它自己在看，没有记录可附

    async def test_different_or_missing_model_gets_none(self):
        bot = Bot()
        bot.config.update(VIDEO_VISION_PROVIDER_ID="vp")
        bot.generation_route = lambda: ("provider", "chat")
        self.assertIsNone(await VideoEvaluate(bot).comment_watch_request(INFO))
        bot.config.update(VIDEO_VISION_PROVIDER_ID="")
        bot.generation_route = lambda: None
        self.assertIsNone(await VideoEvaluate(bot).comment_watch_request(INFO))

    async def test_independent_api_matches_by_address_and_model(self):
        bot = Bot()
        bot.config.update(VIDEO_VISION_API_KEY="k", VIDEO_VISION_API_BASE="https://example.com", VIDEO_VISION_MODEL="m")
        route = bot.video_watch.video_route()
        self.assertEqual(route[0], "api")
        self.assertEqual(route[2], "m")
        bot.generation_route = lambda: route
        bot._get_system_prompt = AsyncMock(return_value="人设")
        self.assertIsNotNone(await VideoEvaluate(bot).comment_watch_request(INFO))


class CommentToolTests(WithToolFakes, unittest.IsolatedAsyncioTestCase):
    def bot(self, script, **config):
        bot = Bot()
        bot.config.update({"VIDEO_VISION_PROVIDER_ID": "vp", **config})
        bot._get_system_prompt = AsyncMock(return_value="人设")
        bot._llm_call = AsyncMock()
        bot._generation_llm_call = AsyncMock(return_value=COMMENT)
        bot.generation_tool_loop = ScriptedToolLoop(script)
        bot.generation_route = lambda: ("provider", "chat")
        return bot

    async def test_tools_offered_and_subtitles_come_with_time(self):
        bot = self.bot([call("video_subtitles", start_seconds=180, end_seconds=200), COMMENT])
        bot.bili_videos._get_video_subtitle_lines = AsyncMock(return_value=[
            {"from": 12.0, "to": 14.0, "content": "开头"}, {"from": 190.0, "to": 192.0, "content": "这就是茶百戏"}])
        text = await VideoEvaluate(bot)._generate_proactive_comment(INFO, "印象", ["热评"], ["宋朝"])
        self.assertEqual(text, "3:10 那句「茶百戏」原来是这个意思")
        loop = bot.generation_tool_loop
        self.assertEqual(loop.calls[0]["tools"], ["video_inspect", "video_subtitles"])
        self.assertEqual(loop.calls[0]["system_prompt"], "人设")
        self.assertIn("【按需查】", loop.calls[0]["prompt"])
        name, result = loop.results[0]
        self.assertIn("[03:10] 这就是茶百戏", result)
        self.assertNotIn("开头", result)
        self.assertIn('trust="untrusted"', result)
        bot._generation_llm_call.assert_not_awaited()

    async def test_inspect_asks_video_model_with_range(self):
        bot = self.bot([call("video_inspect", query="3分10秒说的是什么", start_seconds=180, end_seconds=220), COMMENT])
        bot.video_watch._inspect_comment_video = AsyncMock(return_value={"status": "ok", "finding": "found", "evidence": "说了茶百戏"})
        await VideoEvaluate(bot)._generate_proactive_comment(INFO, "印象")
        bot.video_watch._inspect_comment_video.assert_awaited_once_with(INFO, "3分10秒说的是什么", 180.0, 220.0)
        self.assertIn("说了茶百戏", bot.generation_tool_loop.results[0][1])

    async def test_bad_range_and_missing_subtitles_told_to_model(self):
        bot = self.bot([call("video_inspect", query="问", start_seconds=10), call("video_subtitles"), COMMENT])
        bot.bili_videos._get_video_subtitle_lines = AsyncMock(return_value=[])
        await VideoEvaluate(bot)._generate_proactive_comment(INFO, "印象")
        results = dict(bot.generation_tool_loop.results)
        self.assertIn("同时填", results["video_inspect"])
        self.assertIn("没有可用的B站字幕", results["video_subtitles"])

    async def test_web_search_only_when_configured(self):
        bot = self.bot([COMMENT], ENABLE_WEB_SEARCH=True, WEB_SEARCH_API_KEY="k")
        await VideoEvaluate(bot)._generate_proactive_comment(INFO, "印象")
        self.assertIn("web_search", bot.generation_tool_loop.calls[0]["tools"])

    async def test_independent_api_generation_gets_no_tools(self):
        bot = self.bot([COMMENT])
        bot.generation_route = lambda: ("api", "https://example.com/v1", "m")
        await VideoEvaluate(bot)._generate_proactive_comment(INFO, "印象")
        self.assertEqual(bot.generation_tool_loop.calls, [])
        self.assertNotIn("【按需查】", bot._generation_llm_call.call_args.args[0])

    async def test_no_video_model_and_no_cid_means_plain_call(self):
        bot = self.bot([COMMENT], VIDEO_VISION_PROVIDER_ID="")
        await VideoEvaluate(bot)._generate_proactive_comment({**INFO, "cid": 0}, "印象")
        self.assertEqual(bot.generation_tool_loop.calls, [])
        bot._generation_llm_call.assert_awaited_once()


class DraftFlowTests(unittest.IsolatedAsyncioTestCase):
    def round(self):
        return types.SimpleNamespace(per_video_comment=True, comment_count=0, daily_comment_limit=3)

    async def test_request_prepared_only_when_comment_possible(self):
        bot = Bot()
        bot.config.update(VIDEO_VISION_PROVIDER_ID="vp", PROACTIVE_COMMENT=True)
        bot.video_evaluate.comment_watch_request = AsyncMock(return_value={"system_prompt": "人设", "prompt": "p"})
        info = dict(INFO)
        await WatchVideo(bot)._prepare_comment_draft(self.round(), 11, info)
        self.assertEqual(info["_comment_request"]["prompt"], "p")
        self.assertIn("_hot_comments", info)
        full = types.SimpleNamespace(per_video_comment=True, comment_count=3, daily_comment_limit=3)
        info = dict(INFO)
        await WatchVideo(bot)._prepare_comment_draft(full, 11, info)
        self.assertNotIn("_comment_request", info)

    async def test_draft_used_instead_of_writing_again(self):
        bot = Bot()
        bot.video_evaluate._generate_proactive_comment = AsyncMock(return_value="不该被调")
        bot.bili_comments._send_comment = AsyncMock(return_value=(True, ""))
        sent = []

        async def execute(key, kind, bvid, action, metadata=None):
            sent.append(metadata["text"])
            return types.SimpleNamespace(success=False)

        bot.outward_actions._execute_proactive_action = execute
        watch_round = types.SimpleNamespace(action_key_prefix="proactive")
        await WatchVideo(bot)._send_proactive_video_comment(
            watch_round, {"bvid": BV}, 11, {**INFO, "_comment_draft": "草稿"}, "印象", [])
        self.assertEqual(sent, ["草稿"])
        bot.video_evaluate._generate_proactive_comment.assert_not_awaited()

    async def test_skip_draft_sends_nothing(self):
        bot = Bot()
        bot.video_evaluate._generate_proactive_comment = AsyncMock(return_value="不该被调")
        bot.outward_actions._execute_proactive_action = AsyncMock()
        await WatchVideo(bot)._send_proactive_video_comment(
            types.SimpleNamespace(), {"bvid": BV}, 11, {**INFO, "_comment_draft": ""}, "印象", [])
        bot.outward_actions._execute_proactive_action.assert_not_awaited()
        bot.video_evaluate._generate_proactive_comment.assert_not_awaited()


class HintAndSensitiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_hint_and_sensitive_reach_comment_writer(self):
        bot = Bot()
        bot._llm_call = AsyncMock()
        bot._get_system_prompt = AsyncMock(return_value="人设")
        bot._generation_llm_call = AsyncMock(return_value='{"decision":"skip","text":""}')
        info = {**INFO, "cid": 0, "_comment_hint": "适合补充茶具的知识", "_sensitive": True}
        await VideoEvaluate(bot)._generate_proactive_comment(info, "印象")
        material = bot._generation_llm_call.call_args.args[0].split('<video_material trust="untrusted">', 1)[1]
        self.assertIn("评论建议（只是参考）：适合补充茶具的知识", material)
        self.assertIn("不表态、不站队", material)

    def test_sensitive_videos_pass_by_chance(self):
        from unittest.mock import patch
        bot = Bot()
        watch = WatchVideo(bot)
        self.assertTrue(watch._sensitive_comment_allowed({}))
        bot.config.update(PROACTIVE_SENSITIVE_COMMENT_CHANCE=0.3)
        with patch("core.features._watch_video.random.random", return_value=0.5):
            self.assertFalse(watch._sensitive_comment_allowed({"_sensitive": True}))
        with patch("core.features._watch_video.random.random", return_value=0.1):
            self.assertTrue(watch._sensitive_comment_allowed({"_sensitive": True}))
        bot.config.update(PROACTIVE_SENSITIVE_COMMENT_CHANCE=0)
        with patch("core.features._watch_video.random.random", return_value=0.0):
            self.assertFalse(watch._sensitive_comment_allowed({"_sensitive": True}))


class RouteTests(unittest.TestCase):
    def gateway(self, **config):
        from core.settings import Settings
        gateway = LLMGateway.__new__(LLMGateway)
        settings = Settings(config)
        gateway._settings = lambda: settings
        gateway._context = lambda: types.SimpleNamespace()
        return gateway

    def test_purpose_route(self):
        self.assertEqual(self.gateway(GENERATION_PROVIDER_ID="p").purpose_route("GENERATION"), ("provider", "p"))
        route = self.gateway(GENERATION_API_KEY="k", GENERATION_API_BASE="https://example.com", GENERATION_MODEL="m").purpose_route("generation")
        self.assertEqual((route[0], route[2]), ("api", "m"))
        self.assertEqual(self.gateway(GENERATION_PROVIDER_ID="p", LLM_PROVIDER_ID="x").purpose_route("GENERATION"), ("provider", "p"))
        self.assertEqual(self.gateway(LLM_PROVIDER_ID="chat").purpose_route("GENERATION"), ("provider", "chat"))
        self.assertIsNone(self.gateway(GENERATION_API_KEY="k").purpose_route("GENERATION"))


if __name__ == "__main__":
    unittest.main()
