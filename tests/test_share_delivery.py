"""A parsed share must send its card first, then standalone media messages."""
import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


from share_parts import ShareParts
from itself import itself


class Event:
    message_str = "https://www.bilibili.com/video/BV1udYT69EUv"

    def __init__(self, scene):
        self.unified_msg_origin = f"test:{scene}:123"

    def plain_result(self, text):
        return [types.SimpleNamespace(type="plain", text=text)]

    def get_self_id(self):
        return "10000"

    def chain_result(self, chain):
        if any(component.type == "video" for component in chain) and len(chain) != 1:
            raise AssertionError('message element "video" must be the only segment in a message')
        return chain


class ShareProbe(ShareParts):
    models = itself

    def __init__(self, paths, component_type="video"):
        self.config = {"ENABLE_BILI_SHARE_PARSE": True, "BILI_SHARE_PARSE_AUTO_WATCH": False}
        self.share_links._extract_bili_share_target = AsyncMock(return_value={"bvid": "BV1udYT69EUv"})
        self.share._get_video_info_by_share_target = AsyncMock(return_value={
            "bvid": "BV1udYT69EUv", "title": "测试视频", "duration": 77,
        })
        self.memory_writer._save_self_memory_record = AsyncMock()
        self.processes._find_command = lambda _name: "fake-yt-dlp"
        self.video_files._download_video = AsyncMock(return_value="/fake/original.mp4")
        delivery = self.share_delivery
        delivery._split_share_video_for_chat = AsyncMock(return_value=(paths, False))
        delivery._prepare_share_send_files = lambda raw, _bvid: list(raw)
        delivery._cleanup_share_video_files_later = AsyncMock()
        self.qq_chat._share_video_component = lambda path: (
            types.SimpleNamespace(type=component_type, path=path) if component_type else None
        )


class ShareDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        class Node:
            def __init__(self, content, uin, name):
                self.type, self.content, self.uin, self.name = "node", content, uin, name

        class Nodes:
            def __init__(self, nodes):
                self.type, self.nodes = "nodes", nodes

        module = types.ModuleType("astrbot.api.message_components")
        module.Node, module.Nodes = Node, Nodes
        self.patch = patch.dict(sys.modules, {"astrbot.api.message_components": module})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def collect(self, probe, scene="FriendMessage", mode="auto"):
        event = Event(scene)
        messages = [message async for message in probe.share._handle_bili_share(
            event, text_override=event.message_str, trigger_mode=mode,
        )]
        tasks = list(probe.share_delivery._share_cleanup_tasks)
        if tasks:
            await asyncio.gather(*tasks)
        return messages

    async def test_card_then_video_for_all_share_entry_points(self):
        for scene in ("FriendMessage", "GroupMessage"):
            for mode in ("auto", "manual", "llm"):
                with self.subTest(scene=scene, mode=mode):
                    probe = ShareProbe(["/fake/video.mp4"])
                    messages = await self.collect(probe, scene, mode)
                    self.assertEqual([item[0].type for item in messages], ["plain", "video"])
                    self.assertIn("请稍等...视频一会发出", messages[0][0].text)
                    self.assertEqual(len(messages[1]), 1)
                    self.assertEqual(messages[1][0].path, "/fake/video.mp4")
                    self.assertNotIn("回放切片", messages[0][0].text)
                    probe.share_delivery._cleanup_share_video_files_later.assert_awaited_once_with(
                        ["/fake/video.mp4", "/fake/original.mp4"], delay=1200,
                    )

    async def test_multiple_segments_form_one_bundle_for_group_and_private(self):
        paths = ["/fake/part1.mp4", "/fake/part2.mp4"]
        for scene in ("FriendMessage", "GroupMessage"):
            for mode in ("auto", "manual", "llm"):
                with self.subTest(scene=scene, mode=mode):
                    messages = await self.collect(ShareProbe(paths), scene, mode)
                    self.assertEqual([item[0].type for item in messages], ["plain", "nodes"])
                    self.assertTrue(all(len(item) == 1 for item in messages))
                    nodes = messages[1][0].nodes
                    self.assertEqual([node.content[0].path for node in nodes], paths)
                    self.assertTrue(all(len(node.content) == 1 and node.content[0].type == "video" for node in nodes))
                    self.assertTrue(all(node.uin == "10000" for node in nodes))

    async def test_unavailable_forward_component_falls_back_to_pure_video(self):
        probe = ShareProbe(["/fake/part1.mp4", "/fake/part2.mp4"])
        probe.qq_chat._share_forward_component = lambda _event, _components: None
        messages = await self.collect(probe)
        self.assertEqual([item[0].type for item in messages], ["plain", "plain", "video", "video"])
        self.assertIn("改为逐段发送", messages[1][0].text)
        self.assertTrue(all(len(item) == 1 for item in messages))

    async def test_file_fallback_is_also_standalone(self):
        messages = await self.collect(ShareProbe(["/fake/video.mp4"], "file"))
        self.assertEqual([item[0].type for item in messages], ["plain", "file"])
        self.assertEqual(len(messages[1]), 1)

    async def test_missing_component_keeps_link_fallback(self):
        messages = await self.collect(ShareProbe(["/fake/video.mp4"], None))
        self.assertEqual([item[0].type for item in messages], ["plain", "plain"])
        self.assertIn("没有可用的视频/文件组件", messages[1][0].text)
        self.assertIn("https://www.bilibili.com/video/BV1udYT69EUv", messages[1][0].text)

    async def test_disabled_video_delivery_still_sends_only_card(self):
        probe = ShareProbe(["/fake/video.mp4"])
        probe.config["BILI_SHARE_PARSE_SEND_VIDEO"] = False
        messages = await self.collect(probe)
        self.assertEqual(len(messages), 1)
        self.assertNotIn("请稍等", messages[0][0].text)
        probe.video_files._download_video.assert_not_awaited()


class OneBotEvent(Event):
    """A OneBot group event whose bot returns the id of the sent message."""

    def __init__(self):
        super().__init__("GroupMessage")
        self.unified_msg_origin = "aiocqhttp:GroupMessage:777"
        self.message_obj = types.SimpleNamespace(message_id=111)
        self.bot = types.SimpleNamespace(call_action=AsyncMock(return_value={"message_id": 4242}))

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return "777"

    def get_sender_id(self):
        return "10001"


class ReactionProbe(ShareProbe):
    def __init__(self, llm_replies, *, cached=None, seen=False):
        super().__init__(["/fake/video.mp4"])
        self.config["BILI_SHARE_PARSE_AUTO_WATCH"] = True
        self._llm_call = AsyncMock(side_effect=list(llm_replies))
        self.qq_chat._qq_session_provider_id = AsyncMock(return_value="qq-provider")
        self.qq_chat._qq_session_persona_prompt = AsyncMock(return_value="QQ人格")
        self._cache = cached
        self.video_watch._cached_video_memory = lambda _bvid: self._cache
        self.seen_videos.has_seen = AsyncMock(return_value=seen)

        async def watch(bvid, memory_source):
            self._cache = {"summary": "一只猫在弹钢琴"}
            return {"ok": True, "message": "内容详情：一只猫在弹钢琴", "bvid": bvid}

        self.watch_on_request._watch_video_and_save_memory = AsyncMock(side_effect=watch)
        self.qq_chat._send_share_reaction = AsyncMock()
        self.qq_chat._share_video_component = lambda path: types.SimpleNamespace(
            type="video", path=path,
            to_dict=lambda: {"type": "video", "data": {"file": f"file://{path}"}},
        )

    async def run(self, event):
        messages = [m async for m in self.share._handle_bili_share(
            event, text_override=event.message_str, trigger_mode="auto",
        )]
        await asyncio.gather(*self.share_reaction._share_reaction_tasks)
        return messages


class ShareReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_onebot_video_is_sent_directly_and_quoted_after_watching(self):
        probe = ReactionProbe([
            '{"decision":"watch"}',
            '{"decision":"comment","text":"这猫弹得比我好"}',
        ])
        event = OneBotEvent()
        messages = await probe.run(event)
        self.assertEqual([m[0].type for m in messages], ["plain"])
        action, params = event.bot.call_action.await_args.args[0], event.bot.call_action.await_args.kwargs
        self.assertEqual(action, "send_group_msg")
        self.assertEqual(params["group_id"], 777)
        self.assertEqual(params["message"][0]["data"]["file"], "file:///fake/video.mp4")
        probe.watch_on_request._watch_video_and_save_memory.assert_awaited_once_with(
            "BV1udYT69EUv", memory_source="qq_share"
        )
        probe.qq_chat._send_share_reaction.assert_awaited_once_with(
            "aiocqhttp:GroupMessage:777", "这猫弹得比我好", "4242"
        )
        for call in probe._llm_call.await_args_list:
            self.assertEqual(call.kwargs["provider_id"], "qq-provider")
            self.assertEqual(call.kwargs["system_prompt"], "QQ人格")
        # Follow-up chat now knows the bot has watched it.
        self.assertIn("一只猫在弹钢琴", probe.share_session._get_recent_group_share_prompt(event))

    async def test_skip_decision_does_not_watch_and_prompt_says_unwatched(self):
        probe = ReactionProbe(['{"decision":"skip"}'])
        event = OneBotEvent()
        await probe.run(event)
        probe.watch_on_request._watch_video_and_save_memory.assert_not_awaited()
        probe.qq_chat._send_share_reaction.assert_not_awaited()
        prompt = probe.share_session._get_recent_group_share_prompt(event)
        self.assertIn("还没看过", prompt)
        self.assertIn('trust="untrusted"', prompt)

    async def test_invalid_decision_json_means_no_watch(self):
        probe = ReactionProbe(["好啊我去看看"])
        await probe.run(OneBotEvent())
        probe.watch_on_request._watch_video_and_save_memory.assert_not_awaited()

    async def test_remembered_video_skips_decision_and_still_comments(self):
        probe = ReactionProbe(
            ['{"decision":"comment","text":"之前看过，挺好笑"}'],
            cached={"summary": "旧摘要"},
        )
        await probe.run(OneBotEvent())
        self.assertEqual(probe._llm_call.await_count, 1)
        probe.qq_chat._send_share_reaction.assert_awaited_once()

    async def test_faded_seen_trace_neither_rewatches_nor_comments(self):
        probe = ReactionProbe([], seen=True)
        await probe.run(OneBotEvent())
        probe._llm_call.assert_not_awaited()
        probe.watch_on_request._watch_video_and_save_memory.assert_not_awaited()

    async def test_plain_adapter_quotes_the_original_message_without_video(self):
        probe = ReactionProbe([
            '{"decision":"watch"}',
            '{"decision":"comment","text":"好听"}',
        ])
        probe.config["BILI_SHARE_PARSE_SEND_VIDEO"] = False
        event = OneBotEvent()
        await probe.run(event)
        event.bot.call_action.assert_not_awaited()
        self.assertEqual(probe.qq_chat._send_share_reaction.await_args.args[2], "111")

    async def test_failed_onebot_send_is_not_retried(self):
        probe = ReactionProbe(['{"decision":"skip"}'])
        event = OneBotEvent()
        event.bot.call_action = AsyncMock(side_effect=RuntimeError("upload failed"))
        messages = await probe.run(event)
        event.bot.call_action.assert_awaited_once()
        self.assertEqual([m[0].type for m in messages], ["plain", "plain"])
        self.assertIn("没发出去", messages[1][0].text)


class SessionLookupTests(unittest.IsolatedAsyncioTestCase):
    def probe(self, persona_id, *, provider_getter=True):
        probe = ShareParts()
        conversation = types.SimpleNamespace(persona_id=persona_id)
        context = types.SimpleNamespace(
            conversation_manager=types.SimpleNamespace(
                get_curr_conversation_id=AsyncMock(return_value="cid-1"),
                get_conversation=AsyncMock(return_value=conversation),
            ),
            persona_manager=types.SimpleNamespace(
                get_persona=AsyncMock(return_value=types.SimpleNamespace(system_prompt="会话人格")),
                get_default_persona_v3=AsyncMock(return_value={"prompt": "默认人格"}),
            ),
        )
        if provider_getter:
            context.get_current_chat_provider_id = AsyncMock(return_value="qq-model")
        else:
            context.get_using_provider = lambda umo=None: types.SimpleNamespace(
                meta=lambda: types.SimpleNamespace(id=f"using:{umo}")
            )
        probe.context = context
        return probe

    async def test_persona_follows_the_current_conversation(self):
        self.assertEqual(await self.probe("p1").qq_chat._qq_session_persona_prompt("umo"), "会话人格")
        self.assertEqual(await self.probe(None).qq_chat._qq_session_persona_prompt("umo"), "默认人格")
        self.assertEqual(await self.probe("[%None]").qq_chat._qq_session_persona_prompt("umo"), "")

    async def test_provider_is_the_one_this_session_uses(self):
        probe = self.probe(None)
        self.assertEqual(await probe.qq_chat._qq_session_provider_id("umo"), "qq-model")
        probe.context.get_current_chat_provider_id.assert_awaited_once_with(umo="umo")
        self.assertEqual(
            await self.probe(None, provider_getter=False).qq_chat._qq_session_provider_id("umo"),
            "using:umo",
        )


class _DummyCommandFilter:
    pass


class ShareCommandExclusionTests(unittest.IsolatedAsyncioTestCase):
    def _probe(self):
        probe = ShareProbe(["/fake/video.mp4"])
        probe.config["BILI_SHARE_PARSE_SEND_VIDEO"] = False
        probe.share_links._extract_bili_share_target = AsyncMock(return_value={"bvid": "BV12C4y1H7e9"})
        probe.share._get_video_info_by_share_target = AsyncMock(return_value={
            "bvid": "BV12C4y1H7e9", "title": "测试视频", "duration": 77,
        })
        return probe

    def _event(self, message_str, *, raw_message_str=None, chain=None, extras=None, scene="FriendMessage"):
        extras = dict(extras or {})
        evt = Event(scene)
        evt.message_str = message_str
        evt.message_obj = types.SimpleNamespace(
            message_str=raw_message_str,
            message=chain or [],
            raw_message=None,
        )
        evt.stopped = False
        evt.get_extra = lambda key=None, default=None: extras if key is None else extras.get(key, default)
        evt.stop_event = lambda: setattr(evt, "stopped", True)
        return evt

    async def _run_auto_share(self, probe, event, *, stop_on_handled=False):
        handled = False
        results = []
        async for item in probe.share._handle_bili_share(event, trigger_mode="auto"):
            handled = True
            results.append(item)
        if stop_on_handled and handled:
            event.stop_event()
        return results, handled

    def test_registered_commands_cover_all_main_commands(self):
        import ast
        from pathlib import Path
        from core.features.share import REGISTERED_BILI_COMMANDS

        main_path = Path(__file__).resolve().parents[1] / "main.py"
        tree = ast.parse(main_path.read_text(encoding="utf-8-sig"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BiliBiliBot")
        main_cmds = {
            ast.literal_eval(dec.args[0])
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for dec in node.decorator_list
            if isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "command"
        }
        self.assertEqual(main_cmds - set(REGISTERED_BILI_COMMANDS), set())

    async def test_stripped_bili_commands_are_not_intercepted(self):
        # 模拟 WakingCheckStage 剥离前导 "/" 后的事件；私聊下不能产出结果也不能 stop_event。
        for stripped in (
            "bili测试查询 BV12C4y1H7e9 这个视频讲啥",
            "bili主动 https://b23.tv/abc123",
            "bili状态 BV12C4y1H7e9",
            "bili日志 查询 BV12C4y1H7e9",
            "bili解析 BV12C4y1H7e9",
            "biliUMO BV12C4y1H7e9",
            "biliumo BV12C4y1H7e9",
            "bili记忆 BV12C4y1H7e9",
        ):
            with self.subTest(stripped=stripped):
                probe = self._probe()
                event = self._event(
                    stripped,
                    raw_message_str="/" + stripped,
                    chain=[types.SimpleNamespace(type="plain", text="/" + stripped)],
                    extras={"handlers_parsed_params": {"main_cmd": {}}},
                )
                results, handled = await self._run_auto_share(probe, event, stop_on_handled=True)
                self.assertFalse(handled)
                self.assertEqual(results, [])
                self.assertFalse(event.stopped)
                probe.share_links._extract_bili_share_target.assert_not_awaited()
                self.assertEqual(probe.share_session._pending_bili_shares, {})

    async def test_prefixless_bili_commands_without_extras_are_also_excluded(self):
        # 即使没有 message_obj.message_str 和 extras（例如私聊无前缀或单测构造），也不误拦截。
        for text in (
            "bili测试查询 BV12C4y1H7e9",
            "bili主动 https://b23.tv/xyz",
            "bili状态",
            "bili日志 视频 BV12C4y1H7e9",
        ):
            with self.subTest(text=text):
                probe = self._probe()
                event = self._event(text)
                results, handled = await self._run_auto_share(probe, event, stop_on_handled=True)
                self.assertFalse(handled)
                self.assertEqual(results, [])
                self.assertFalse(event.stopped)

    async def test_other_commands_detected_via_raw_prefix_chain_or_extras(self):
        cases = [
            # 其他插件斜杠命令：WakingCheckStage 剥去 "/" 后仍可在 message_obj.message_str 看到
            self._event("othercmd BV12C4y1H7e9", raw_message_str="/othercmd BV12C4y1H7e9"),
            # message_obj.message_str 为空，但消息链里 At/Reply 后的首个文本以 "/" 开头
            self._event(
                "othercmd BV12C4y1H7e9",
                chain=[
                    types.SimpleNamespace(type="at", qq="10000"),
                    types.SimpleNamespace(type="reply", id="1"),
                    types.SimpleNamespace(type="plain", text="/othercmd BV12C4y1H7e9"),
                ],
            ),
            # 自定义 wake_prefix（如 "!"）：通过 activated_handlers 中的 CommandFilter 识别
            self._event(
                "othercmd BV12C4y1H7e9",
                raw_message_str="!othercmd BV12C4y1H7e9",
                extras={
                    "activated_handlers": [
                        types.SimpleNamespace(
                            event_filters=[type("CommandFilter", (), {})()]
                        )
                    ]
                },
            ),
        ]
        for idx, event in enumerate(cases):
            with self.subTest(case=idx):
                probe = self._probe()
                results, handled = await self._run_auto_share(probe, event, stop_on_handled=True)
                self.assertFalse(handled)
                self.assertEqual(results, [])
                self.assertFalse(event.stopped)

    async def test_natural_chat_and_miniapp_shares_still_trigger(self):
        cases = [
            ("看看这个 BV12C4y1H7e9", None, None, None),
            ("https://www.bilibili.com/video/BV12C4y1H7e9", None, None, None),
            ("https://b23.tv/abc123", None, None, None),
            ("bilibili.com/video/BV12C4y1H7e9", None, None, None),
            ("bili记忆力真好 BV12C4y1H7e9", None, None, None),
            ("bilibot帮我看看 BV12C4y1H7e9", None, None, None),
            # 小程序 JSON 卡片没有纯文本，也必须正常解析
            ("", "", [types.SimpleNamespace(type="json", data='{"url":"https://b23.tv/abc"}')], None),
            # 昵称唤醒词不是命令：handlers_parsed_params 为空且无 CommandFilter
            ("看看 BV12C4y1H7e9", "小助手 看看 BV12C4y1H7e9", None, {"handlers_parsed_params": {}}),
        ]
        for msg, raw, chain, extras in cases:
            with self.subTest(msg=msg, raw=raw):
                probe = self._probe()
                event = self._event(msg, raw_message_str=raw, chain=chain, extras=extras)
                results, handled = await self._run_auto_share(probe, event, stop_on_handled=True)
                self.assertTrue(handled)
                self.assertEqual(len(results), 1)
                self.assertIn("B站视频解析", results[0][0].text)
                self.assertTrue(event.stopped)

    async def test_manual_trigger_still_works_when_message_is_bili_parse(self):
        probe = self._probe()
        event = self._event("bili解析 BV12C4y1H7e9", raw_message_str="/bili解析 BV12C4y1H7e9")
        results = [
            item
            async for item in probe.share._handle_bili_share(
                event, text_override="BV12C4y1H7e9", trigger_mode="manual", show_errors=True
            )
        ]
        self.assertEqual(len(results), 1)
        self.assertIn("B站视频解析", results[0][0].text)


if __name__ == "__main__":
    unittest.main()
