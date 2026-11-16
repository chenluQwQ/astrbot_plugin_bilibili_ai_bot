"""CDN 线路探测：原线路可达时保持直连，不可达时换镜像域名后下载。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from video_parts import VideoParts

ORIGIN = "cn-sh-cc-01-13.bilivideo.com"
MIRROR = "upos-sz-mirrorcos.bilivideo.com"


class Probe(VideoParts):
    def __init__(self, folder, info):
        self.config = {}
        self.folder = folder
        self.info = info
        self.loaded = None
        self.download_calls = 0

    async def _run_process(self, *command, timeout=None):
        if "-J" in command:
            return 0, json.dumps(self.info), ""
        if "--load-info-json" in command:
            self.download_calls += 1
            path = command[command.index("--load-info-json") + 1]
            self.loaded = json.loads(Path(path).read_text(encoding="utf-8"))
            (Path(self.folder) / "BV1.mp4").write_bytes(b"x")
            return 0, "", ""
        return 1, "", "unexpected command"


class CdnMirrorTest(unittest.IsolatedAsyncioTestCase):
    def _video_files(self, folder, info):
        probe = Probe(folder, info)
        return probe, probe.video_files

    async def test_unreachable_origin_switches_to_mirror(self):
        info = {"formats": [{"url": f"https://{ORIGIN}/a?x=1"}, {"url": f"https://{ORIGIN}/b"}]}
        with tempfile.TemporaryDirectory() as folder:
            probe, video_files = self._video_files(folder, info)

            async def reachable(hosts):
                # 原线路全不通，镜像可达。
                return "" if ORIGIN in hosts else MIRROR

            video_files._reachable_cdn_host = reachable
            result = await video_files._download_via_reachable_cdn(
                "BV1", 480, os.path.join(folder, "cookies.txt"), output_dir=folder
            )

            self.assertTrue(result and result.endswith("BV1.mp4"))
            self.assertEqual(probe.download_calls, 1)
            hosts = {urlsplit(item["url"]).hostname for item in probe.loaded["formats"]}
            self.assertEqual(hosts, {MIRROR})

    async def test_reachable_origin_keeps_direct_path(self):
        info = {"formats": [{"url": f"https://{ORIGIN}/a"}]}
        with tempfile.TemporaryDirectory() as folder:
            probe, video_files = self._video_files(folder, info)

            async def reachable(hosts):
                return ORIGIN

            video_files._reachable_cdn_host = reachable
            result = await video_files._download_via_reachable_cdn(
                "BV1", 480, os.path.join(folder, "cookies.txt"), output_dir=folder
            )

            self.assertIsNone(result)
            self.assertEqual(probe.download_calls, 0)

    async def test_info_without_cdn_url_returns_none(self):
        info = {"formats": [{"url": "https://example.com/a"}]}
        with tempfile.TemporaryDirectory() as folder:
            probe, video_files = self._video_files(folder, info)
            result = await video_files._download_via_reachable_cdn(
                "BV1", 480, os.path.join(folder, "cookies.txt"), output_dir=folder
            )
            self.assertIsNone(result)
            self.assertEqual(probe.download_calls, 0)


if __name__ == "__main__":
    unittest.main()
