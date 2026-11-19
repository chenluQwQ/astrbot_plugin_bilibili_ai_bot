"""视频/图片视觉兼容性与可靠性回归测试：
- AstrBot 原生/Responses/OpenAI 兼容提供商的视频直读分发与拒收判定；
- 客户端配置指纹缓存失效、空 content/列表 content 解析、图片魔数检测与 [:4] 对齐；
- 单段体积超限不拉黑模型、回退抽帧准确标记 analysis_basis、无时长 ffprobe 兜底；
- 评论抽查复用主探测/跳过逻辑、合并 12 帧结构、时长未知不误报时间段错误、JSON 容错修复。
"""
import asyncio
import base64
from contextlib import contextmanager
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from bili_parts import BiliParts
from core.domain.video_watch import VideoWatch, parse_inspection
from core.infra.vision import Vision, classify_vision_rejection, detect_image_mime
from core.settings import Settings
from core.video_diagnostics import exception_detail, get_call_diagnostic, set_call_diagnostic
from core.video_timeline import parse_segment_watch
from video_parts import VideoParts
from vision_fakes import vision_on


@contextmanager
def _with_google_genai_types():
    """在未安装 google-genai 的最小测试环境里提供等价 types 桩对象。"""
    try:
        from google.genai import types as _real_types  # noqa: F401
        yield
        return
    except ImportError:
        pass
    part_cls = type(
        "Part",
        (),
        {
            "from_text": staticmethod(lambda text="": types.SimpleNamespace(text=text)),
            "from_bytes": staticmethod(
                lambda data=b"", mime_type="": types.SimpleNamespace(
                    inline_data=types.SimpleNamespace(data=data, mime_type=mime_type)
                )
            ),
        },
    )
    fake_types = types.SimpleNamespace(
        Part=part_cls,
        UserContent=lambda parts=(): types.SimpleNamespace(parts=list(parts)),
        GenerateContentConfig=lambda **kw: types.SimpleNamespace(**kw),
    )
    fake_genai = types.ModuleType("google.genai")
    fake_genai.types = fake_types
    fake_google = types.ModuleType("google")
    fake_google.genai = fake_genai
    with patch.dict(
        sys.modules,
        {"google": fake_google, "google.genai": fake_genai, "google.genai.types": fake_types},
    ):
        yield


def _seg_json(text="看到猫", at=1, audio="有人讲话"):
    return json.dumps(
        {
            "timeline": [{"at": at, "text": text}],
            "impression": f"{text}的印象",
            "audio": audio,
            "sensitive": False,
        },
        ensure_ascii=False,
    )


class _CodeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _StatusError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class _FakeGeminiProvider:
    def __init__(self, *, model="gemini-test", api_key="k1", response_parts=None, error=None):
        self._model = model
        self._api_key = api_key
        self.timeout = 120
        self.provider_config = {"type": "googlegenai_chat_completion", "api_base": "https://example.invalid", "proxy": ""}
        self.calls = []
        owner = self

        async def generate_content(**kwargs):
            owner.calls.append(kwargs)
            if error is not None:
                raise error
            parts = response_parts or [types.SimpleNamespace(text=_seg_json("原生视频"), thought=False)]
            candidate = types.SimpleNamespace(content=types.SimpleNamespace(parts=parts))
            return types.SimpleNamespace(candidates=[candidate], text="")

        self.client = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=generate_content))

    def meta(self):
        return types.SimpleNamespace(type="googlegenai_chat_completion")

    def get_model(self):
        return self._model

    def get_current_key(self):
        return self._api_key

    async def _prepare_query_config(self, payloads, system_instruction=None, **_kwargs):
        return {"model": payloads.get("model"), "system_instruction": system_instruction}


class _FakeOpenAIChatProvider:
    def __init__(self, *, model="gpt-vision", api_key="k1", content="ok", error=None, extra_body=None):
        self._model = model
        self._api_key = api_key
        self.provider_config = {
            "type": "openai_chat_completion",
            "api_base": "https://example.invalid/v1",
            "custom_extra_body": extra_body or {},
        }
        self.calls = []
        owner = self

        async def create(**kwargs):
            owner.calls.append(kwargs)
            if error is not None:
                raise error
            msg = types.SimpleNamespace(content=content)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

        self.client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
        )

    def meta(self):
        return types.SimpleNamespace(type="openai_chat_completion")

    def get_model(self):
        return self._model

    def get_current_key(self):
        return self._api_key


class _FakeSimpleAdapterProvider:
    def __init__(self, adapter_type):
        self._type = adapter_type
        self.provider_config = {"type": adapter_type}

    def meta(self):
        return types.SimpleNamespace(type=self._type)

    def get_model(self):
        return "m"

    def get_current_key(self):
        return "k"


class _Host(VideoParts, BiliParts):
    vision = property(vision_on)

    def __init__(self, config=None, providers=None):
        self.config = dict(config or {})
        self.settings = Settings(self.config)
        self._embed_client = None
        self._video_vision_client = None
        self._image_vision_client = None
        self.providers = dict(providers or {})
        self.llm_generate_calls = []
        self.llm_generate_result = _seg_json("抽帧内容", audio="听不到")
        owner = self

        class _Ctx:
            def get_provider_by_id(self, pid):
                return owner.providers.get(pid)

            async def llm_generate(self, **kwargs):
                owner.llm_generate_calls.append(kwargs)
                return types.SimpleNamespace(completion_text=owner.llm_generate_result)

        self.context = _Ctx()


class VisionRoutingAndClassificationTests(unittest.IsolatedAsyncioTestCase):
    def test_classify_reads_code_attribute_and_extra_unavailable_hints(self):
        self.assertEqual(classify_vision_rejection(_CodeError(401, "unauthorized")), "unavailable")
        self.assertEqual(classify_vision_rejection(_CodeError(404, "not found")), "unavailable")
        self.assertEqual(classify_vision_rejection(_CodeError(413, "too large")), "too_large")
        self.assertEqual(
            classify_vision_rejection(_CodeError(400, "Invalid image input: Unsupported MIME type")),
            "unsupported",
        )
        self.assertEqual(classify_vision_rejection(RuntimeError("API key not valid")), "unavailable")
        self.assertEqual(classify_vision_rejection(RuntimeError("Requested entity was not found")), "unavailable")
        self.assertEqual(classify_vision_rejection(RuntimeError("Request payload too large")), "too_large")
        self.assertEqual(
            classify_vision_rejection(ValueError("Invalid image file: data URI mime='video/mp4' payload_len=1234")),
            "unsupported",
        )
        self.assertIn("HTTP 400", exception_detail(_CodeError(400, "bad mime"), {}))

    async def test_gemini_native_provider_sends_video_bytes_and_skips_thought_parts(self):
        provider = _FakeGeminiProvider(
            response_parts=[
                types.SimpleNamespace(text="内心思考不能泄露", thought=True),
                types.SimpleNamespace(text=_seg_json("原生听懂了声音"), thought=False),
            ]
        )
        host = _Host(
            {"VIDEO_VISION_PROVIDER_ID": "gem", "VIDEO_VISION_FORMAT": "gemini"},
            providers={"gem": provider},
        )
        parts = host.video_watch._build_video_content(base64.b64encode(b"mp4bytes").decode(), "分析", "gemini")
        with _with_google_genai_types():
            out = await host.vision._astrbot_multimodal_generate("gem", parts, max_tokens=500, system_prompt="人设")
        self.assertEqual(out, _seg_json("原生听懂了声音"))
        self.assertEqual(host.llm_generate_calls, [])
        self.assertEqual(len(provider.calls), 1)
        sent_parts = provider.calls[0]["contents"][0].parts
        self.assertEqual(sent_parts[0].inline_data.mime_type, "video/mp4")
        self.assertEqual(sent_parts[0].inline_data.data, b"mp4bytes")
        self.assertEqual(provider.calls[0]["config"]["system_instruction"], "人设")

    async def test_gemini_native_unsupported_auto_probes_only_once(self):
        provider = _FakeGeminiProvider(error=_CodeError(400, "Unsupported MIME type: video/mp4"))
        host = _Host(
            {"VIDEO_VISION_PROVIDER_ID": "gem", "VIDEO_VISION_FORMAT": "auto"},
            providers={"gem": provider},
        )
        with tempfile.TemporaryDirectory() as folder, _with_google_genai_types():
            seg = Path(folder) / "seg.mp4"
            seg.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)
            out = await host.video_watch._try_video_direct(str(seg), "看视频", "auto", "gem", None, "", {})
        self.assertIsNone(out)
        self.assertEqual(len(provider.calls), 1)
        key = host.video_watch._video_direct_key("gem", "", "auto")
        self.assertIn(key, host.video_watch._video_direct_skip_set())

    async def test_openai_chat_provider_bypasses_context_llm_generate_for_video(self):
        provider = _FakeOpenAIChatProvider(
            content=[{"type": "think", "think": "推理"}, {"type": "text", "text": _seg_json("直发成功")}],
            extra_body={"reasoning_effort": "low"},
        )
        host = _Host(
            {"VIDEO_VISION_PROVIDER_ID": "oai", "VIDEO_VISION_FORMAT": "gemini"},
            providers={"oai": provider},
        )
        parts = host.video_watch._build_video_content(base64.b64encode(b"mp4").decode(), "看视频", "gemini")
        out = await host.vision._astrbot_multimodal_generate("oai", parts, max_tokens=300)
        self.assertEqual(out, _seg_json("直发成功"))
        self.assertEqual(host.llm_generate_calls, [])
        self.assertEqual(provider.calls[0]["extra_body"], {"reasoning_effort": "low"})

    async def test_responses_and_anthropic_adapters_reject_video_without_silent_drop(self):
        for adapter in ("openai_responses", "anthropic_chat_completion"):
            with self.subTest(adapter=adapter):
                host = _Host(
                    {"VIDEO_VISION_PROVIDER_ID": "p", "VIDEO_VISION_FORMAT": "openai"},
                    providers={"p": _FakeSimpleAdapterProvider(adapter)},
                )
                parts = host.video_watch._build_video_content(base64.b64encode(b"mp4").decode(), "看视频", "openai")
                out = await host.vision._astrbot_multimodal_generate("p", parts)
                self.assertIsNone(out)
                self.assertEqual(get_call_diagnostic().get("rejection"), "unsupported")
                self.assertEqual(host.llm_generate_calls, [])

    async def test_vision_call_handles_none_list_content_and_think_tags(self):
        host = _Host({})
        cases = [
            (None, None),
            ("<think>内部思考</think>  真实回答  ", "真实回答"),
            ([{"type": "thinking", "text": "skip"}, {"type": "text", "text": "列表内容"}], "列表内容"),
        ]
        for raw_content, expected in cases:
            client = types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(
                        create=AsyncMock(
                            return_value=types.SimpleNamespace(
                                choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=raw_content))]
                            )
                        )
                    )
                )
            )
            self.assertEqual(await host.vision._vision_call(client, "m", [{"type": "text", "text": "hi"}]), expected)

    async def test_vision_call_timeout_stays_retryable(self):
        host = _Host({})

        async def slow_create(**_kwargs):
            await asyncio.sleep(1)

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=slow_create))
        )
        with patch.object(Vision, "_vision_timeout", return_value=0.01):
            self.assertIsNone(await host.vision._vision_call(client, "m", [{"type": "text", "text": "hi"}]))
        self.assertEqual(get_call_diagnostic().get("rejection"), "other")


class ClientCacheAndImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_clients_rebuild_on_config_change_and_clear_on_empty_key(self):
        closed = []

        class _FakeOpenAI:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def close(self):
                closed.append(self.kwargs["api_key"])

        host = _Host(
            {
                "EMBED_API_KEY": "ek1",
                "EMBED_API_BASE": "https://e1.example",
                "VIDEO_VISION_API_KEY": "vk1",
                "VIDEO_VISION_API_BASE": "https://v1.example",
                "IMAGE_VISION_API_KEY": "ik1",
                "IMAGE_VISION_API_BASE": "https://i1.example",
            }
        )
        with patch("openai.AsyncOpenAI", side_effect=lambda **kw: _FakeOpenAI(**kw)):
            v1 = host.vision._get_video_vision_client()
            self.assertEqual(v1.kwargs["timeout"], 90)
            self.assertEqual(v1.kwargs["max_retries"], 0)
            self.assertIs(host.vision._get_video_vision_client(), v1)
            host.config["VIDEO_VISION_API_KEY"] = "vk2"
            v2 = host.vision._get_video_vision_client()
            self.assertIsNot(v1, v2)
            host.config["VIDEO_VISION_API_KEY"] = ""
            self.assertIsNone(host.vision._get_video_vision_client())

            e1 = host.vision._get_embed_client()
            host.config["PROXY_URL"] = "http://127.0.0.1:7890"
            e2 = host.vision._get_embed_client()
            self.assertIsNot(e1, e2)

            i1 = host.vision._get_image_vision_client()
            host.config["IMAGE_VISION_API_BASE"] = "https://i2.example"
            i2 = host.vision._get_image_vision_client()
            self.assertIsNot(i1, i2)
            await asyncio.sleep(0)
        self.assertEqual(set(closed), {"vk1", "vk2", "ek1", "ik1"})

    async def test_recognize_images_detects_magic_mime_aligns_to_4_and_merges_diagnostics(self):
        self.assertEqual(detect_image_mime(b"\x89PNG\r\n\x1a\nxxxx"), "image/png")
        self.assertEqual(detect_image_mime(b"RIFF\x00\x00\x00\x00WEBPxxxx"), "image/webp")
        self.assertEqual(detect_image_mime(b"GIF89axxxx"), "image/gif")
        self.assertEqual(detect_image_mime(b"\xff\xd8\xffxxxx"), "image/jpeg")

        payloads = {
            "https://1": base64.b64encode(b"\x89PNG\r\n\x1a\npngdata").decode(),
            "https://2": base64.b64encode(b"RIFF1234WEBPdata").decode(),
            "https://3": base64.b64encode(b"GIF89agifdata").decode(),
            "https://4": base64.b64encode(b"\xff\xd8\xffjpgdata").decode(),
            "https://5": base64.b64encode(b"\xff\xd8\xffignored").decode(),
        }
        host = _Host({"IMAGE_VISION_PROVIDER_ID": "p", "IMAGE_VISION_API_KEY": "k", "IMAGE_VISION_MODEL": "m"})
        seen_contents = []

        async def fail_provider(_self, pid, content, **_kw):
            seen_contents.append(content)
            set_call_diagnostic(reason="provider 401", rejection="unavailable")
            return None

        async def fail_client(_self, client, model, content, **_kw):
            set_call_diagnostic(reason="client 502", rejection="other")
            return None

        with (
            patch.object(Vision, "_fetch_image_base64", side_effect=lambda u: payloads[u]),
            patch.object(Vision, "_astrbot_multimodal_generate", new=fail_provider),
            patch.object(Vision, "_vision_call", new=fail_client),
            patch.object(Vision, "_get_image_vision_client", return_value=object()),
        ):
            result = await host.vision._recognize_images(["https://1", "https://2", "https://3", "https://4", "https://5"])
        self.assertEqual(result, "")
        urls = [part["image_url"]["url"].split(";")[0] for part in seen_contents[0] if part["type"] == "image_url"]
        self.assertEqual(urls, ["data:image/png", "data:image/webp", "data:image/gif", "data:image/jpeg"])
        diag = get_call_diagnostic()
        self.assertIn("provider 401", diag["reason"])
        self.assertIn("client 502", diag["reason"])


class VideoWatchReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_video_direct_key_changes_when_api_key_or_proxy_changes(self):
        host = _Host({"VIDEO_VISION_API_KEY": "k1", "VIDEO_VISION_API_BASE": "https://v.example"})
        k1 = host.video_watch._video_direct_key("", "m", "auto")
        host.config["VIDEO_VISION_API_KEY"] = "k2"
        k2 = host.video_watch._video_direct_key("", "m", "auto")
        host.config["PROXY_URL"] = "http://127.0.0.1:7890"
        k3 = host.video_watch._video_direct_key("", "m", "auto")
        self.assertEqual(len({k1, k2, k3}), 3)

    async def test_too_large_skips_only_current_segment_without_blacklisting(self):
        host = _Host({"VIDEO_VISION_PROVIDER_ID": "p", "VIDEO_VISION_FORMAT": "openai"})

        async def reject_large(*_args, **_kwargs):
            set_call_diagnostic(reason="HTTP 413 too large", rejection="too_large")
            return None

        host._astrbot_multimodal_generate = AsyncMock(side_effect=reject_large)
        with tempfile.TemporaryDirectory() as folder:
            seg = Path(folder) / "seg.mp4"
            seg.write_bytes(b"\x00" * 100)
            self.assertIsNone(await host.video_watch._try_video_direct(str(seg), "看", "openai", "p", None, "", {}))
            self.assertEqual(host.video_watch._video_direct_skip_set(), set())

            # 预检超出单段字节上限时直接跳过且不调用模型也不拉黑
            host.video_watch._VIDEO_DIRECT_MAX_BYTES = 10
            try:
                host._astrbot_multimodal_generate.reset_mock()
                self.assertIsNone(await host.video_watch._try_video_direct(str(seg), "看", "openai", "p", None, "", {}))
                host._astrbot_multimodal_generate.assert_not_awaited()
                self.assertEqual(host.video_watch._video_direct_skip_set(), set())
            finally:
                host.video_watch._VIDEO_DIRECT_MAX_BYTES = VideoWatch._VIDEO_DIRECT_MAX_BYTES

    async def test_zero_duration_uses_ffprobe_and_fallback_sets_video_frames_basis(self):
        host = _Host({"VIDEO_VISION_PROVIDER_ID": "p", "VIDEO_VISION_FORMAT": "openai"})
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "BV1.mp4"
            video.write_bytes(b"mp4")
            frame = Path(folder) / "frame_0.jpg"
            frame.write_bytes(b"jpg")
            host.video_files._download_video = AsyncMock(return_value=str(video))
            host.video_files._get_video_duration = AsyncMock(return_value=42.0)
            host.video_files._slice_video_segments = AsyncMock(return_value=[str(video)])
            host.video_files._extract_video_frames = AsyncMock(return_value=[str(frame)])
            host.video_files._cleanup_video_artifacts = lambda *_a, **_kw: None
            host.bili_videos._get_video_subtitle_lines = AsyncMock(return_value=[])

            async def answer(_pid, content, **_kw):
                if any(p.get("type") == "video_url" for p in content):
                    set_call_diagnostic(reason="unsupported", rejection="unsupported")
                    return None
                return _seg_json("抽帧看到的", at=5, audio="听不到")

            host._astrbot_multimodal_generate = AsyncMock(side_effect=answer)
            info = {"bvid": "BV1234567890", "title": "无时长", "desc": "", "tname": "科技", "duration": 0}
            result = await host.video_watch._analyze_video_with_vision_inner(info)
            self.assertIn("抽帧看到的", result)
            self.assertEqual(info["analysis_basis"], "video_frames")
            # 截帧提示词里的时间戳按 ffprobe 的 42 秒算（约第 7 秒），而不是 300 秒（第 50 秒）
            frame_prompt = host._astrbot_multimodal_generate.call_args_list[-1].args[1][-1]["text"]
            self.assertIn("第 7 秒", frame_prompt)
            self.assertNotIn("第 50 秒", frame_prompt)

    async def test_extract_video_frames_guards_zero_ffprobe_duration(self):
        host = _Host({})
        timestamps = []
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "v.mp4"
            video.write_bytes(b"v")

            async def run(*args, **_kw):
                if args[0] == "ffprobe":
                    return 0, "0\n", ""
                timestamps.append(args[3])
                Path(args[-1]).write_bytes(b"jpg")
                return 0, "", ""

            host.processes._run_process = run
            frames = await host.video_files._extract_video_frames(str(video), count=2)
        self.assertEqual(len(frames), 2)
        self.assertEqual(timestamps, ["10.00", "20.00"])

    def test_parse_segment_and_inspection_repair_dirty_json(self):
        dirty_seg = '好的，结果如下：\n```json\n{"timeline": [{"at": 2, "text": "他说“看到猫”",}], "impression": "很可爱", "audio": "有声音", "sensitive": false,}\n```'
        parsed_seg = parse_segment_watch(dirty_seg, length=30)
        self.assertEqual(parsed_seg["timeline"], [(2.0, "他说'看到猫'")])

        dirty_ins = '核查完毕：\n{"finding": "found", "evidence": "出现了猫", "moments": [{"start_seconds": 1, "end_seconds": 3},],}'
        parsed_ins = parse_inspection(dirty_ins, 30)
        self.assertEqual(parsed_ins["finding"], "found")

    async def test_inspect_zero_duration_returns_unavailable_instead_of_value_error(self):
        host = _Host({"VIDEO_VISION_PROVIDER_ID": "p"})
        with tempfile.TemporaryDirectory() as folder, patch("core.domain.video_watch.TEMP_VIDEO_DIR", folder):
            video = Path(folder) / "BV1234567890.mp4"
            video.write_bytes(b"v")
            host.video_files._download_video = AsyncMock(return_value=str(video))
            host.video_files._get_video_duration = AsyncMock(return_value=0.0)
            res = await host.video_watch._inspect_comment_video(
                {"bvid": "BV1234567890", "duration": 0}, "有猫吗"
            )
        self.assertEqual(res["status"], "unavailable")
        self.assertEqual(res["detail"], "无法确认视频时长")

    async def test_inspect_video_clip_reuses_probe_cache_and_combines_12_frames(self):
        host = _Host({"VIDEO_VISION_PROVIDER_ID": "p", "VIDEO_VISION_FORMAT": "auto"})
        calls = []

        async def mock_gen(pid, content, **_kw):
            calls.append([p.get("type") for p in content])
            if any(p.get("type") == "video_url" for p in content):
                set_call_diagnostic(reason="unsupported", rejection="unsupported")
                return None
            if any(p.get("type") in {"file", "image_url"} for p in content):
                return json.dumps({"finding": "found", "evidence": "看到了", "moments": [{"start_seconds": 1, "end_seconds": 2}]})
            return None

        host._astrbot_multimodal_generate = mock_gen
        with tempfile.TemporaryDirectory() as folder:
            clip = Path(folder) / "clip.mp4"
            clip.write_bytes(b"clip")
            res1 = await host.video_watch._inspect_video_clip(str(clip), "有猫吗", 20.0, "p", None, "")
            res2 = await host.video_watch._inspect_video_clip(str(clip), "还有狗吗", 20.0, "p", None, "")
            self.assertEqual(res1["mode"], "video")
            self.assertEqual(res2["mode"], "video")
            # 第一次探测锁定为 file，第二次直接走 file，不再试 openai
            self.assertEqual(calls, [["video_url", "text"], ["file", "text"], ["file", "text"]])

            # 改配置触发合并 12 帧结构
            host.config["VIDEO_VISION_FORMAT"] = "none"
            frames = []
            for i in range(12):
                fp = Path(folder) / f"frame_{i}.jpg"
                fp.write_bytes(b"jpg")
                frames.append(str(fp))
            host.video_files._extract_video_frames = AsyncMock(return_value=frames)
            res3 = await host.video_watch._inspect_video_clip(str(clip), "有猫吗", 20.0, "p", None, "")
            self.assertEqual(res3["mode"], "frames")
            self.assertEqual(calls[-1], ["image_url"] * 12 + ["text"])

    async def test_multi_segment_video_accepts_absolute_and_relative_timelines(self):
        host = _Host(
            {
                "VIDEO_VISION_PROVIDER_ID": "gem",
                "VIDEO_VISION_FORMAT": "auto",
                "VIDEO_SEGMENT_MINUTES": 5,
                "VIDEO_SEGMENT_MAX_COUNT": 10,
            }
        )
        host._summary_llm_call = AsyncMock(
            return_value='{"impression": "完整七段看完了"}'
        )
        host._llm_call = AsyncMock(side_effect=AssertionError("不应回退到 _llm_call"))
        host.models = host
        host.bili_videos._get_video_oid = AsyncMock(return_value=123)
        host.bili_videos._get_video_tags = AsyncMock(return_value=[])
        host.bili_comments._get_hot_comments = AsyncMock(return_value=[])
        host.bili_videos._get_video_subtitle_lines = AsyncMock(return_value=[])
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "BV1BKpP6NELo.mp4"
            video.write_bytes(b"mp4")
            segs = []
            for i in range(7):
                sp = Path(folder) / f"BV1BKpP6NELo_seg{i}.mp4"
                sp.write_bytes(b"seg")
                segs.append(str(sp))

            host.video_files._download_video = AsyncMock(return_value=str(video))
            host.video_files._slice_video_segments = AsyncMock(return_value=segs)
            host.video_files._cleanup_video_artifacts = lambda *_a, **_kw: None

            # 第 1、2、3、5 段（0-based）故意输出原视频绝对秒数，第 0、4、6 段输出段内相对秒数。
            absolute_segments = {1, 2, 3, 5}
            prompts = []

            async def answer(_pid, content, **_kw):
                idx = len(prompts)
                prompts.append(content[-1]["text"])
                offset = idx * 300
                at = offset + 25 if idx in absolute_segments else 25
                return _seg_json(f"第{idx}段内容", at=at)

            host._astrbot_multimodal_generate = AsyncMock(side_effect=answer)
            info = {"bvid": "BV1BKpP6NELo", "title": "长视频", "desc": "", "tname": "科技", "duration": 1950}
            result = await host.video_watch._analyze_video_with_vision_inner(info)

        self.assertEqual(info["analysis_basis"], "video_media")
        self.assertNotIn("仅分析部分视频片段", result)
        for idx, expected_clock in enumerate(["[00:25]", "[05:25]", "[10:25]", "[15:25]", "[20:25]", "[25:25]", "[30:25]"]):
            self.assertIn(f"{expected_clock} 第{idx}段内容", result)
        self.assertIn("本段片长 300 秒，at 从 0 开始记本段内秒数", prompts[1])
        self.assertIn("本段片长 150 秒，at 从 0 开始记本段内秒数", prompts[6])


if __name__ == "__main__":
    unittest.main()
