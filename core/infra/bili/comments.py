"""评论：发回复、发评论、删自己的评论、读热评。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from astrbot.api import logger

from ...config import (
    BILI_REPLY_URL,
)
from ...runtime import WriteResultUnknownError
from .base import BiliApi

BILI_REPLY_DEL_URL = "https://api.bilibili.com/x/v2/reply/del"


@dataclass(frozen=True)
class PostedComment:
    """发成了的一条评论。真值恒为真，照旧当「发成了」用；带着删它要的三样（WebUI 删评论）。"""
    oid: int
    type: int
    rpid: int
    text: str = ""

    def __bool__(self) -> bool:
        return True


def _posted(d, oid, reply_type, text):
    """B站回的 ``data.rpid``；没给（接口改了、假对象）就退回 ``True``。"""
    try:
        rpid = int((d.get("data") or {}).get("rpid") or 0)
        return PostedComment(int(oid), int(reply_type), rpid, text) if rpid > 0 else True
    except (TypeError, ValueError, AttributeError):
        return True


class BiliComments(BiliApi):
    async def _send_reply(self, oid, rpid, reply_type, content):
        msg = (content or "").strip()
        # B站不接受空内容 / 纯标点 / 纯符号（会报「不可发送单个标点符号」），
        # 至少要含一个文字或数字，否则提前拦掉，省一次必然失败的请求
        if not msg or not re.search(r'[0-9A-Za-z一-鿿]', msg):
            logger.warning(f"[BiliBot] 回复内容无效（空/纯标点），跳过发送：{content!r}")
            return False
        try:
            d, _ = await self._http_post(
                BILI_REPLY_URL,
                data={"oid": oid, "type": reply_type, "root": rpid, "parent": rpid, "message": msg, "csrf": self.settings.get("BILI_JCT")},
            )
            if d["code"] == 0:
                return _posted(d, oid, reply_type, msg)
            elif d["code"] == -101:
                logger.error("[BiliBot] SESSDATA 失效！")
            elif d["code"] == -111:
                logger.error("[BiliBot] bili_jct 错误！")
            else:
                logger.warning(f"[BiliBot] 回复失败: {d.get('message', d['code'])}")
            return False
        except WriteResultUnknownError:
            # 请求可能已经到达B站；交给统一运行时记录 unknown，禁止自动重试。
            raise
        except Exception as e:
            logger.error(f"[BiliBot] 回复出错: {e}")
            return False

    def _strip_at_prefix(self, content):
        content = (content or "").strip()
        content = re.sub(r'^@[^ \t\n\r]+\s*', '', content)
        return content.strip()

    async def _send_comment(self, oid, comment_text, oid_type=1, *, sync_to_dynamic=False):
        ok, _reason = await self._send_comment_detailed(oid, comment_text, oid_type, sync_to_dynamic=sync_to_dynamic)
        return ok

    async def _send_comment_detailed(self, oid, comment_text, oid_type=1, *, sync_to_dynamic=False):
        """发评论，返回 ``(成功, 原因)``；原因是 ``cookie`` / ``risk`` / ``closed`` / ``sensitive`` / ``error``，
        别的返回码是 ``code:<码> <说明>``。被风控只拦这一次，不重试也不停后面的（issue #62）。
        ``sync_to_dynamic`` 是B站网页上的「同时转发到我的动态」（PR #50 的思路）；关着时请求和原来一模一样。"""
        data = {"oid": oid, "type": oid_type, "message": comment_text, "csrf": self.settings.get("BILI_JCT")}
        if sync_to_dynamic:
            data["sync_to_dynamic"] = 1
        try:
            d, _ = await self._http_post(BILI_REPLY_URL, data=data)
            code = d.get("code", -1)
            if code == 0:
                return _posted(d, oid, oid_type, comment_text), ""
            if code == -101:
                logger.warning("[BiliBot] 发评论失败：Cookie 已失效")
                return False, "cookie"
            if code in (-352, -403, -412, 12015):
                logger.warning("[BiliBot] 发评论失败：访问被限制（可能触发风控）")
                return False, "risk"
            if code == 12002:
                logger.warning("[BiliBot] 发评论失败：评论区已关闭")
                return False, "closed"
            if code == 12025:
                logger.warning("[BiliBot] 发评论失败：评论内容包含敏感词")
                return False, "sensitive"
            logger.warning(f"[BiliBot] 发评论失败({oid}): code={code} {d.get('message', '')}")
            return False, f"code:{code} {str(d.get('message', ''))[:60]}".strip()
        except WriteResultUnknownError:
            # 请求可能已经到达B站；交给统一运行时记录 unknown，禁止自动重试。
            raise
        except Exception as e:
            logger.error(f"[BiliBot] 发送评论异常: {e}")
            return False, "error"

    async def _delete_comment(self, oid, reply_type, rpid):
        """删自己发的评论，返回 ``(成功, 原因)``。B站说已经删过了也算成功（原因 ``already``）。"""
        data = {"oid": oid, "type": reply_type, "rpid": rpid, "csrf": self.settings.get("BILI_JCT")}
        try:
            d, _ = await self._http_post(BILI_REPLY_DEL_URL, data=data)
            code = d.get("code", -1)
            if code == 0:
                return True, ""
            if code == 12022:
                return True, "already"
            if code == -101:
                logger.warning("[BiliBot] 删评论失败：Cookie 已失效")
                return False, "cookie"
            if code in (-352, -412, 12015):
                logger.warning("[BiliBot] 删评论失败：访问被限制（可能触发风控）")
                return False, "risk"
            logger.warning(f"[BiliBot] 删评论失败({oid}/{rpid}): code={code} {d.get('message', '')}")
            return False, f"code:{code} {str(d.get('message', ''))[:60]}".strip()
        except WriteResultUnknownError:
            raise
        except Exception as e:
            logger.error(f"[BiliBot] 删评论异常: {e}")
            return False, "error"

    async def _get_hot_comments(self, oid, limit=10):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/v2/reply/main", params={"oid": oid, "type": 1, "mode": 3, "ps": limit})
            if d["code"] == 0:
                replies = d.get("data", {}).get("replies", []) or []
                return [r.get("content", {}).get("message", "")[:100] for r in replies if r.get("content", {}).get("message")]
        except Exception:
            pass
        return []

    async def _get_comment_images(self, oid, rpid, comment_type):
        """获取评论中的图片 URL 列表"""
        try:
            d, _ = await self._http_get(
                "https://api.bilibili.com/x/v2/reply/detail",
                params={"oid": oid, "type": comment_type, "root": rpid},
            )
            if d["code"] != 0:
                return []
            content = d.get("data", {}).get("root", {}).get("content", {})
            pictures = content.get("pictures", [])
            return [p["img_src"] for p in pictures if "img_src" in p]
        except Exception:
            return []
