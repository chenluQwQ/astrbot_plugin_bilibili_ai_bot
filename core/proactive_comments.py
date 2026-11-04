"""主动评论额度和评论区主人通知；热评、私信、QQ 保持独立。"""
import asyncio
import os
from datetime import datetime

from astrbot.api import logger
from .config import OWNER_RECOMMEND_LOG_FILE, PROACTIVE_LOG_FILE


OWNER_COMMENT_NOTIFY_LOG_FILE = os.path.join(
    os.path.dirname(OWNER_RECOMMEND_LOG_FILE), "owner_comment_notify_log.json"
)


def today_primary_comment_count(watch_log, proactive_log=None):
    """同一视频只发一条主动评论，两份日志及配图留存不能重复占额度。"""
    today = datetime.now().strftime("%Y-%m-%d")
    actions = set()
    for records, is_watch in ((proactive_log, False), (watch_log, True)):
        for item in records if isinstance(records, list) else []:
            if not isinstance(item, dict):
                continue
            time_text = str(item.get("time") or "")
            if not time_text.startswith(today):
                continue
            if is_watch:
                if "💬评论" not in (item.get("actions") or []):
                    continue
            elif (
                item.get("send_status") not in (None, "", "published")
                or item.get("type") not in (None, "", "proactive", "special_follow", "bangumi")
            ):
                continue
            bvid = str(item.get("bvid") or "")
            # 旧番剧记录无 BV 号；按分钟和标题兼容旧格式。
            identity = bvid or f"{time_text[:16]}|{item.get('title') or 'unknown'}"
            actions.add(identity)
    return len(actions)


class ProactiveCommentMixin:
    def _owner_comment_notification_state(self, bvid):
        """只去重当前主人的真实 @；兼容历史成功主动评论，未知结果也保留。"""
        owner_mid = str(self.config.get("OWNER_MID", "") or "").strip()
        if not owner_mid.isdigit():
            return ""
        key = f"{owner_mid}:{bvid}"
        ledger = self._load_json(OWNER_COMMENT_NOTIFY_LOG_FILE, {})
        ledger = ledger if isinstance(ledger, dict) else {}
        record = ledger.get(key)
        if isinstance(record, dict) and record.get("state") in {"published", "unknown"}:
            return record["state"]
        owner_name = str(self.config.get("OWNER_BILI_NAME", "") or "").strip()
        history = self._load_json(PROACTIVE_LOG_FILE, [])
        for item in history if isinstance(history, list) else []:
            if not isinstance(item, dict) or str(item.get("bvid") or "") != str(bvid):
                continue
            if item.get("type") not in (None, "", "proactive", "special_follow"):
                continue
            if "owner_mention_mid" in item:
                mentioned = str(item["owner_mention_mid"]) == owner_mid
            else:
                # 旧版主动评论只会在昵称和数字 UID 都有效时加此前缀。
                mentioned = bool(owner_name) and str(item.get("comment") or "").startswith(f"@{owner_name} ")
            if not mentioned or item.get("send_status") == "failed":
                continue
            state = "unknown" if item.get("send_status") == "unknown" else "published"
            ledger[key] = {"state": state, "time": item.get("time", ""), "rpid": item.get("rpid", "")}
            self._save_json(OWNER_COMMENT_NOTIFY_LOG_FILE, ledger)
            return state
        return ""

    async def _send_proactive_comment_once(
        self, oid, text, *, bvid, title, result_sink, recommendation=False
    ):
        """主评论可照常发送，评论区 @ 每个视频/主人最多一次。

        先持久化 unknown 再发送；断电、超时、重载后禁止盲目补发 @。
        两条发送路径共用锁，避免同时通过检查。明确失败才释放提醒额度。
        """
        lock = getattr(self, "_owner_comment_notify_lock", None)
        if lock is None:
            lock = self._owner_comment_notify_lock = asyncio.Lock()
        async with lock:
            state = self._owner_comment_notification_state(bvid)
            owner_mid = str(self.config.get("OWNER_MID", "") or "").strip()
            owner_name = str(self.config.get("OWNER_BILI_NAME", "") or "").strip()
            if recommendation and state:
                result_sink.update(notification_deduplicated=state, owner_mention_mid="")
                logger.info(f"[BiliBot] 跳过重复评论区主人通知：bvid={bvid} state={state}")
                return False
            if recommendation:
                final_text = f"@{owner_name} {text}" if owner_name else text
                at_names = {owner_name: owner_mid} if owner_name and owner_mid.isdigit() else None
            elif state:
                final_text, at_names = text, None
            else:
                final_text, at_names = self._owner_comment_with_at(text)
            # 昵称过长导致正文被截断时，没有完整 @ 前缀就不占提醒额度。
            if at_names and not final_text.startswith(f"@{owner_name} "):
                at_names = None
            mentioned_mid = owner_mid if at_names else ""
            key = f"{owner_mid}:{bvid}"
            ledger = self._load_json(OWNER_COMMENT_NOTIFY_LOG_FILE, {})
            ledger = ledger if isinstance(ledger, dict) else {}
            if mentioned_mid:
                ledger[key] = {"state": "unknown", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                self._save_json(OWNER_COMMENT_NOTIFY_LOG_FILE, ledger)
            try:
                if recommendation:
                    ok = await self._send_comment(oid, final_text, at_names=at_names, result_sink=result_sink)
                else:
                    ok = await self._send_gallery_comment(
                        oid, final_text, bvid=bvid, title=title,
                        at_names=at_names, result_sink=result_sink,
                    )
                if not ok and result_sink.get("send_status") == "unknown":
                    raise TimeoutError("owner_comment_send_result_unknown")
            finally:
                result_sink.update(comment=final_text, owner_mention_mid=mentioned_mid)
            if mentioned_mid:
                ledger[key].update(
                    state="published" if ok else "failed", rpid=result_sink.get("rpid", "")
                )
                try:
                    self._save_json(OWNER_COMMENT_NOTIFY_LOG_FILE, ledger)
                except Exception as exc:
                    # 实际发布不能因后续记账失败被当成可重试；预存 unknown 仍能阻止重复 @。
                    logger.error(f"[BiliBot] 主人通知凭据待核对：{type(exc).__name__}")
            return ok
