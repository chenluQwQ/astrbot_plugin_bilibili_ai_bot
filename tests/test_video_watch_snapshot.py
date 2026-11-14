"""理解视频的快照：字幕校验、直读探测、截帧、整合、抽查和视频记忆缓存。

假的只有边界（B站接口、模型、视频文件操作、看过记录、json 文件），中间是真代码。
搬进 ``core/domain/video_watch.py`` 时用它确认没搬错。

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_video_watch_snapshot.py
"""
import copy
import json
import os
import re
import tempfile
import unittest

from bili_parts import BiliParts
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.config import EXTERNAL_MEMORY_FILE, VIDEO_MEMORY_FILE
from vision_fakes import vision_on
from core.settings import Settings
from video_parts import VideoParts
from core.video_diagnostics import get_call_diagnostic, set_call_diagnostic
from itself import itself

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "video_watch.json"
BV = "BV1234567890"
INFO = {"bvid": BV, "title": "标题", "desc": "简介", "tname": "分区", "owner_name": "UP", "cid": 22, "oid": 11, "duration": 60}
LONG_INFO = {**INFO, "duration": 300}
VISION = {"VIDEO_VISION_PROVIDER_ID": "vp", "VIDEO_VISION_MODEL": "vm", "VIDEO_SEGMENT_MINUTES": 5, "VIDEO_SEGMENT_MAX_COUNT": 3}
ALWAYS = {**VISION, "VIDEO_VISUAL_ANALYSIS_POLICY": "always"}
DIRECT = {**ALWAYS, "VIDEO_VISION_FORMAT": "auto"}


def seg(text, at=1):
    """视频模型一段的记录（按时间记的 JSON）。"""
    return json.dumps({"timeline": [{"at": at, "text": text}], "impression": f"{text}的印象", "audio": "有人在讲话"}, ensure_ascii=False)


def rejected(kind, reason="不行"):
    return {"rejection": kind, "reason": reason}


class Probe(VideoParts, BiliParts):
    models = itself
    web_search = itself
    search_judge = itself
    vision = property(vision_on)

    def __init__(self, folder, config=None, **script):
        self.config = dict(config or {})
        self.settings = Settings(self.config)
        self.folder = folder
        self.files = copy.deepcopy(script.pop("files", {}))
        self._memory = script.pop("memory", [])
        self.script = {key: list(value) if isinstance(value, list) else value for key, value in script.items()}
        self.calls = []

    def _next(self, name, default=None):
        value = self.script.get(name, default)
        if isinstance(value, list):
            value = value.pop(0) if value else default
        if isinstance(value, Exception):
            raise value
        return value

    def _load_json(self, key, default=None):
        return copy.deepcopy(self.files.get(key, default))

    def _save_json(self, key, value):
        self.calls.append(["save", os.path.basename(key)])
        self.files[key] = copy.deepcopy(value)

    def _append_json_list(self, key, entry, cap=None):
        self.calls.append(["append", os.path.basename(key), cap])
        self.files.setdefault(key, []).append(copy.deepcopy(entry))

    async def _get_video_oid(self, bvid):
        self.calls.append(["oid", bvid])
        return 11

    async def _get_video_info(self, oid):
        self.calls.append(["info", oid])
        return self._next("info", dict(INFO))

    async def _get_video_tags(self, bvid):
        self.calls.append(["tags", bvid])
        return self._next("tags", [])

    async def _get_hot_comments(self, oid):
        self.calls.append(["hot", oid])
        return self._next("hot", [])

    async def _get_video_subtitles(self, bvid, cid):
        self.calls.append(["subtitles", bvid, cid])
        return self._next("subtitles", "")

    async def _get_video_subtitle_lines(self, bvid, cid):
        self.calls.append(["subtitle_lines", bvid, cid])
        return self._next("subtitle_lines", [])

    async def _should_search_for_video(self, info, extra):
        self.calls.append(["should_search", extra])
        return self._next("should_search", "")

    async def _web_search(self, query):
        self.calls.append(["search", query])
        return self._next("search", "")

    async def _summary_llm_call(self, prompt, max_tokens=None):
        self.calls.append(["summary", prompt, max_tokens])
        return self._next("summary", "总结")

    # 代码写的是 ``getattr(self, "_summary_llm_call", self._llm_call)``，默认值总会被求值。
    async def _llm_call(self, prompt, max_tokens=None):
        raise AssertionError("配了总结模型入口时不该走到主模型")

    def _get_video_vision_client(self):
        self.calls.append(["client"])
        return self.script.get("client")

    async def _astrbot_multimodal_generate(self, provider_id, content, max_tokens=250):
        self.calls.append(["multimodal", provider_id, self._parts(content), max_tokens])
        return self._model_answer("multimodal")

    async def _vision_call(self, client, model, content, max_tokens=250):
        self.calls.append(["vision", model, self._parts(content), max_tokens])
        return self._model_answer("vision")

    def _model_answer(self, name):
        value = self._next(name)
        if isinstance(value, dict):
            set_call_diagnostic(**value)
            return None
        return value

    @staticmethod
    def _parts(content):
        """base64 太长，只记形状和文字。"""
        shape = []
        for part in content:
            text = json.dumps(part, ensure_ascii=False)
            shape.append(re.sub(r"base64,[A-Za-z0-9+/=]+", "base64,…", text))
        return shape

    async def _download_video(self, bvid, max_height=480, *, output_dir=None, duration=None):
        self.calls.append(["download", bvid, bool(output_dir)])
        value = self._next("download", "file")
        if not value:
            set_call_diagnostic(reason="HTTP 403")
            return None
        path = Path(output_dir or self.folder) / f"{bvid}.mp4"
        path.write_bytes(b"video")
        return str(path)

    async def _slice_video_segments(self, video_path, segment_sec=300, max_segments=10):
        self.calls.append(["slice", segment_sec, max_segments])
        count = self._next("segments", 1)
        if isinstance(count, Exception):
            raise count
        paths = []
        for index in range(count):
            path = Path(self.folder) / f"seg{index}.mp4"
            path.write_bytes(b"segment")
            paths.append(str(path))
        return paths

    async def _extract_video_frames(self, video_path, count=5):
        self.calls.append(["frames", count])
        frames = []
        for index in range(self._next("frames", 2)):
            path = Path(self.folder) / f"frame_{index}.jpg"
            path.write_bytes(b"frame")
            frames.append(str(path))
        return frames

    def _cleanup_video_artifacts(self, video_path, frames=None):
        self.calls.append(["cleanup", bool(video_path), len(frames or [])])

    async def _get_video_duration(self, path):
        self.calls.append(["duration"])
        return self._next("duration", 300.0)

    async def _run_process(self, *command, timeout=None):
        code = self._next("clip_code", 0)
        self.calls.append(["ffmpeg", [str(part).replace(self.folder, "<DIR>") for part in command[1:-1]], code])
        if not code:
            Path(command[-1]).write_bytes(b"clip")
        return code, "", ""

    async def _seen_video_record(self, bvid):
        self.calls.append(["seen", bvid])
        return self._next("seen")


def cache_entry(**extra):
    return {VIDEO_MEMORY_FILE: {BV: {"bvid": BV, "analysis": "看过的内容", "summary": "摘要", "time": datetime.now().strftime("%Y-%m-%d %H:%M"), "analysis_basis": "video_media", **extra}}}


def days_ago(days):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")


INSPECT_OK = '{"finding":"found","evidence":"看到猫","moments":[{"start_seconds":1,"end_seconds":2}]}'
INSPECT_NO = '{"finding":"not_observed","evidence":"没看到","moments":[]}'

# 场景名 → (方法, 参数, Probe 的配置和脚本)
ANALYZE = "_analyze_video_with_vision"
SCENES = {
    # 入口：缓存、看过、强制重看
    "analyze/from_cache": (ANALYZE, (INFO,), dict(files=cache_entry())),
    "analyze/seen_only": (ANALYZE, (INFO,), dict(seen={"bvid": BV})),
    "analyze/force_ignores_cache": (ANALYZE, ({**INFO, "_force_rewatch": True},), dict(files=cache_entry())),
    "analyze/no_bvid": (ANALYZE, ({**INFO, "bvid": ""},), {}),
    "analyze/exception_becomes_metadata": (ANALYZE, (INFO,), dict(summary=RuntimeError("模型炸了"))),
    # 上下文：标签、热评、搜索、字幕校验
    "context/tags_hot_search": (ANALYZE, (INFO,), dict(config={"ENABLE_WEB_SEARCH": True}, tags=["甲", "乙"], hot=["梗"], should_search="查", search="搜到的")),
    "context/search_no_result": (ANALYZE, (INFO,), dict(config={"ENABLE_WEB_SEARCH": True}, should_search="查", search="")),
    "context/needs_oid": (ANALYZE, ({**INFO, "oid": None},), {}),
    "subtitle/kept": (ANALYZE, (INFO,), dict(subtitles="字幕", summary=['{"relevant": true}', "总结"])),
    "subtitle/rejected": (ANALYZE, (INFO,), dict(subtitles="字幕", summary=['```json\n{"relevant": false, "reason": "串了"}\n```', "总结"])),
    "subtitle/check_empty_keeps": (ANALYZE, (INFO,), dict(subtitles="字幕", summary=["", "总结"])),
    "subtitle/check_garbled_keeps": (ANALYZE, (INFO,), dict(subtitles="字幕", summary=["不是 JSON", "总结"])),
    "text/summary_empty_uses_title": (ANALYZE, (INFO,), dict(summary="")),
    # 要不要看画面
    "policy/subtitle_skips_media": (ANALYZE, (INFO,), dict(config={**VISION, "VIDEO_VISUAL_ANALYSIS_POLICY": "when_text_insufficient"}, subtitles="字幕", summary=['{"relevant": true}', "总结"])),
    "policy/always_uses_media": (ANALYZE, (INFO,), dict(config=ALWAYS, subtitles="字幕", summary=['{"relevant": true}', "整合"], frames=2, multimodal=seg("画面"))),
    # 媒体：配置和前置失败
    "media/not_configured": (ANALYZE, (INFO,), dict(config={})),
    "media/download_failed": (ANALYZE, (INFO,), dict(config=VISION, download=None)),
    "media/no_segments": (ANALYZE, (INFO,), dict(config=VISION, segments=0)),
    "media/slice_raises": (ANALYZE, (INFO,), dict(config=VISION, segments=RuntimeError("切坏了"))),
    "media/legacy_segment_seconds": (ANALYZE, (INFO,), dict(config={**VISION, "VIDEO_SEGMENT_MINUTES": 0, "VIDEO_SEGMENT_SEC": 120}, multimodal=seg("画面"))),
    # 截帧
    "frames/provider_ok_then_merge": (ANALYZE, (INFO,), dict(config=VISION, multimodal=seg("画面"), summary="整合后")),
    "frames/merge_empty_keeps_visual": (ANALYZE, (INFO,), dict(config=VISION, multimodal=seg("画面"), summary="")),
    "frames/falls_back_to_client": (ANALYZE, (INFO,), dict(config=VISION, client="c", multimodal=None, vision=seg("客户端看到的"))),
    "frames/none_extracted": (ANALYZE, (INFO,), dict(config=VISION, frames=0)),
    "frames/model_empty": (ANALYZE, (INFO,), dict(config=VISION, multimodal=None)),
    "frames/unavailable_marks_dead": (ANALYZE, (INFO,), dict(config=VISION, multimodal=rejected("unavailable", "没权限"))),
    "frames/dead_stops_remaining": (ANALYZE, (INFO,), dict(config=VISION, segments=3, multimodal=rejected("unavailable", "没权限"))),
    # 多段
    "segments/consolidated": (ANALYZE, (INFO,), dict(config=VISION, segments=2, multimodal=[seg("一段"), seg("二段")], summary=["汇总", "整合"])),
    "segments/consolidation_empty_joins": (ANALYZE, (INFO,), dict(config=VISION, segments=2, multimodal=[seg("一段"), seg("二段")], summary=["", "整合"])),
    "segments/one_fails_is_partial": (ANALYZE, (INFO,), dict(config=VISION, segments=2, multimodal=[None, seg("二段")], summary="整合")),
    "segments/long_video_is_partial": (ANALYZE, ({**INFO, "duration": 9000},), dict(config=VISION, multimodal=seg("画面"), summary="整合")),
    "segments/partial_prefix_restored": (ANALYZE, ({**INFO, "duration": 9000},), dict(config=VISION, multimodal=seg("画面"), summary="没有前缀的整合")),
    "segments/all_fail": (ANALYZE, (INFO,), dict(config=VISION, segments=2, multimodal=None)),
    # 按时间记：格式、声音、截图配字幕
    "timeline/garbled_direct_fails_segment": (ANALYZE, (INFO,), dict(config=DIRECT, multimodal="一段散文，不是 JSON", summary="总结")),
    "timeline/time_outside_segment_rejected": (ANALYZE, (INFO,), dict(config=DIRECT, multimodal=seg("编的", at=500), summary="总结")),
    "timeline/heard_nothing_noted": (ANALYZE, (INFO,), dict(config={**DIRECT, "VIDEO_VISION_AUDIO": True}, multimodal=json.dumps({"timeline": [{"at": 3, "text": "有人在切木板"}], "impression": "做木工", "audio": "听不到"}, ensure_ascii=False))),
    "timeline/frames_get_timed_subtitles": (ANALYZE, (INFO,), dict(config=VISION, subtitle_lines=[[{"from": 2.0, "to": 4.0, "content": "先量尺寸"}, {"from": 70.0, "to": 72.0, "content": "片外"}]], summary=['{"relevant": true}'], multimodal=seg("画面"))),
    "timeline/audio_off_strips_track": (ANALYZE, (INFO,), dict(config={**DIRECT, "VIDEO_VISION_AUDIO": False}, multimodal=seg("直读"))),
    # 视频直读
    "direct/auto_first_works": (ANALYZE, (INFO,), dict(config=DIRECT, multimodal=seg("直读"), summary="整合")),
    "direct/auto_probes_until_works": (ANALYZE, (INFO,), dict(config=DIRECT, multimodal=[rejected("unsupported"), rejected("unsupported"), seg("直读")], summary="整合")),
    "direct/locks_format_for_next_segment": (ANALYZE, (INFO,), dict(config=DIRECT, segments=2, multimodal=[rejected("unsupported"), seg("一段"), seg("二段")], summary=["汇总", "整合"])),
    "direct/all_unsupported_uses_frames": (ANALYZE, (INFO,), dict(config=DIRECT, multimodal=[rejected("unsupported")] * 4 + [seg("帧")], summary="整合")),
    "direct/skip_remembered_next_segment": (ANALYZE, (INFO,), dict(config=DIRECT, segments=2, multimodal=[rejected("unsupported")] * 4 + [seg("帧一"), seg("帧二")], summary=["汇总", "整合"])),
    "direct/unavailable_stops": (ANALYZE, (INFO,), dict(config=DIRECT, multimodal=rejected("unavailable", "没权限"))),
    "direct/too_large_uses_frames": (ANALYZE, (INFO,), dict(config=DIRECT, multimodal=[rejected("too_large", "太大"), seg("帧")], summary="整合")),
    "direct/other_error_tries_next": (ANALYZE, (INFO,), dict(config=DIRECT, multimodal=[rejected("other", "抖了"), seg("直读")], summary="整合")),
    "direct/fixed_format": (ANALYZE, (INFO,), dict(config={**ALWAYS, "VIDEO_VISION_FORMAT": "gemini"}, multimodal=seg("直读"), summary="整合")),
    "direct/file_format": (ANALYZE, (INFO,), dict(config={**ALWAYS, "VIDEO_VISION_FORMAT": "file"}, multimodal=seg("直读"), summary="整合")),
    "direct/qwen_format": (ANALYZE, (INFO,), dict(config={**ALWAYS, "VIDEO_VISION_FORMAT": "qwen", "VIDEO_VISION_FPS": 4}, multimodal=seg("直读"), summary="整合")),
    "direct/client_fallback": (ANALYZE, (INFO,), dict(config={**ALWAYS, "VIDEO_VISION_FORMAT": "openai"}, client="c", multimodal=None, vision=seg("客户端直读"), summary="整合")),
    # 视频记忆缓存
    "cached/miss": ("_cached_video_memory", (BV,), {}),
    "cached/hit": ("_cached_video_memory", (BV,), dict(files=cache_entry())),
    "cached/preview_only": ("_cached_video_memory", (BV,), dict(files=cache_entry(content_analyzed=False))),
    "cached/group_share_preview": ("_cached_video_memory", (BV,), dict(files=cache_entry(source="group_share"))),
    "cached/subtitle_mismatch": ("_cached_video_memory", (BV,), dict(files=cache_entry(analysis="字幕与本视频内容不符，先说明"))),
    "cached/external_memory": ("_cached_video_memory", (BV,), dict(files={EXTERNAL_MEMORY_FILE: {BV: {"description": "旧记忆", "up_name": "UP", "up_mid": 3, "watched_at": days_ago(1)}}})),
    "cached/semantic_memory": ("_cached_video_memory", (BV,), dict(memory=[{"bvid": BV, "memory_type": "video", "video_summary": "语义记忆", "video_title": "T", "time": days_ago(1)}])),
    "cached/old_becomes_long_term": ("_cached_video_memory", (BV,), dict(files=cache_entry(time=days_ago(20)))),
    "cached/very_old_fades": ("_cached_video_memory", (BV,), dict(files=cache_entry(time=days_ago(100), review="好看"))),
    # 抽查一段
    "inspect/bad_bvid": ("_inspect_comment_video", ({"bvid": "x"}, "有猫吗"), {}),
    "inspect/empty_question": ("_inspect_comment_video", (INFO, ""), {}),
    "inspect/no_model": ("_inspect_comment_video", (INFO, "有猫吗"), {}),
    "inspect/download_failed": ("_inspect_comment_video", (INFO, "有猫吗"), dict(config=VISION, download=None)),
    "inspect/three_samples": ("_inspect_comment_video", (INFO, "有猫吗"), dict(config=VISION, multimodal=[INSPECT_OK, INSPECT_NO, INSPECT_NO])),
    "inspect/range": ("_inspect_comment_video", (LONG_INFO, "有猫吗", 100, 130), dict(config=VISION, multimodal=INSPECT_OK)),
    "inspect/clip_failed": ("_inspect_comment_video", (LONG_INFO, "有猫吗", 100, 130), dict(config=VISION, clip_code=1)),
    "inspect/garbled_answer": ("_inspect_comment_video", (LONG_INFO, "有猫吗", 100, 130), dict(config=VISION, multimodal="不是 JSON")),
    "inspect/direct_then_frames": ("_inspect_comment_video", (LONG_INFO, "有猫吗", 100, 130), dict(config=DIRECT, multimodal=["不是 JSON", INSPECT_OK])),
    "inspect/no_frames": ("_inspect_comment_video", (LONG_INFO, "有猫吗", 100, 130), dict(config=VISION, frames=0)),
    "memory_context/no_video": ("_get_comment_video_memory_context", (11,), dict(info=None)),
    "memory_context/not_watched": ("_get_comment_video_memory_context", (11,), {}),
    "memory_context/watched": ("_get_comment_video_memory_context", (11,), dict(files=cache_entry(review="好看"))),
}


def _normalize(value, folder):
    text = json.dumps(value, ensure_ascii=False, default=lambda o: f"<{type(o).__name__}>")
    text = text.replace(folder, "<DIR>")
    text = re.sub(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", "<TIME>", text)
    # 临时目录名是随机的。
    text = re.sub(r"inspection_[A-Za-z0-9_]+", "inspection_<RANDOM>", text)
    return json.loads(text)


class VideoWatchSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def _scene(self, method, args, script):
        with tempfile.TemporaryDirectory() as folder:
            probe = Probe(folder, **copy.deepcopy(script))
            set_call_diagnostic()
            args = copy.deepcopy(args)
            patches = [patch(f"{name}.TEMP_VIDEO_DIR", folder) for name in ("core.domain.video_watch",)]
            for item in patches:
                item.start()
            try:
                result = getattr(probe.video_watch, method)(*args)
                result = await result if hasattr(result, "__await__") else result
            except Exception as exc:
                result = {"raises": type(exc).__name__}
            finally:
                for item in patches:
                    item.stop()
            files = {os.path.basename(key): value for key, value in probe.files.items()}
            return _normalize({
                "result": result, "calls": probe.calls, "info": args[0] if isinstance(args[0], dict) else None,
                "files": files, "diagnostic": get_call_diagnostic(),
            }, folder)

    async def test_matches_snapshot(self):
        results = {name: await self._scene(*scene) for name, scene in SCENES.items()}
        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS") or not SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(results, json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
