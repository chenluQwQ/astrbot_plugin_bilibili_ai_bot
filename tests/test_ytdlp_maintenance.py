"""yt-dlp：下载失败要看到真正的错误；每周更新默认关闭、不重复跑。"""
from base_parts import attach_base
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from core.infra.video_files import _yt_dlp_error
from core.infra.ytdlp_update import YtDlpUpdater
from core.storage.json_file import JsonFile


class ErrorTextTests(unittest.TestCase):
    def test_real_error_survives_the_old_version_warning(self):
        stderr = ("WARNING: Your yt-dlp version (2025.01.01) is older than 90 days!\n"
                  "         It is strongly recommended to always use the latest version.\n"
                  "ERROR: [BiliBili] BV1: Unable to download webpage: HTTP Error 412; please report this issue on  "
                  "https://github.com/x , filling out the appropriate issue template. Confirm you are on the latest version using  yt-dlp -U\n")
        self.assertEqual(_yt_dlp_error(stderr), "ERROR: [BiliBili] BV1: Unable to download webpage: HTTP Error 412")

    def test_output_without_error_line_is_kept(self):
        self.assertEqual(_yt_dlp_error("  出了点事\n"), "出了点事")


class UpdaterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = JsonFile(Path(folder.name) / "ytdlp_update.json")

    def _updater(self, enabled=True, pip=(0, "", "")):
        versions = iter(["2025.1.1", "2026.1.1"])

        async def run(*args, timeout=300):
            if args[0] == "yt-dlp":
                return 0, next(versions, "2026.1.1") + "\n", ""
            return pip

        self.run = AsyncMock(side_effect=run)
        return YtDlpUpdater(attach_base(SimpleNamespace(settings={"ENABLE_YTDLP_AUTO_UPDATE": enabled}, _run_process=self.run)), self.state)

    def _pip_calls(self):
        return [c for c in self.run.await_args_list if c.args[0] != "yt-dlp"]

    async def test_off_by_default(self):
        self.assertIsNone(self._updater(enabled=False).maybe_start())
        self.run.assert_not_awaited()

    async def test_updates_only_this_package_once_a_week(self):
        updater = self._updater()
        await updater.maybe_start()
        args = self._pip_calls()[0].args
        self.assertEqual(args[-1], "yt-dlp")
        self.assertIn("--upgrade", args)
        self.assertIsNone(updater.maybe_start())
        self.assertGreater(self.state.read({})["next_check"] - self.state.read({})["last_attempt"], 6 * 24 * 3600)

    async def test_failure_retries_tomorrow_not_next_round(self):
        updater = self._updater(pip=(1, "", "网络不通"))
        await updater.maybe_start()
        self.assertIsNone(updater.maybe_start())
        wait = self.state.read({})["next_check"] - self.state.read({})["last_attempt"]
        self.assertLess(wait, 2 * 24 * 3600)

    async def test_cancel_stops_a_running_update(self):
        updater = self._updater()
        task = updater.maybe_start()
        updater.cancel()
        await asyncio.wait({task})
        self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main()
