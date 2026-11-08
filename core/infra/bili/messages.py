"""私信：写操作不自动重试，避免重复发送。"""
from __future__ import annotations

import json
import time
import uuid

from astrbot.api import logger

from ...config import (
    BILI_PRIVATE_MESSAGES_URL,
    BILI_PRIVATE_MSG_SEND_URL,
    BILI_PRIVATE_SESSIONS_URL,
)
from ...runtime import WriteResultUnknownError
from .base import BiliApi


class BiliMessages(BiliApi):
    async def _send_bili_private_payload(self, receiver_mid, msg_type, content, label="私信"):
        """发送 B站网页私信载荷；写操作不自动重试，避免重复发送。"""
        sender_mid = str(self.settings.get("DEDE_USER_ID") or "").strip()
        receiver_mid = str(receiver_mid or "").strip()
        csrf = str(self.settings.get("BILI_JCT") or "").strip()
        if not self._has_cookie() or not csrf or not sender_mid.isdigit():
            logger.warning(f"[BiliBot] 发送B站{label}失败：登录 Cookie、CSRF 或 Bot UID 不完整")
            return False
        if not receiver_mid.isdigit():
            logger.warning(f"[BiliBot] 发送B站{label}失败：接收 UID 未填写或格式错误")
            return False
        if sender_mid == receiver_mid:
            logger.warning(f"[BiliBot] 发送B站{label}失败：接收 UID 与 Bot UID 相同")
            return False
        if not isinstance(content, dict) or not content:
            logger.warning(f"[BiliBot] 发送B站{label}失败：消息内容为空")
            return False
        payload = {
            "msg[sender_uid]": sender_mid,
            "msg[receiver_id]": receiver_mid,
            "msg[receiver_type]": 1,
            "msg[msg_type]": int(msg_type),
            "msg[msg_status]": 0,
            "msg[dev_id]": str(uuid.uuid4()).upper(),
            "msg[timestamp]": int(time.time()),
            "msg[new_face_version]": 0,
            "msg[content]": json.dumps(
                content,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "csrf": csrf,
            "csrf_token": csrf,
            "from_firework": 0,
            "build": 0,
            "mobi_app": "web",
        }
        headers = {
            **self._headers(),
            "Origin": "https://message.bilibili.com",
            "Referer": "https://message.bilibili.com/",
        }
        try:
            result, _ = await self._http_post(
                BILI_PRIVATE_MSG_SEND_URL,
                headers=headers,
                data=payload,
                retries=0,
            )
            code = result.get("code", -1)
            if code == 0:
                return True
            logger.warning(
                f"[BiliBot] 发送B站{label}失败({receiver_mid}): "
                f"code={code} {result.get('message', '')}"
            )
            return False
        except WriteResultUnknownError:
            # 请求可能已经到达B站；交给统一运行时记录 unknown，禁止自动重试。
            raise
        except Exception as e:
            logger.warning(f"[BiliBot] 发送B站{label}异常({receiver_mid}): {e}")
            return False

    async def _send_bili_private_message(self, receiver_mid, text):
        """通过 B站网页私信发送纯文本；写操作不自动重试，避免重复发送。"""
        message = str(text or "").strip()
        if not message:
            logger.warning("[BiliBot] 发送B站私信失败：消息为空")
            return False
        return await self._send_bili_private_payload(
            receiver_mid,
            1,
            {"content": message},
        )

    async def _send_bili_private_video_share(self, receiver_mid, video_info):
        """通过 B站网页私信发送原生视频分享卡片。"""
        info = video_info if isinstance(video_info, dict) else {}
        bvid = str(info.get("bvid", "") or "").strip()
        aid = info.get("aid") or info.get("oid") or info.get("id")
        try:
            aid = int(aid)
        except (TypeError, ValueError):
            aid = 0
        if not bvid or aid <= 0:
            logger.warning("[BiliBot] 发送B站视频私信失败：视频 BV 号或 aid 无效")
            return False
        content = {
            "author": str(info.get("owner_name", "") or ""),
            "headline": "",
            "id": aid,
            "source": 5,
            "thumb": str(info.get("pic", "") or ""),
            "title": str(info.get("title", "") or bvid),
            "bvid": bvid,
        }
        return await self._send_bili_private_payload(
            receiver_mid,
            7,
            content,
            label="视频私信",
        )

    def _private_message_headers(self):
        return {
            **self._headers(),
            "Referer": "https://message.bilibili.com/",
            "Origin": "https://message.bilibili.com",
        }

    async def _get_private_sessions(self):
        data, _ = await self._http_get(
            BILI_PRIVATE_SESSIONS_URL,
            headers=self._private_message_headers(),
            params={
                "session_type": 1,
                "group_fold": 1,
                "unfollow_fold": 0,
                "sort_rule": 2,
                "size": 100,
                "build": 0,
                "mobi_app": "web",
            },
        )
        if data.get("code") != 0:
            raise RuntimeError(
                f"获取私信会话失败: code={data.get('code')} {data.get('message', '')}"
            )
        return list((data.get("data") or {}).get("session_list") or [])

    async def _fetch_private_session_messages(self, talker_id, session_type, begin_seqno):
        data, _ = await self._http_get(
            BILI_PRIVATE_MESSAGES_URL,
            headers=self._private_message_headers(),
            params={
                "talker_id": talker_id,
                "session_type": session_type,
                "begin_seqno": begin_seqno,
                "size": 20,
                "sender_device_id": 1,
                "build": 0,
                "mobi_app": "web",
            },
        )
        if data.get("code") != 0:
            raise RuntimeError(
                f"获取私信内容失败: code={data.get('code')} {data.get('message', '')}"
            )
        return data.get("data") or {}
