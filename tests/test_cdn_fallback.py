"""CDN 线路探测与镜像改道：原线路不通时用可达镜像域名下载，不依赖任何代理。"""
import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

from video_parts import VideoParts

INFO = {
    "id": "BV1TEST12345",
    "formats": [
        {"format_id": "100023", "url": "https://cn-sh-cc-01-03.bilivideo.com/video/a.m4s?sign=1", "ext": "mp4"},
        {"format_id": "30280", "url": "https://cn-sh-cc-01-06.bilivideo.com/audio/a.m4a?sign=2", "ext": "m4a"},
    ],
}


class Probe(VideoParts):
    def __init__(self, reachable):
        self.config = {"SESSDATA": "s", "BILI_JCT": "j", "DEDE_USER_ID": "1"}
        self.reachable = reachable
        self.commands = []
        self.rewritten = []

    async def _run_process(self, *args, timeout=None):
        self.commands.append(list(args))
        if "-J" in args:
            return 0, json.dumps(INFO), ""
        if "--load-info-json" in args:
            path = args[args.index("--load-info-json") + 1]
            try:
                with open(path, encoding="utf-8") as handle:
                    info = json.load(handle)
                self.rewritten = [item["url"].split("/")[2] for item in info["formats"]]
            except Exception as exc:
                self.error = repr(exc)
                raise
            return 0, "", ""
        return 1, "", "unexpected"

    async def _reachable_cdn_host(self, hosts):
        return next((host for host in hosts if host in self.reachable), "")

    def _pick_downloaded_video_file(self, bvid, output_dir=None):
        return f"/fake/{bvid}.mp4"


class CdnFallbackTests(unittest.TestCase):
    def run_download(self, reachable):
        probe = Probe(reachable)
        with tempfile.TemporaryDirectory() as folder, patch("core.infra.video_files.TEMP_VIDEO_DIR", folder):
            result = asyncio.run(probe.video_files._download_video("BV1TEST12345"))
        return probe, result

    def test_reachable_original_cdn_skips_rewrite(self):
        probe, result = self.run_download({"cn-sh-cc-01-03.bilivideo.com"})
        self.assertIsNone(result)
        self.assertFalse(any("--load-info-json" in command for command in probe.commands))

    def test_unreachable_cdn_switches_to_mirror(self):
        probe, result = self.run_download({"upos-sz-mirrorcos.bilivideo.com"})
        self.assertEqual(result, "/fake/BV1TEST12345.mp4")
        self.assertEqual(set(probe.rewritten), {"upos-sz-mirrorcos.bilivideo.com"})
        loads = [command for command in probe.commands if "--load-info-json" in command]
        self.assertEqual(len(loads), 1)

    def test_no_reachable_host_falls_back_to_direct(self):
        probe, result = self.run_download(set())
        self.assertIsNone(result)
        self.assertFalse(any("--load-info-json" in command for command in probe.commands))

    def test_cdn_host_only_matches_bilivideo(self):
        probe = Probe(set())
        cdn = probe.video_files
        self.assertEqual(cdn._cdn_host("https://cn-sh-cc-01-03.bilivideo.com/a.m4s"), "cn-sh-cc-01-03.bilivideo.com")
        self.assertEqual(cdn._cdn_host("https://api.bilibili.com/x/player"), "")
        self.assertEqual(cdn._cdn_host(None), "")

    def test_swap_keeps_path_and_signature(self):
        probe = Probe(set())
        self.assertEqual(
            probe.video_files._swap_cdn_host(
                "https://cn-sh-cc-01-03.bilivideo.com/video/a.m4s?sign=1&x=2",
                "upos-sz-mirrorcos.bilivideo.com",
            ),
            "https://upos-sz-mirrorcos.bilivideo.com/video/a.m4s?sign=1&x=2",
        )


if __name__ == "__main__":
    unittest.main()
