"""找主人说：回复评论、私信时，特别有感觉就顺手在回复 JSON 里填 ``tell_owner``，
这里决定发不发、发到主人的一个 QQ 窗口。不另外调模型，回复时顺带判断。

发到一个指定窗口（``TELL_OWNER_QQ_UMO``，留空用 ``OWNER_QQ_UMO``），语气跟着情绪走。
把关：每天最多 ``TELL_OWNER_DAILY_LIMIT`` 次（默认 2）、两次隔 ``TELL_OWNER_GAP_HOURS`` 小时（默认 3）、
同一个人一天一次；当天心情低（有点安静、懒得废话）时吐槽一天最多一次；主人自己的消息不算。
发过的记在 ``tell_owner.json``，重启不忘。
"""
from __future__ import annotations

import time
from datetime import datetime

from astrbot.api import logger
from ..infra.host_view import HostView
from ..storage.json_file import JsonFile

LOW_MOODS = {"🌧️ 有点安静", "🧊 懒得废话"}
CHANNEL_NAMES = {"comment": "评论", "private": "私信"}


class TellOwner(HostView):
    HOST_API = frozenset({"context", "mood", "roles", "settings"})

    def __init__(self, host, file: JsonFile | None = None):
        super().__init__(host)
        self._file = file if file is not None else host.states.tell_owner

    def _today_sent(self, today):
        data = self._file.read({})
        if not isinstance(data, dict) or data.get("date") != today:
            return []
        return [item for item in data.get("sent") or [] if isinstance(item, dict)]

    def _why_not(self, note, uid, now):
        """不发的原因；能发返回空字符串。"""
        if not self.settings.get("ENABLE_TELL_OWNER"):
            return "off"
        if self.roles.is_owner(uid):
            return "owner"
        sent = self._today_sent(datetime.fromtimestamp(now).strftime("%Y-%m-%d"))
        limit = max(0, self.settings.int("TELL_OWNER_DAILY_LIMIT", minimum=0))
        if note["mood"] == "吐槽" and self.mood.today()[0] in LOW_MOODS:
            limit = min(limit, 1)
            sent_count = sum(1 for item in sent if item.get("mood") == "吐槽")
        else:
            sent_count = len(sent)
        if sent_count >= limit:
            return "daily_limit"
        if any(str(item.get("uid")) == str(uid) for item in sent):
            return "same_person"
        gap = max(0, self.settings.int("TELL_OWNER_GAP_HOURS", minimum=0)) * 3600
        if sent and now - max(float(item.get("ts") or 0) for item in sent) < gap:
            return "too_soon"
        return ""

    async def maybe_tell(self, note, *, uid, username, content, channel, now=None):
        """``note`` 是回复 JSON 里的 ``tell_owner``（null 就什么都不做）。返回发没发出去。"""
        if not note:
            return False
        now = time.time() if now is None else now
        reason = self._why_not(note, uid, now)
        if reason:
            if reason not in {"off", "owner"}:
                logger.info(f"[BiliBot] 想找主人说（{note['mood']}）但这次不发：{reason}")
            return False
        umo = str(self.settings.get("TELL_OWNER_QQ_UMO") or self.settings.get("OWNER_QQ_UMO") or "").strip()
        if not umo:
            logger.info("[BiliBot] 想找主人说，但没填 QQ 窗口（TELL_OWNER_QQ_UMO / OWNER_QQ_UMO），不发")
            return False
        snippet = str(content or "").strip().replace("\n", " ")
        snippet = snippet[:80] + ("…" if len(snippet) > 80 else "")
        message = f"{note['text']}\n——{username}（UID {uid}）在B站{CHANNEL_NAMES.get(channel, '')}里说：「{snippet}」"
        try:
            from astrbot.api.event import MessageChain
            await self.context.send_message(umo, MessageChain().message(message))
        except Exception as exc:
            logger.warning(f"[BiliBot] 找主人说没发出去: {exc}")
            return False
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

        def add(data):
            sent = data.get("sent") if isinstance(data, dict) and data.get("date") == today else []
            return {"date": today, "sent": [*sent, {"ts": now, "uid": str(uid), "mood": note["mood"]}]}

        self._file.update(add, {})
        logger.info(f"[BiliBot] 💌 找主人说（{note['mood']}）：{note['text'][:30]}")
        return True
