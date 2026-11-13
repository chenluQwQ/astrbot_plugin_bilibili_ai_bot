"""视频文件操作的快照：下载、压缩、切片、截帧发了什么命令、留下什么文件、返回什么。

搬进 ``core/infra/video_files.py`` 时用它确认没搬错。

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_video_files_snapshot.py
"""
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.settings import Settings
from video_parts import VideoParts
from core.video_diagnostics import get_call_diagnostic

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "video_files.json"
COOKIES = {"SESSDATA": "sess", "BILI_JCT": "jct", "DEDE_USER_ID": "1"}


class Probe(VideoParts):
    def __init__(self, folder, script=None, config=None):
        self.config = {**COOKIES, **(config or {})}
        self.settings = Settings(self.config)
        self.folder = folder
        self.script = list(script or [])
        self.calls = []

    def _clean(self, value):
        return str(value).replace(self.folder, "<DIR>")

    async def _run_process(self, *command, timeout=None):
        if "-J" in command:
            # 线路探测在这里不启用，快照只记录直连下载命令
            return 1, "", "probe disabled"
        step = self.script.pop(0) if self.script else (0, "", "")
        code, stdout, stderr, *files = step
        call = {"command": [self._clean(part) for part in command], "timeout": timeout}
        cookie = next((part for part in command if str(part).endswith("_cookies.txt")), None)
        if cookie:
            call["cookie_file"] = Path(cookie).read_text(encoding="utf-8")
            call["cookie_mode"] = oct(os.stat(cookie).st_mode & 0o777)
        self.calls.append(call)
        for name, size in (files[0] if files else {}).items():
            path = Path(self.folder) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * size)
        return code, stdout, stderr

    def listing(self):
        return sorted(
            os.path.relpath(os.path.join(root, name), self.folder)
            for root, _, names in os.walk(self.folder) for name in names
        )


FAIL = (1, "", "格式不可用")
MP4 = {"BV1.mp4": 5}
# 场景名 → (方法, 参数函数(目录), 命令脚本, 预先放好的文件, 配置)
SCENES = {
    "download/first_format": ("_download_video", lambda d: ("BV1",), [(0, "", "", MP4)], {}, {}),
    "download/into_output_dir": ("_download_video", lambda d: ("BV1",), [(0, "", "", MP4)], {}, {"_use_output_dir": True}),
    "download/height": ("_download_video", lambda d: ("BV1", 720), [(0, "", "", MP4)], {}, {}),
    "download/buvid_and_proxy": ("_download_video", lambda d: ("BV1",), [(0, "", "", MP4)], {}, {"BUVID3": "b3", "PROXY_URL": "http://127.0.0.1:7890"}),
    "download/audio_only_then_ok": ("_download_video", lambda d: ("BV1",), [(0, "", "", {"BV1.m4a": 5}), (0, "", "", MP4)], {}, {}),
    "download/fail_then_ok": ("_download_video", lambda d: ("BV1",), [FAIL, (0, "", "", MP4)], {}, {}),
    "download/all_fail": ("_download_video", lambda d: ("BV1", 360), [FAIL] * 20, {}, {}),
    "download/no_stderr": ("_download_video", lambda d: ("BV1", 360), [(1, "", "")] * 20, {}, {}),
    "download/clears_leftovers": ("_download_video", lambda d: ("BV1",), [(0, "", "", MP4)], {"BV1.part": 1, "BV2.mp4": 1}, {}),
    "download/cookie_unwritable": ("_download_video", lambda d: ("BV1",), [], {}, {"_missing_dir": True}),
    "pick/prefers_mp4_then_size": ("_pick_downloaded_video_file", lambda d: ("BV1",), [], {"BV1.mkv": 50, "BV1.mp4": 5, "BV1_a.mp4": 9, "BV1.m4a": 90, "BV1.part": 90, "BV1_cookies.txt": 9, "BV1.txt": 9, "BV2.mp4": 90}, {}),
    "pick/empty_file": ("_pick_downloaded_video_file", lambda d: ("BV1",), [], {"BV1.mp4": 0}, {}),
    "pick/none": ("_pick_downloaded_video_file", lambda d: ("BV1",), [], {}, {}),
    "cleanup_partial": ("_cleanup_partial_downloads", lambda d: ("BV1",), [], {"BV1.mp4": 1, "BV1_cookies.txt": 1, "BV2.mp4": 1}, {}),
    "duration/ok": ("_get_video_duration", lambda d: ("v.mp4",), [(0, " 12.5\n", "")], {}, {}),
    "duration/empty": ("_get_video_duration", lambda d: ("v.mp4",), [(0, "", "")], {}, {}),
    "duration/garbage": ("_get_video_duration", lambda d: ("v.mp4",), [(0, "N/A", "")], {}, {}),
    "duration/fail": ("_get_video_duration", lambda d: ("v.mp4",), [(1, "9", "e")], {}, {}),
    "compress/ok": ("_compress_video", lambda d: (f"{d}/v.mp4",), [(0, "", "", {"v_compressed.mp4": 3})], {"v.mp4": 5}, {}),
    "compress/empty_output": ("_compress_video", lambda d: (f"{d}/v.mp4",), [(0, "", "", {"v_compressed.mp4": 0})], {"v.mp4": 5}, {}),
    "compress/fail": ("_compress_video", lambda d: (f"{d}/v.mp4",), [(1, "", "坏了")], {"v.mp4": 5}, {}),
    "slice/short": ("_slice_video_segments", lambda d: (f"{d}/v.mp4", 300, 10), [(0, "100", ""), (0, "", "", {"v_compressed.mp4": 3})], {"v.mp4": 5}, {}),
    "slice/unknown_duration": ("_slice_video_segments", lambda d: (f"{d}/v.mp4", 300, 10), [(1, "", ""), (0, "", "", {"v_compressed.mp4": 3})], {"v.mp4": 5}, {}),
    "slice/long": ("_slice_video_segments", lambda d: (f"{d}/v.mp4", 100, 10), [(0, "205", ""), (0, "", "", {"v_seg0.mp4": 1}), (0, "", "", {"v_seg1.mp4": 1})], {"v.mp4": 5}, {}),
    "slice/remainder_kept": ("_slice_video_segments", lambda d: (f"{d}/v.mp4", 100, 10), [(0, "215", ""), (0, "", "", {"v_seg0.mp4": 1}), (0, "", "", {"v_seg1.mp4": 1}), (0, "", "", {"v_seg2.mp4": 1})], {"v.mp4": 5}, {}),
    "slice/capped": ("_slice_video_segments", lambda d: (f"{d}/v.mp4", 100, 2), [(0, "900", ""), (0, "", "", {"v_seg0.mp4": 1}), (0, "", "", {"v_seg1.mp4": 1})], {"v.mp4": 5}, {}),
    "slice/one_fails": ("_slice_video_segments", lambda d: (f"{d}/v.mp4", 100, 10), [(0, "205", ""), (1, "", "e"), (0, "", "", {"v_seg1.mp4": 1})], {"v.mp4": 5}, {}),
    "slice/all_fail": ("_slice_video_segments", lambda d: (f"{d}/v.mp4", 100, 10), [(0, "205", ""), (1, "", ""), (1, "", "")], {"v.mp4": 5}, {}),
    "frames/ok": ("_extract_video_frames", lambda d: (f"{d}/v.mp4", 2), [(0, "30", ""), (0, "", "", {"v_frames/frame_0.jpg": 1}), (0, "", "", {"v_frames/frame_1.jpg": 1})], {"v.mp4": 5}, {}),
    "frames/one_fails": ("_extract_video_frames", lambda d: (f"{d}/v.mp4", 2), [(0, "30", ""), (1, "", ""), (0, "", "", {"v_frames/frame_1.jpg": 1})], {"v.mp4": 5}, {}),
    "frames/no_duration": ("_extract_video_frames", lambda d: (f"{d}/v.mp4", 1), [(1, "", ""), (0, "", "", {"v_frames/frame_0.jpg": 1})], {"v.mp4": 5}, {}),
    "frames/garbage_duration": ("_extract_video_frames", lambda d: (f"{d}/v.mp4", 1), [(0, "N/A", ""), (0, "", "", {"v_frames/frame_0.jpg": 1})], {"v.mp4": 5}, {}),
    "artifacts/with_video": ("_cleanup_video_artifacts", lambda d: (f"{d}/v.mp4", [f"{d}/f.jpg", f"{d}/gone.jpg"]), [], {"v.mp4": 1, "f.jpg": 1, "keep.mp4": 1, "v_frames/frame_0.jpg": 1}, {}),
    "artifacts/frames_only": ("_cleanup_video_artifacts", lambda d: (None, [f"{d}/f.jpg"]), [], {"f.jpg": 1, "keep.mp4": 1}, {}),
}


def _temp_dir_patches(folder):
    """默认目录写在模块常量里，搬家前后所在的模块不同，都换掉。"""
    patches = []
    for name in ("core.video", "core.infra.video_files"):
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if hasattr(module, "TEMP_VIDEO_DIR"):
            patches.append(patch.object(module, "TEMP_VIDEO_DIR", folder))
    return patches


class VideoFilesSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def _scene(self, method, make_args, script, files, config):
        config = dict(config)
        use_output_dir = config.pop("_use_output_dir", False)
        missing_dir = config.pop("_missing_dir", False)
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "work")
            if not missing_dir:
                os.makedirs(folder)
            probe = Probe(folder, script, config)
            for name, size in files.items():
                path = Path(folder) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * size)
            patches = _temp_dir_patches(folder)
            for item in patches:
                item.start()
            try:
                kwargs = {"output_dir": folder} if use_output_dir else {}
                result = getattr(probe.video_files, method)(*make_args(folder), **kwargs)
                result = await result if hasattr(result, "__await__") else result
            finally:
                for item in patches:
                    item.stop()
            leftover = probe.listing() if os.path.isdir(folder) else None
            return json.loads(json.dumps({
                "result": json.loads(probe._clean(json.dumps(result, ensure_ascii=False))),
                "calls": probe.calls,
                "files": leftover,
                "diagnostic": json.loads(probe._clean(json.dumps(get_call_diagnostic(), ensure_ascii=False))) if method == "_download_video" else None,
            }, ensure_ascii=False))

    async def test_matches_snapshot(self):
        results = {name: await self._scene(*scene) for name, scene in SCENES.items()}
        probe = Probe("")
        results["format_fallbacks"] = {
            str(height): probe.video_files._format_fallbacks(height) for height in (480, 360, 144, 1080, "坏", 0)
        }
        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS") or not SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(results, json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
