"""安全日志：注入尝试、恶意评论、拉黑等事件。活动日志的 ``security`` 那种只由这里读写。"""

from __future__ import annotations

from datetime import datetime

from ..storage.activity_log import ActivityLog


class SecurityLog:
    def __init__(self, log: ActivityLog):
        self._log = log

    def record(self, event_type: str, uid, username: str, content: str, detail: str) -> None:
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "type": event_type,
            "uid": str(uid),
            "username": username,
            "content": (content or "")[:200],
            "detail": detail,
        }
        self._log.append(entry)

    def entries(self) -> list[dict]:
        return self._log.recent()
