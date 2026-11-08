"""BiliBot 自己的问题日志：AstrBot 日志里带 ``[BiliBot`` 的警告和错误另存一份到数据目录。

AstrBot 的日志混着所有插件，查问题时翻起来累；这里只留本插件的，报错堆栈也一起写。
文件满了自动轮换（默认 2 MB × 3 份），不会越写越大。
"""

import logging
import os
from logging.handlers import RotatingFileHandler


class _BiliBotOnly(logging.Filter):
    def filter(self, record):
        try:
            return "[BiliBot" in record.getMessage()
        except Exception:
            return False


def attach(logger, path, *, level=logging.WARNING, max_bytes=2_000_000, backups=3):
    """给 ``logger`` 加一个只记 BiliBot 问题的文件输出，返回它（停用时交给 ``detach``）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setLevel(level)
    handler.addFilter(_BiliBotOnly())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return handler


def detach(logger, handler):
    if handler is None:
        return
    logger.removeHandler(handler)
    handler.close()
