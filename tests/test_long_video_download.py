import asyncio
import tempfile
import unittest
from unittest.mock import patch

from core.video import VideoMixin


class Probe(VideoMixin):
    def __init__(self, results):
        self.config = {"SESSDATA": "s", "BILI_JCT": "j", "DEDE_USER_ID": "1"}
        self.results = list(results)
        self.timeouts = []

    async def _run_process(self, *args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return self.results.pop(0) if self.results else (1, "", "failed")


def download(probe, **kwargs):
    with tempfile.TemporaryDirectory() as directory, patch("core.video.TEMP_VIDEO_DIR", directory):
        return asyncio.run(probe._download_video("BV1TEST12345", **kwargs))


class LongVideoDownloadTest(unittest.TestCase):
    def test_timeout_scales_with_duration(self):
        probe = Probe([])
        for duration, expected in ((None, 120), (0, 120), ("bad", 120), (200, 120), (3600, 1800), (86400, 3600)):
            self.assertEqual(probe._video_download_timeout(duration), expected)

    def test_long_video_timeout_does_not_retry_other_formats(self):
        probe = Probe([(124, "", "timeout")])
        self.assertIsNone(download(probe, duration=3600))
        self.assertEqual(probe.timeouts, [1800])

    def test_short_video_timeout_still_tries_other_formats(self):
        probe = Probe([(124, "", "timeout")])
        self.assertIsNone(download(probe, duration=60))
        self.assertEqual(probe.timeouts, [120] * len(probe._format_fallbacks(480)))


if __name__ == "__main__":
    unittest.main()
