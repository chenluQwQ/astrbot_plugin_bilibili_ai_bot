"""故障注入与文件/记忆集成；公开请求全部使用模拟接口。"""
import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import AsyncMock, patch
from PIL import Image
import test_bilibot_image_sync as fixture

gallery = sys.modules.get(fixture.PACKAGE + ".image_gallery") or fixture.load("image_gallery")
workflow = fixture.load("gallery_workflow")
bridge = fixture.bridge


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bot = fixture.Bot(self.root)
        self.bot.config.update(EMBED_MODEL="model-a", EMBED_API_BASE="http://isolated")
        self.bot._get_embedding = AsyncMock(return_value=[1., 0.])
        self.bot._cosine_similarity = lambda a, b: sum(x*y for x,y in zip(a,b))
        self.bot._upload_image_to_bilibili = AsyncMock(return_value={
            "img_src": "https://i0.hdslb.com/bfs/new.jpg", "img_width": 100, "img_height": 100, "img_size": 1})
        self.sent = []

        async def send(oid, text, **kwargs):
            self.sent.append(kwargs)
            kwargs["result_sink"].update(send_status="published", rpid="123")
            return True

        self.bot._send_comment = send
        self.patches = [
            patch.object(gallery, "DATA_DIR", str(self.root)),
            patch.object(gallery, "PROACTIVE_LOG_FILE", str(self.root / "proactive_log.json")),
            patch.object(bridge, "MEDIA_STATE_FILE", str(self.root / "comment_media_state.json")),
            patch.object(bridge, "PROACTIVE_LOG_FILE", str(self.root / "proactive_log.json")),
            patch.object(bridge, "SYNC_STATE_FILE", str(self.root / "sync.json")),
        ]
        for p in self.patches: p.start()
        gallery._LOCK = asyncio.Lock()
        workflow._COLLECT_LOCK = asyncio.Lock()
        output = io.BytesIO()
        Image.new("RGB", (100, 100), "red").save(output, "JPEG")
        self.raw = output.getvalue()
        self.caption = {"suitable": True, "description": "猫咪开心点赞", "emotion": "开心赞同", "liking": 8}
        self.source = {"source_url": "https://i0.hdslb.com/bfs/cat.jpg", "source_bvid": "BV_SOURCE", "source_kind": "comment"}

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    async def collect(self, sources=None, caption=None):
        with patch.object(workflow, "_download", AsyncMock(return_value=self.raw)), \
                patch.object(workflow, "_caption", AsyncMock(return_value=caption or self.caption)) as vision:
            result = await workflow.collect(self.bot, sources or [self.source])
            return result, vision.await_count

    async def publish(self):
        sink = {}
        ok = await workflow.send(self.bot, 1, "猫咪开心点赞", bvid="BV_TEST",
                                 title="测试", at_names={}, result_sink=sink)
        return ok, sink

    async def test_duplicate_url_and_bytes_captioned_once(self):
        result, calls = await self.collect([self.source, dict(self.source, source_url="https://i1.hdslb.com/bfs/cat.jpg")])
        self.assertEqual((result["added"], calls), (1, 1))
        result, calls = await self.collect()
        self.assertEqual((result["added"], calls), (0, 0))
        self.assertEqual(len(gallery.list_assets()), 1)

    async def test_daily_budget_counts_failures_and_survives_reload(self):
        self.bot.config["GALLERY_CAPTION_DAILY_MAX"] = 1
        with patch.object(workflow, "_download", AsyncMock(return_value=self.raw)), \
                patch.object(workflow, "_caption", AsyncMock(side_effect=TimeoutError)) as caption:
            await workflow.collect(self.bot, [self.source])
            await workflow.collect(self.bot, [dict(self.source, source_url="https://i1.hdslb.com/bfs/other.jpg")])
            self.assertEqual(caption.await_count, 1)
        self.assertEqual(workflow.listing()["status"]["caption_attempts_today"], 1)

    async def test_unsuitable_image_keeps_context_but_not_gallery(self):
        result, _ = await self.collect(caption=dict(self.caption, suitable=False))
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["descriptions"], [self.caption["description"]])

    async def test_batch_cannot_exceed_two(self):
        self.bot.config["GALLERY_COLLECT_PER_BROWSE"] = 999
        sources = [dict(self.source, source_url=f"https://i0.hdslb.com/bfs/{i}.jpg") for i in range(5)]
        with patch.object(workflow, "_download", AsyncMock(return_value=self.raw)) as download:
            with patch.object(workflow, "_caption", AsyncMock(return_value=self.caption)):
                await workflow.collect(self.bot, sources)
        self.assertEqual(download.await_count, 2)

    async def test_success_retained_immediately_and_sync_after_log_removed(self):
        await self.collect()
        ok, sink = await self.publish()
        self.assertTrue(ok)
        self.assertEqual(len(sink["images"]), 1)
        self.assertEqual(gallery.status()["history_pending"], 1)
        self.assertEqual(gallery.list_assets()[0]["uses"], 1)
        result = await bridge.sync(self.bot)
        self.assertEqual(result["written"], 1)
        self.assertEqual(self.bot.engine.rows()[0]["metadata"]["bilibot_images"][0]["published_url"],
                         "https://i0.hdslb.com/bfs/new.jpg")

    async def test_upload_failure_falls_back_once_without_image_history(self):
        await self.collect()
        self.bot._upload_image_to_bilibili.side_effect = TimeoutError
        ok, sink = await self.publish()
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(self.sent[0]["pictures"])
        self.assertEqual(sink["images"], [])
        self.assertEqual(gallery.status()["history_events"], 0)

    async def test_unknown_send_never_resends_or_remembers(self):
        await self.collect()
        async def unknown(*args, **kwargs):
            self.sent.append(kwargs)
            kwargs["result_sink"].update(send_status="unknown")
            return False
        self.bot._send_comment = unknown
        sink = {}
        with self.assertRaises(TimeoutError):
            await workflow.send(self.bot, 1, "猫咪开心点赞", bvid="BV_TEST",
                                title="", at_names={}, result_sink=sink)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(sink["images"], [])
        self.assertEqual(gallery.status()["history_events"], 0)
        self.assertEqual(gallery.list_assets()[0]["uses"], 0)

    async def test_eviction_after_published_keeps_history_and_thumbnail_metadata(self):
        await self.collect()
        await self.publish()
        asset = gallery.list_assets()[0]
        self.assertTrue((await gallery.evict_asset(self.bot, asset["asset_id"]))["ok"])
        self.assertFalse((self.root / "liked_images/files" / asset["file_name"]).exists())
        listing = workflow.listing()
        self.assertEqual(listing["items"][0]["thumbnail"], "")
        self.assertEqual(listing["items"][0]["local_status"], "evicted")
        await bridge.sync(self.bot)
        self.assertEqual(self.bot.engine.rows()[0]["metadata"]["bilibot_images"][0]["local_status"], "evicted")

    async def test_no_match_or_disabled_sends_text(self):
        await self.collect()
        self.bot._get_embedding.return_value = [0., -1.]
        ok, sink = await workflow.send(self.bot, 1, "完全不同的意思", bvid="BV_TEST",
                                       title="", at_names={}, result_sink={}), {}
        self.assertTrue(ok)
        self.assertIsNone(self.sent[0]["pictures"])
        self.bot.config["GALLERY_COMMENT_ENABLED"] = False
        await self.publish()
        self.assertEqual(self.bot._upload_image_to_bilibili.await_count, 0)

    async def test_model_namespace_prevents_cross_model_vector_matching(self):
        await self.collect()
        self.bot.config["EMBED_MODEL"] = "model-b"
        self.assertIsNone(await workflow.select(self.bot, "完全不同的内容"))

    async def test_local_thumbnail_bounded_and_private_fields_absent(self):
        await self.collect()
        item = workflow.listing()["items"][0]
        self.assertTrue(item["thumbnail"].startswith("data:image/jpeg;base64,"))
        self.assertLess(len(item["thumbnail"]), 22000)
        self.assertFalse({"embedding", "file_name", "model_key"} & item.keys())

    async def test_caption_rejection_cached_by_content(self):
        with patch.object(workflow, "_download", AsyncMock(return_value=self.raw)), \
                patch.object(workflow, "_caption", AsyncMock(return_value=None)) as caption:
            await workflow.collect(self.bot, [self.source])
            await workflow.collect(self.bot, [dict(self.source, source_url="https://i1.hdslb.com/bfs/cat.jpg")])
            self.assertEqual(caption.await_count, 1)

    async def test_captioned_evicted_image_does_not_resurrect(self):
        await self.collect()
        await gallery.evict_asset(self.bot, gallery.list_assets()[0]["asset_id"])
        result, calls = await self.collect([dict(self.source, source_url="https://i1.hdslb.com/bfs/cat.jpg")])
        self.assertEqual((result["added"], calls, gallery.status()["available"]), (0, 0, 0))

    async def test_caption_missing_provider_does_not_crash(self):
        self.bot._get_image_vision_client = lambda: None
        self.assertIsNone(await workflow._caption(self.bot, self.raw))

    async def test_missing_target_never_uploads(self):
        await self.collect()
        self.bot.umo = ""
        self.assertTrue((await self.publish())[0])
        self.assertEqual(self.bot._upload_image_to_bilibili.await_count, 0)

    async def test_malformed_vector_gracefully_uses_text_similarity(self):
        await self.collect()
        self.bot._get_embedding.return_value = [float("nan")]
        self.assertIsNotNone(await workflow.select(self.bot, "猫咪开心点赞"))

    async def test_capacity_automatically_evicts_least_used_and_liked(self):
        files = self.root / "liked_images/files"
        files.mkdir(parents=True)
        for index in range(150):
            raw = f"isolated-{index}".encode()
            sha = hashlib.sha256(raw).hexdigest()
            name = sha + ".jpg"
            (files / name).write_bytes(raw)
            gallery.register_asset(self.bot, file_name=name, image={
                "asset_id": sha, "description": "反应图", "liking": 0 if index == 0 else 9})
        result, calls = await self.collect()
        self.assertEqual((result["added"], calls), (1, 1))
        self.assertEqual(gallery.status()["available"], 150)
        self.assertEqual(gallery.status()["evicted"], 1)
        self.assertFalse((files / (hashlib.sha256(b"isolated-0").hexdigest() + ".jpg")).exists())

    async def test_provider_call_has_single_attempt_and_small_output_budget(self):
        import types
        response = types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(
            content=json.dumps(self.caption)))], usage=types.SimpleNamespace(prompt_tokens=600, completion_tokens=70))
        create = AsyncMock(return_value=response)
        client = types.SimpleNamespace()
        calls = []
        def options(**kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        client.with_options = options
        self.bot._get_image_vision_client = lambda: client
        self.bot.config["IMAGE_VISION_MODEL"] = "isolated-vision"
        await workflow._caption(self.bot, self.raw)
        self.assertEqual(calls, [{"max_retries": 0, "timeout": 12}])
        self.assertEqual(create.await_args.kwargs["max_tokens"], 200)
        self.assertEqual(create.await_count, 1)

    async def test_unknown_result_is_blocked_on_next_action_attempt(self):
        runtime = fixture.load("runtime")
        await self.collect()
        self.bot._send_comment = AsyncMock(side_effect=lambda *args, **kwargs:
            kwargs["result_sink"].update(send_status="unknown") or False)
        queue = runtime.EventRuntime()
        request = runtime.ActionRequest(key="image-comment:once", kind="proactive_comment")
        try:
            first = await queue.execute(request, lambda: self.publish())
            second = await queue.execute(request, lambda: self.publish())
            self.assertEqual(first.state, "unknown")
            self.assertEqual(second.state, "unknown")
            self.assertTrue(second.duplicate)
            self.assertEqual(self.bot._send_comment.await_count, 1)
        finally:
            await queue.close()

    async def test_unknown_send_persists_across_new_queue_and_browse_path(self):
        await self.collect()
        self.bot._send_comment = AsyncMock(side_effect=lambda *args, **kwargs:
            kwargs["result_sink"].update(send_status="unknown") or False)
        with self.assertRaises(TimeoutError): await self.publish()
        gallery._LOCK = asyncio.Lock()
        with self.assertRaises(TimeoutError): await self.publish()
        self.assertEqual(self.bot._send_comment.await_count, 1)
        self.assertEqual(gallery.status()["send_unknown"], 1)


if __name__ == "__main__":
    unittest.main()
