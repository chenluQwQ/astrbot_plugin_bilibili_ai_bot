"""A parsed share must send its card first, then standalone media messages."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def load_share_module():
    root = Path(__file__).resolve().parents[1] / "core"
    package = types.ModuleType("share_delivery_core")
    package.__path__ = [str(root)]
    config = types.ModuleType("share_delivery_core.config")
    config.TEMP_VIDEO_DIR = "unused"
    config.VIDEO_MEMORY_FILE = "unused"
    api = types.ModuleType("astrbot.api")
    api.logger = MagicMock()
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    spec = importlib.util.spec_from_file_location("share_delivery_core.share", root / "share.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "share_delivery_core": package, "share_delivery_core.config": config,
        "astrbot": astrbot, "astrbot.api": api,
    }):
        spec.loader.exec_module(module)
    return module


ShareMixin = load_share_module().ShareMixin


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


class ShareProbe(ShareMixin):
    def __init__(self, paths, component_type="video"):
        self.config = {"ENABLE_BILI_SHARE_PARSE": True}
        self._extract_bili_share_target = AsyncMock(return_value={"bvid": "BV1udYT69EUv"})
        self._get_video_info_by_share_target = AsyncMock(return_value={
            "bvid": "BV1udYT69EUv", "title": "测试视频", "duration": 77,
        })
        self._save_self_memory_record = AsyncMock()
        self._find_command = lambda _name: "fake-yt-dlp"
        self._download_video = AsyncMock(return_value="/fake/original.mp4")
        self._split_share_video_for_chat = AsyncMock(return_value=(paths, False))
        self._prepare_share_send_files = lambda raw, _bvid: list(raw)
        self._share_video_component = lambda path: (
            types.SimpleNamespace(type=component_type, path=path) if component_type else None
        )
        self._cleanup_share_video_files_later = AsyncMock()


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
        messages = [message async for message in probe._handle_bili_share(
            event, text_override=event.message_str, trigger_mode=mode,
        )]
        tasks = list(getattr(probe, "_share_cleanup_tasks", ()))
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
                    probe._cleanup_share_video_files_later.assert_awaited_once_with(
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
        probe._share_forward_component = lambda _event, _components: None
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
        probe._download_video.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
