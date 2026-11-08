"""分享后偶尔问一句喜不喜欢。

在主人 QQ 私聊里推荐视频成功后，按 ``ASK_OWNER_FEEDBACK_CHANCE``（默认 0.2）、每天最多一次，
用那个会话当前的模型和人设补问一句；模型觉得不合适可以不问（严格 JSON，和分享反应同一套格式）。
主人回的话照常由 AstrBot 那边的对话处理；QQ 工具 ``bili_owner_feedback`` 调 ``record`` 记下喜不喜欢，
写进主人画像的口味（公开评论那份，和每周收藏夹画像同一份）。问过哪个视频记在 ``owner_feedback.json``，2 天内有效。
"""
from __future__ import annotations

import random
import time
from datetime import datetime

from astrbot.api import logger
from ..content_protocol import ContentProtocolError, SHARE_REACTION_SCHEMA_PROMPT, parse_share_reaction
from ..infra.host_view import HostView
from ..security.redact import wrap_untrusted
from ..storage.json_file import JsonFile

PENDING_SECONDS = 2 * 86400
SCOPE = "bili_comment"


class OwnerFeedback(HostView):
    HOST_API = frozenset({"models", "qq_chat", "settings", "user_profiles"})

    def __init__(self, host, file: JsonFile | None = None, rng=random.random):
        super().__init__(host)
        self._file = file if file is not None else host.states.owner_feedback
        self._rng = rng

    def _state(self):
        data = self._file.read({})
        return data if isinstance(data, dict) else {}

    async def maybe_ask(self, bvid, title, rec_text, now=None):
        """QQ 推荐发出去之后调。返回问没问。"""
        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        chance = self.settings.float("ASK_OWNER_FEEDBACK_CHANCE", minimum=0.0, maximum=1.0)
        if chance <= 0 or self._state().get("asked_on") == today or self._rng() >= chance:
            return False
        umo = str(self.settings.get("OWNER_QQ_UMO") or "").strip()
        if not umo:
            return False
        try:
            provider_id = await self.qq_chat._qq_session_provider_id(umo)
            if not provider_id:
                return False
            persona = await self.qq_chat._qq_session_persona_prompt(umo)
            prompt = (
                "你刚在QQ私聊里把一个B站视频分享给了对方，想顺口问一句对方喜不喜欢、觉得怎么样。\n"
                f"{wrap_untrusted(f'视频：《{title[:80]}》；你分享时说的：{rec_text[:200]}', 'shared_video')}\n"
                "像平时聊天一样只说一句，不要重复分享时说过的话，不带链接；觉得这时候问不合适就skip。\n"
                + SHARE_REACTION_SCHEMA_PROMPT
            )
            raw = await self.models._llm_call(prompt, system_prompt=persona, max_tokens=80, provider_id=provider_id)
            parsed = parse_share_reaction(raw) if raw else None
        except ContentProtocolError as exc:
            logger.info(f"[BiliBot] 分享后追问格式不对，这次不问: {exc}")
            return False
        except Exception as exc:
            logger.warning(f"[BiliBot] 分享后追问没生成出来: {exc}")
            return False
        if not parsed or parsed["decision"] != "comment":
            return False
        try:
            await self.qq_chat._send_share_reaction(umo, parsed["text"], None)
        except Exception as exc:
            logger.warning(f"[BiliBot] 分享后追问没发出去: {exc}")
            return False
        self._file.write({"asked_on": today, "pending": {"bvid": bvid, "title": title[:80], "ts": now}})
        logger.info(f"[BiliBot] 💬 分享后问了主人一句：{parsed['text'][:30]}")
        return True

    def record(self, liked, points, bvid="", now=None):
        """主人说了喜不喜欢：喜欢的点加进口味标签；不喜欢的去掉对应标签、记一条「不太喜欢」。返回给模型的话。"""
        now = time.time() if now is None else now
        owner = str(self.settings.get("OWNER_MID") or "").strip()
        if not owner:
            return "没配置主人的B站 UID，记不下来。"
        pending = self._state().get("pending") or {}
        fresh = pending if now - float(pending.get("ts") or 0) <= PENDING_SECONDS else {}
        target = str(bvid or "").strip() or fresh.get("bvid", "")
        if not target:
            return "最近没有在等主人反馈的视频；要记的话请带上 BV 号。"
        points = [str(p).strip()[:12] for p in points or [] if str(p).strip()][:3]
        title = fresh.get("title", "") if fresh.get("bvid") == target else ""
        if liked:
            if points:
                self.user_profiles.update(owner, new_tags=points, source_scope=SCOPE)
        else:
            if points:
                self.user_profiles.drop_tags(owner, points)
            what = "、".join(points) or (f"《{title}》这类视频" if title else "")
            if what:
                self.user_profiles.update(owner, new_facts=[f"不太喜欢{what}"], source_scope=SCOPE)
        if fresh.get("bvid") == target:
            self._file.write({**self._state(), "pending": {}})
        logger.info(f"[BiliBot] 📝 记下主人对 {target} 的反馈：{'喜欢' if liked else '不太喜欢'} {points}")
        return f"记下了：主人{'喜欢' if liked else '不太喜欢'}{('（' + '、'.join(points) + '）') if points else ''}。"
