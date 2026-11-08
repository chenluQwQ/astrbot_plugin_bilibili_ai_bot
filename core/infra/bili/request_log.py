"""最近发出的 B站请求（时间、方法、路径），被频控时用来看那几分钟里谁在请求。"""
from __future__ import annotations

import time
from collections import Counter, deque
from urllib.parse import urlsplit

_RECENT_REQUESTS = deque(maxlen=1000)


def note_request(method, url):
    _RECENT_REQUESTS.append((time.monotonic(), method, urlsplit(str(url)).path))


def recent_request_counts(window=300.0):
    """最近 ``window`` 秒内每个「方法 路径」发了几次，含重试。"""
    since = time.monotonic() - window
    return Counter(
        f"{method} {path}" for at, method, path in _RECENT_REQUESTS if at >= since
    )
