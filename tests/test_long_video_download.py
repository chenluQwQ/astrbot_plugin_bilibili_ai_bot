import asyncio
import tempfile
import unittest
from unittest.mock import patch

from video_parts import VideoParts


class Probe(VideoParts):
    def __init__(self, results):
        self.config = {"SESSDATA": "s", "BILI_JCT": "j", "DEDE_USER_ID": "1"}
        self.results = list(results)
        self.timeouts = []

    async def _run_process(self, *args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return self.results.pop(0) if self.results else (1, "", "failed")


def download(probe, **kwargs):
    with tempfile.TemporaryDirectory() as directory, patch("core.infra.video_files.TEMP_VIDEO_DIR", directory):
        return asyncio.run(probe.video_files._download_video("BV1TEST12345", **kwargs))


class LongVideoDownloadTest(unittest.TestCase):
    def test_timeout_scales_with_duration(self):
        probe = Probe([])
        for duration, expected in ((None, 120), (0, 120), ("bad", 120), (200, 120), (3600, 1800), (86400, 3600)):
            self.assertEqual(probe.video_files._video_download_timeout(duration), expected)

    def test_long_video_timeout_does_not_retry_other_formats(self):
        probe = Probe([(124, "", "timeout")])
        self.assertIsNone(download(probe, duration=3600))
        self.assertEqual(probe.timeouts, [1800])

    def test_timeout_does_not_retry_other_formats(self):
        # 同一视频的其他格式指向同一 CDN，超时后换格式只是把同样的等待再来一遍。
        probe = Probe([(124, "", "timeout")])
        self.assertIsNone(download(probe, duration=60))
        self.assertEqual(probe.timeouts, [120])

    def test_network_error_is_reported_instead_of_later_format_error(self):
        probe = Probe([
            (1, "", "ERROR: unable to download video data: HTTP Error 503"),
            (1, "", "ERROR: [BiliBili] BV1TEST12345: Requested format is not available."),
        ])
        with patch("core.infra.video_files.logger") as log:
            self.assertIsNone(download(probe))
            warnings = " ".join(str(call.args[0]) for call in log.warning.call_args_list)
        self.assertEqual(len(probe.timeouts), 1)
        self.assertIn("503", warnings)


if __name__ == "__main__":
    unittest.main()
