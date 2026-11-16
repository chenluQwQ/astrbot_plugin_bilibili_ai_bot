"""直连下载全失败时探线路：原线路连得上就不管，连不上换镜像域名；cookie 不落进视频信息文件。"""
import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from video_parts import VideoParts

ORIGIN = "cn-xx-01-13.bilivideo.com"
MIRROR = "upos-sz-mirrorcos.bilivideo.com"
INFO = {
    "cookies": "SESSDATA=secret",
    "http_headers": {"Cookie": "SESSDATA=secret", "Referer": "https://www.bilibili.com"},
    "formats": [
        {"url": f"https://{ORIGIN}/a.m4s?sig=1", "cookies": "SESSDATA=secret",
         "http_headers": {"Cookie": "SESSDATA=secret"}},
        {"url": f"https://{ORIGIN}/b.m4s", "fragments": [{"url": f"https://{ORIGIN}/b-1.m4s"}]},
    ],
}


class Probe(VideoParts):
    def __init__(self, folder, *, direct_ok=False, proxy=""):
        self.config = {"SESSDATA": "s", "BILI_JCT": "j", "DEDE_USER_ID": "1", "PROXY_URL": proxy}
        self.folder = folder
        self.direct_ok = direct_ok
        self.calls = []
        self.loaded = None
        self.info_mode = None

    async def _run_process(self, *command, timeout=None):
        if "-J" in command:
            self.calls.append("info")
            return 0, json.dumps(INFO), ""
        if "--load-info-json" in command:
            self.calls.append("mirror")
            path = command[command.index("--load-info-json") + 1]
            self.info_mode = stat.S_IMODE(os.stat(path).st_mode)
            self.loaded = json.loads(Path(path).read_text(encoding="utf-8"))
            (Path(self.folder) / "BV1TEST12345.mp4").write_bytes(b"x")
            return 0, "", ""
        self.calls.append("direct")
        if self.direct_ok:
            (Path(self.folder) / "BV1TEST12345.mp4").write_bytes(b"x")
            return 0, "", ""
        return 1, "", "ERROR: Unable to download"


def fake_reachable(reachable):
    async def probe(hosts):
        return next((host for host in hosts if host in reachable), "")
    return probe


class CdnMirrorTest(unittest.TestCase):
    def run_download(self, probe, reachable, folder):
        with patch("core.infra.video_files.TEMP_VIDEO_DIR", folder), \
                patch("core.infra.video_files._reachable_host", fake_reachable(reachable)):
            return asyncio.run(probe.video_files._download_video("BV1TEST12345"))

    def test_direct_success_never_probes(self):
        with tempfile.TemporaryDirectory() as folder:
            probe = Probe(folder, direct_ok=True)
            self.assertTrue(self.run_download(probe, {ORIGIN}, folder))
            self.assertEqual(probe.calls, ["direct"])

    def test_reachable_origin_does_not_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            probe = Probe(folder)
            self.assertIsNone(self.run_download(probe, {ORIGIN, MIRROR}, folder))
            self.assertNotIn("mirror", probe.calls)
            self.assertEqual(probe.calls[-1], "info")

    def test_dead_origin_switches_to_mirror_without_cookies(self):
        with tempfile.TemporaryDirectory() as folder:
            probe = Probe(folder)
            result = self.run_download(probe, {MIRROR}, folder)
            self.assertTrue(result and result.endswith("BV1TEST12345.mp4"))
            self.assertEqual(probe.calls[-2:], ["info", "mirror"])
            hosts = {urlsplit(item["url"]).hostname for item in probe.loaded["formats"]}
            self.assertEqual(hosts, {MIRROR})
            self.assertEqual(urlsplit(probe.loaded["formats"][1]["fragments"][0]["url"]).hostname, MIRROR)
            self.assertNotIn("secret", json.dumps(probe.loaded))
            self.assertEqual(probe.info_mode, 0o600)
            self.assertEqual([name for name in os.listdir(folder) if name.endswith(".json")], [])

    def test_after_mirror_success_next_download_goes_mirror_first(self):
        with tempfile.TemporaryDirectory() as folder:
            probe = Probe(folder)
            self.run_download(probe, {MIRROR}, folder)
            os.remove(os.path.join(folder, "BV1TEST12345.mp4"))
            probe.calls.clear()
            self.assertTrue(self.run_download(probe, {MIRROR}, folder))
            self.assertEqual(probe.calls, ["info", "mirror"])

    def test_proxy_configured_skips_mirror(self):
        with tempfile.TemporaryDirectory() as folder:
            probe = Probe(folder, proxy="http://127.0.0.1:7890")
            self.assertIsNone(self.run_download(probe, {MIRROR}, folder))
            self.assertNotIn("info", probe.calls)


if __name__ == "__main__":
    unittest.main()
