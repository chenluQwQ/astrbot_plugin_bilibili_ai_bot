"""Behavior checks with fake providers, in-memory logs and no Bilibili writes."""
import asyncio
import copy
import json
import sys
import tempfile
import types
import unittest
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Match the existing suite's import-only AstrBot substitute.
if "astrbot.api.star" not in sys.modules:
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.logger = types.SimpleNamespace(**{key: lambda *a, **k: None for key in
                                        ("info", "debug", "warning", "error")})
    star = types.ModuleType("astrbot.api.star")
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda _: Path(tempfile.gettempdir()) / "bilibot-behavior-tests")
    sys.modules["astrbot.api.star"] = star
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    astrbot.api = api

from core import config
from core.dynamic import DynamicMixin
from core.llm import LLMMixin
from core.memory import MemoryMixin
from core.proactive import ProactiveMixin
from core.share import ShareMixin
from core.video import VideoMixin
from core.storage import Database
from core.storage.stores import PreferenceStore


BV = "BV1xx411c7mD"


class Bot(VideoMixin, ShareMixin, ProactiveMixin, DynamicMixin, MemoryMixin):
    def __init__(self):
        self.config = {
            "ENABLE_VIDEO_LONG_TERM_MEMORY": False,
            "VIDEO_ANALYSIS_MODE": "legacy",
        }
        self.files = {}
        self._memory = []
        self._get_video_oid = AsyncMock(return_value=123)
        self._oid_to_bvid = AsyncMock(return_value=BV)
        self._get_video_info = AsyncMock(return_value={
            "bvid": BV, "title": "木工抽屉", "owner_name": "工匠", "owner_mid": "42",
            "tname": "手工", "desc": "制作抽屉", "duration": 90,
        })
        self._get_video_tags = AsyncMock(return_value=[])
        self._get_hot_comments = AsyncMock(return_value=[])
        self._report_video_history = AsyncMock()  # 已有观看流程会上报历史；离线测试隔离该接口。
        self._save_self_memory_record = AsyncMock()
        self._evaluate_video = AsyncMock(return_value={
            "score": 8.3, "score_reason": "清楚演示榫卯结构", "mood": "开心",
            "review": "最后抽屉合拢时才发现榫头的作用", "preference_signals": [], "search_keywords": [],
        })
        self.analysis_calls = 0

    def _load_json(self, key, default):
        return copy.deepcopy(self.files.get(key, default))

    def _save_json(self, key, value):
        self.files[key] = copy.deepcopy(value)

    async def _analyze_video_with_vision_inner(self, info):
        self.analysis_calls += 1
        await asyncio.sleep(0)
        info["analysis_basis"] = "video_media"
        return "先切割木料，再做榫卯连接，最后演示抽屉合拢。"


class VideoRecallTests(unittest.IsolatedAsyncioTestCase):
    def test_low_token_subtitle_sampler_covers_full_timeline(self):
        text = "\n".join(f"第{i:02d}段内容" for i in range(50))
        sampled = VideoMixin._sample_subtitle_timeline(text, 600)
        self.assertIn("第00段", sampled)
        self.assertIn("第49段", sampled)
        self.assertLessEqual(len(sampled), 600)

    async def test_low_token_subtitle_skips_summary_and_visual_models(self):
        bot = Bot()
        bot.config["VIDEO_ANALYSIS_MODE"] = "pond_low_token"
        bot._get_low_token_subtitle = AsyncMock(
            return_value="开头介绍材料\n中段完成榫卯\n结尾展示抽屉"
        )
        bot._analyze_video_pond_frames = AsyncMock()
        bot._summary_llm_call = AsyncMock()
        info = {"bvid": BV, "title": "木工", "cid": 1}
        result = await VideoMixin._analyze_video_with_vision_inner(bot, info)
        self.assertIn("榫卯", result)
        self.assertEqual(info["analysis_basis"], "subtitles_low_token")
        bot._analyze_video_pond_frames.assert_not_awaited()
        bot._summary_llm_call.assert_not_awaited()

    async def test_low_token_no_subtitle_uses_single_frame_path(self):
        bot = Bot()
        bot.config["VIDEO_ANALYSIS_MODE"] = "pond_low_token"
        bot._get_low_token_subtitle = AsyncMock(return_value="")
        bot._analyze_video_pond_frames = AsyncMock(return_value="画面展示木工抽屉")
        info = {"bvid": BV, "title": "木工", "cid": 1}
        result = await VideoMixin._analyze_video_with_vision_inner(bot, info)
        self.assertIn("木工抽屉", result)
        self.assertEqual(info["analysis_basis"], "video_media_low_token")
        bot._analyze_video_pond_frames.assert_awaited_once()

    async def test_owner_recent_group_is_recorded_and_used_for_delivery(self):
        bot = Bot()
        bot.config.update({
            "OWNER_QQ_UMO": "FriendMessage:10001",
            "OWNER_NAME": "主人",
            "OWNER_QQ_RECENT_GROUP_MAX_AGE_HOURS": 168,
        })
        event = types.SimpleNamespace(
            unified_msg_origin="demo_onebot:GroupMessage:20001",
            get_sender_id=lambda: "10001",
        )
        self.assertTrue(bot._remember_owner_recent_group(event))
        bot.context = types.SimpleNamespace(send_message=AsyncMock())
        class FakeMessageChain:
            def __init__(self):
                self.chain = []
            def at(self, name, qq):
                self.chain.append(types.SimpleNamespace(name=name, qq=str(qq)))
                return self
            def message(self, text):
                self.chain.append(types.SimpleNamespace(text=text))
                return self
        event_module = types.ModuleType("astrbot.api.event")
        event_module.MessageChain = FakeMessageChain
        with patch.dict(sys.modules, {"astrbot.api.event": event_module}):
            self.assertTrue(
                await bot._send_owner_recommend_qq_group("看完挺有意思", BV)
            )
        args = bot.context.send_message.await_args.args
        self.assertEqual(args[0], event.unified_msg_origin)
        self.assertEqual(args[1].chain[0].qq, "10001")
        self.assertIn(BV, args[1].chain[-1].text)

    async def test_stale_owner_group_is_not_used(self):
        bot = Bot()
        bot.config.update({
            "OWNER_QQ_UMO": "FriendMessage:10001",
            "OWNER_QQ_RECENT_GROUP_MAX_AGE_HOURS": 1,
        })
        bot.files[config.OWNER_QQ_RECENT_GROUP_FILE] = {
            "umo": "demo_onebot:GroupMessage:20001",
            "group_id": "20001",
            "owner_qq": "10001",
            "updated_at": time.time() - 7200,
        }
        bot.context = types.SimpleNamespace(send_message=AsyncMock())
        self.assertFalse(
            await bot._send_owner_recommend_qq_group("过期消息", BV)
        )
        bot.context.send_message.assert_not_awaited()

    async def test_low_token_subtitle_validator_rejects_mismatched_topic(self):
        bot = Bot()
        bot._summary_llm_call = AsyncMock(
            return_value='{"relevant":false,"reason":"主题完全不符"}'
        )
        bot._llm_call = AsyncMock()
        info = {"bvid": BV, "title": "帝王蟹煎饼果子", "desc": ""}
        result = await bot._validate_low_token_subtitle(
            info, "沃尔玛零食区、烘焙区和宠物区商品测评"
        )
        self.assertEqual(result, "")
        bot._summary_llm_call.assert_awaited_once()
        prompt = bot._summary_llm_call.call_args.args[0]
        self.assertLess(len(prompt), 1400)

    async def test_low_token_subtitle_validator_keeps_matching_topic(self):
        bot = Bot()
        bot._summary_llm_call = AsyncMock(
            return_value='{"relevant":true,"reason":"主题一致"}'
        )
        bot._llm_call = AsyncMock()
        subtitle = "老板把帝王蟹拆开，加进煎饼果子里。"
        result = await bot._validate_low_token_subtitle(
            {"bvid": BV, "title": "帝王蟹煎饼果子"}, subtitle
        )
        self.assertEqual(result, subtitle)

    async def test_private_then_qq_then_comment_share_one_analysis(self):
        bot = Bot()
        first = await bot._watch_video_and_save_memory(BV, "private_message")
        qq = await bot._summarize_shared_video({"bvid": BV})
        comment, cached = await bot._get_video_context(123, 1)
        self.assertFalse(first["from_cache"])
        self.assertIn("从观看记忆读取", qq)
        self.assertIn("从观看记忆读取", comment)
        self.assertIn("8.3", comment)
        self.assertEqual(cached["score"], 8.3)
        self.assertEqual(bot.analysis_calls, 1)
        bot._evaluate_video.assert_awaited_once()

    async def test_expired_detail_keeps_score_and_feelings_on_every_entry(self):
        bot = Bot()
        await bot._watch_video_and_save_memory(BV)
        original = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d %H:%M")
        bot.files[config.VIDEO_MEMORY_FILE][BV]["time"] = original
        again = await bot._watch_video_and_save_memory(BV)
        qq = await bot._summarize_shared_video({"bvid": BV})
        comment, cached = await bot._get_video_context(123, 1)
        self.assertTrue(again["from_cache"])
        self.assertEqual(bot.analysis_calls, 1)
        self.assertIn("榫头", qq)
        self.assertIn("8.3", comment)
        self.assertEqual(cached["memory_stage"], "faded")
        self.assertEqual(cached["time"], original)

    async def test_ledger_only_never_rewatches(self):
        bot = Bot()
        await bot._watch_video_and_save_memory(BV)
        bot.files[config.VIDEO_MEMORY_FILE] = {}
        result = await bot._watch_video_and_save_memory(BV)
        qq = await bot._summarize_shared_video({"bvid": BV})
        self.assertTrue(result["seen_only"])
        self.assertIn("淡忘", qq)
        self.assertEqual(bot.analysis_calls, 1)

    async def test_explicit_rewatch_bypasses_all_content_caches(self):
        bot = Bot()
        await bot._watch_video_and_save_memory(BV)
        second = await bot._watch_video_and_save_memory(BV, force_rewatch=True)
        self.assertFalse(second["from_cache"])
        self.assertEqual(bot.analysis_calls, 2)
        self.assertEqual(bot._evaluate_video.await_count, 2)

    async def test_old_qq_metadata_preview_does_not_claim_watched(self):
        bot = Bot()
        bot.files[config.VIDEO_MEMORY_FILE] = {BV: {
            "source": "group_share", "analysis": "根据标题猜测", "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }}
        self.assertFalse(await bot._has_seen_video(BV))
        result = await bot._watch_video_and_save_memory(BV)
        self.assertFalse(result["from_cache"])
        self.assertEqual(bot.analysis_calls, 1)

    async def test_legacy_external_summary_is_read_without_redownload(self):
        bot = Bot()
        bot.files[config.EXTERNAL_MEMORY_FILE] = {BV: {
            "description": "旧分析榫卯结构", "score": 7.8, "review": "讲解很细",
            "watched_at": (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d %H:%M"),
        }}
        result = await bot._watch_video_and_save_memory(BV)
        self.assertTrue(result["from_cache"])
        self.assertIn("7.8", result["message"])
        self.assertEqual(bot.analysis_calls, 0)

    async def test_concurrent_video_entries_do_not_duplicate_analysis(self):
        bot = Bot()
        infos = [{"bvid": BV, "title": "木工"} for _ in range(3)]
        results = await asyncio.gather(*(bot._analyze_video_with_vision(info) for info in infos))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(bot.analysis_calls, 1)

    async def test_failed_rating_does_not_invent_five_out_of_ten(self):
        bot = Bot()
        bot._evaluate_video = AsyncMock(return_value=None)
        result = await bot._watch_video_and_save_memory(BV)
        self.assertIn("未评分", result["message"])
        self.assertIsNone(bot.files[config.VIDEO_MEMORY_FILE][BV]["score"])
        self.assertTrue((await bot._watch_video_and_save_memory(BV))["from_cache"])
        self.assertEqual(bot.analysis_calls, 1)

    async def test_different_videos_do_not_overwrite_each_others_cache(self):
        bot = Bot()
        infos = [{"bvid": BV, "title": "木工"}, {"bvid": "BV2xx411c7mD", "title": "实验"}]
        await asyncio.gather(*(bot._analyze_video_with_vision(info) for info in infos))
        self.assertEqual(set(bot.files[config.VIDEO_MEMORY_FILE]), {BV, "BV2xx411c7mD"})

    async def test_hot_comments_alone_do_not_skip_video_media(self):
        bot = Bot()
        joint = {"has_signal": True, "subtitle": "", "extra": "热门评论：精彩", "joint_hint": ""}
        bot._build_joint_context = AsyncMock(return_value=joint)
        bot._analyze_video_media = AsyncMock(return_value="画面里先切木板，再安装榫头")
        bot._summary_llm_call = AsyncMock(return_value="具体摘要：制作抽屉并演示榫卯连接")
        bot._llm_call = AsyncMock()
        info = {"bvid": BV, "title": "木工"}
        result = await VideoMixin._analyze_video_with_vision_inner(bot, info)
        bot._analyze_video_media.assert_awaited_once()
        bot._summary_llm_call.assert_awaited_once()
        bot._llm_call.assert_not_awaited()
        self.assertIn("具体摘要", result)
        self.assertEqual(info["analysis_basis"], "video_media")

    async def test_metadata_only_is_labeled_and_not_saved_as_watched(self):
        bot = Bot()

        async def metadata(info):
            info["analysis_basis"] = "metadata"
            return "仅有标题和简介，尚未确认视频内容"

        bot._analyze_video_with_vision_inner = metadata
        bot._evaluate_video = AsyncMock(return_value=None)
        await bot._watch_video_and_save_memory(BV)
        self.assertFalse(bot.files[config.VIDEO_MEMORY_FILE][BV]["content_analyzed"])
        self.assertFalse(await bot._has_seen_video(BV))


class PostingAndRatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_moderate_score_is_material_but_private_chat_is_not(self):
        bot = Bot()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        bot.files[config.WATCH_LOG_FILE] = [{"time": now, "title": "普通木工", "score": 6.2, "review": "榫头那里没讲清楚"}]
        bot._memory = [
            {"time": now, "user_id": "self", "memory_type": "video", "source": "bilibili", "text": "公开视频观点"},
            {"time": now, "user_id": "42", "memory_type": "chat", "text": "主人秘密"},
            {"time": now, "user_id": "self", "memory_type": "video", "source": "private_message", "text": "私信秘密"},
        ]
        material = "\n".join(bot._dynamic_grounding_context())
        self.assertIn("榫头", material)
        self.assertIn("公开视频观点", material)
        self.assertNotIn("秘密", material)

    async def test_post_interval_survives_restart(self):
        bot = Bot()
        bot.config["DYNAMIC_MIN_INTERVAL_DAYS"] = 3
        now = datetime.now()
        history = [{"time": (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")}]
        bot.files[config.DYNAMIC_LOG_FILE] = history
        self.assertFalse(bot._dynamic_post_due())
        history[0]["time"] = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
        self.assertTrue(bot._dynamic_post_due(history))

    async def test_recommendation_gap_and_limit(self):
        bot = Bot()
        valid = {"recommend_owner": True, "recommend_reason": "榫头设计有意思"}
        self.assertTrue(bot._can_recommend_owner(valid, 7.2, 0))
        self.assertFalse(bot._can_recommend_owner(valid, 7.2, 3))
        bot._owner_last_recommended_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.assertFalse(bot._can_recommend_owner(valid, 8.9, 1))
        bot._owner_last_recommended_at = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
        self.assertTrue(bot._can_recommend_owner(valid, 7.2, 1))

    async def test_successful_share_migration_survives_empty_watch_log(self):
        bot = Bot()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        history = [{"bvid": BV, "time": now, "actions": ["✉️私信推荐给主人"]}]
        _, first_count = bot._load_owner_recommendation_state(history)
        ledger, second_count = bot._load_owner_recommendation_state([])
        self.assertIn(BV, ledger)
        self.assertIn(BV, bot._owner_recommended_bvids)
        self.assertEqual((first_count, second_count), (1, 1))

    async def test_evaluation_skips_missing_content_evidence(self):
        bot = Bot()
        bot._llm_call = AsyncMock()
        for basis in ("metadata", "seen_only"):
            self.assertIsNone(await ProactiveMixin._evaluate_video(bot, {"analysis_basis": basis}, "只有简介"))
        bot._llm_call.assert_not_awaited()

    async def test_rating_requires_details_without_forcing_a_distribution(self):
        bot = Bot()
        bot._get_system_prompt = AsyncMock(return_value="人设")
        bot._get_today_mood = lambda: ("平静", "正常")
        bot._owner_recommendation_context = AsyncMock(return_value="")
        bot._lifecycle_preference_items = lambda: []
        bot._llm_call = AsyncMock(return_value=None)
        await ProactiveMixin._evaluate_video(bot, {"title": "木工"}, "具体内容")
        prompt = bot._llm_call.call_args.args[0]
        self.assertNotIn("大部分视频应该落在5-7分", prompt)
        self.assertIn("相邻档位", prompt)
        self.assertIn("具体亮点或问题", prompt)


class ModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_outage_does_not_block_separate_generation_provider(self):
        bot = LLMMixin()
        bot.config = {"SUMMARY_PROVIDER_ID": "summary", "GENERATION_PROVIDER_ID": "write",
                      "LLM_CIRCUIT_FAILURE_THRESHOLD": 2, "LLM_CIRCUIT_COOLDOWN_SECONDS": 120}

        async def generate(**options):
            if options["chat_provider_id"] == "summary":
                raise TimeoutError("fake outage")
            return types.SimpleNamespace(completion_text="generation still works")

        bot.context = types.SimpleNamespace(llm_generate=AsyncMock(side_effect=generate))
        await bot._summary_llm_call("facts")
        await bot._summary_llm_call("facts")
        self.assertIsNone(await bot._summary_llm_call("facts"))
        self.assertEqual(await bot._generation_llm_call("write"), "generation still works")
        self.assertEqual(bot.context.llm_generate.await_count, 3)

    async def test_summary_and_generation_use_distinct_providers(self):
        bot = LLMMixin()
        bot.config = {"SUMMARY_PROVIDER_ID": "summary-id", "GENERATION_PROVIDER_ID": "write-id"}
        bot._llm_call = AsyncMock(return_value="result")
        await bot._summary_llm_call("facts")
        await bot._generation_llm_call("write", system_prompt="persona")
        calls = bot._llm_call.call_args_list
        self.assertEqual(calls[0].kwargs["provider_id"], "summary-id")
        self.assertEqual(calls[1].kwargs["provider_id"], "write-id")
        self.assertEqual(calls[1].kwargs["system_prompt"], "persona")

    async def test_blank_models_inherit_without_additional_requests(self):
        bot = LLMMixin()
        bot.config = {}
        bot._llm_call = AsyncMock(return_value="old provider")
        self.assertEqual(await bot._summary_llm_call("facts"), "old provider")
        self.assertEqual(bot._llm_call.await_count, 1)
        self.assertNotIn("provider_id", bot._llm_call.call_args.kwargs)

    async def test_independent_api_uses_correct_key_model_and_closes_client(self):
        bot = LLMMixin()
        bot.config = {"LLM_CIRCUIT_FAILURE_THRESHOLD": 0,
                      "SUMMARY_API_KEY": "fake-summary-key", "SUMMARY_MODEL": "summary-model",
                      "GENERATION_API_KEY": "fake-write-key", "GENERATION_MODEL": "write-model"}
        bot._normalize_openai_base_url = lambda value: value
        observed = []

        class Client:
            def __init__(self, **options):
                self.row = {**options}
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))
                observed.append(self.row)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                self.row["closed"] = True

            async def create(self, **options):
                self.row.update(options)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="done"))])

        with patch.dict(sys.modules, {"openai": types.SimpleNamespace(AsyncOpenAI=Client)}):
            await bot._summary_llm_call("facts")
            await bot._generation_llm_call("write", system_prompt="persona")
        self.assertEqual([item["model"] for item in observed], ["summary-model", "write-model"])
        self.assertEqual([item["api_key"] for item in observed], ["fake-summary-key", "fake-write-key"])
        self.assertTrue(all(item["closed"] and item["max_retries"] == 0 for item in observed))

    async def test_partial_independent_api_configuration_does_not_silently_switch(self):
        bot = LLMMixin()
        bot.config = {"SUMMARY_API_KEY": "fake-key"}
        bot._llm_call = AsyncMock()
        self.assertIsNone(await bot._summary_llm_call("facts"))
        bot._llm_call.assert_not_awaited()


class PreferenceChangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_fatigue_does_not_inherit_old_stable_like_stage(self):
        db = Database(":memory:")
        await db.open()
        try:
            store = PreferenceStore(db)
            at = datetime.now().timestamp()
            for age in (35, 28, 21):
                await store.record_video_signals(source_ref=f"like-{age}", occurred_at=at - age * 86400,
                    signals=[{"type": "theme", "value": "木工", "polarity": "like", "strength": 1, "evidence": "当时喜欢"}])
            first = await store.refresh(at=at - 20 * 86400)
            self.assertEqual(first["current"][0]["stage"], "stable")
            for age in (1, 0):
                await store.record_video_signals(source_ref=f"fatigue-{age}", occurred_at=at - age * 86400,
                    signals=[{"type": "theme", "value": "木工", "polarity": "fatigue", "strength": 1, "evidence": "现在疲劳"}])
            current = (await store.refresh(at=at))["current"][0]
            self.assertEqual(current["polarity"], "fatigue")
            self.assertEqual(current["stage"], "recent")
        finally:
            await db.close()

    async def test_fresh_dislike_can_override_many_old_likes(self):
        db = Database(":memory:")
        await db.open()
        try:
            store = PreferenceStore(db)
            at = datetime.now().timestamp()
            for index in range(8):
                await store.record_video_signals(source_ref=f"old-{index}", occurred_at=at - (60 + index) * 86400,
                    signals=[{"type": "theme", "value": "木工", "polarity": "like", "strength": 1, "evidence": "以前喜欢"}])
            for index in range(2):
                await store.record_video_signals(source_ref=f"new-{index}", occurred_at=at - index * 86400,
                    signals=[{"type": "theme", "value": "木工", "polarity": "fatigue", "strength": 1, "evidence": "近期重复疲劳"}])
            result = await store.refresh(at=at)
            self.assertEqual(result["current"][0]["polarity"], "fatigue")
        finally:
            await db.close()


class ShareRedirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_offsite_redirect_is_blocked_before_request(self):
        bot = Bot()
        urls = []

        class Response:
            url = "https://b23.tv/test"
            status = 302
            headers = {"Location": "http://127.0.0.1/private"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class Session(Response):
            def get(self, url, **options):
                self.assertion = options
                urls.append(url)
                if options["allow_redirects"]:
                    raise AssertionError("automatic redirects expose the target before validation")
                if "Cookie" in options["headers"]:
                    raise AssertionError("shortlinks must not carry credentials")
                return Response()

        with patch("core.share.plugin_http_session", return_value=Session()):
            result = await bot._resolve_share_url("https://b23.tv/test")
        self.assertEqual(urls, ["https://b23.tv/test"])
        self.assertEqual(result, "https://b23.tv/test")

    def test_non_http_and_credential_bearing_links_are_rejected(self):
        for url in ("file://bilibili.com/a", "https://user:pass@b23.tv/x", "https://b23.tv:123/x"):
            self.assertFalse(ShareMixin._is_allowed_share_host(url))


if __name__ == "__main__":
    unittest.main()
