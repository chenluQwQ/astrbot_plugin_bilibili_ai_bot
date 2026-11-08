"""问题日志只收本插件的警告和错误，堆栈一起写，摘掉后不再写。"""
import logging
import tempfile
import unittest
from pathlib import Path

from core.infra import problem_log


class ProblemLogTests(unittest.TestCase):
    def test_keeps_only_bilibot_warnings_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "logs" / "problems.log")
            logger = logging.getLogger("problem-log-test")
            logger.setLevel(logging.DEBUG)
            handler = problem_log.attach(logger, path)
            logger.info("[BiliBot] 普通信息")
            logger.warning("[OtherPlugin] 别人的警告")
            logger.warning("[BiliBot] Cookie 失效")
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                logger.exception("[BiliBot] 主循环出错")
            problem_log.detach(logger, handler)
            logger.error("[BiliBot] 摘掉之后")
            text = Path(path).read_text(encoding="utf-8")
        self.assertIn("Cookie 失效", text)
        self.assertIn("RuntimeError: boom", text)
        self.assertNotIn("普通信息", text)
        self.assertNotIn("OtherPlugin", text)
        self.assertNotIn("摘掉之后", text)


if __name__ == "__main__":
    unittest.main()
