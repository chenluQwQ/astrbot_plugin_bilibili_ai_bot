"""「今天」页：某一天看过的视频、发过的评论、发的动态、看的番剧；唯一的写操作是删自己的评论。

都从活动日志按日期查（数据库里留 30 天）。评论另外查运行库的动作表：
QQ 工具发的评论只记在那里，发出去不知道成没成的也只在那里。
能删的评论是发的时候记下了 rpid 的（``own_comments`` 表）；更早的没记，删不了。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request
from ..app import app_of
from ..runtime import ActionRequest, EventPriority
from ._common import _failure, _response

# 动作表里算「发评论」的几种：回复别人、看片时评论、QQ 工具评论
COMMENT_ACTION_KINDS = ("comment_reply", "proactive_comment", "tool_comment")

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _clock(stamp: str) -> str:
    return str(stamp or "")[11:16]


def _video(item: dict[str, Any]) -> dict[str, Any]:
    failed = item.get("evaluation_status") == "failed"
    return {
        "time": _clock(item.get("time")),
        "bvid": str(item.get("bvid") or ""),
        "title": str(item.get("title") or ""),
        "up_name": str(item.get("up_name") or ""),
        "score": None if failed else item.get("score"),
        "score_reason": str(item.get("score_reason") or ""),
        "mood": "" if failed else str(item.get("mood") or ""),
        "review": str(item.get("review") or ""),
        "actions": [str(action) for action in item.get("actions") or []],
        "tname": str(item.get("tname") or ""),
        "failed": failed,
    }


def _reply(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": _clock(item.get("time")),
        "kind": "reply",
        "text": str(item.get("reply") or ""),
        "to_user": str(item.get("username") or ""),
        "to_text": str(item.get("content") or ""),
        "bvid": str(item.get("bvid") or ""),
        "title": str(item.get("video_title") or ""),
        "result": "sent",
        "rpid": str(item.get("own_rpid") or ""),
    }


def _video_comment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": _clock(item.get("time")),
        "kind": "bangumi" if item.get("type") == "bangumi" else "video",
        "text": str(item.get("comment") or ""),
        "to_user": "",
        "to_text": "",
        "bvid": str(item.get("bvid") or ""),
        "title": str(item.get("title") or ""),
        "result": "sent",
        "rpid": str(item.get("own_rpid") or ""),
    }


def _action_comment(row: dict[str, Any]) -> dict[str, Any]:
    try:
        meta = json.loads(row.get("digest") or "{}")
    except (TypeError, ValueError):
        meta = {}  # 存的时候截到 500 字，太长的会截坏
    meta = meta if isinstance(meta, dict) else {}
    kind = {"comment_reply": "reply", "tool_comment": "tool"}.get(str(row.get("kind")), "video")
    target = str(row.get("target_id") or "")
    return {
        "time": datetime.fromtimestamp(float(row.get("created_at") or 0)).strftime("%H:%M"),
        "kind": kind,
        "text": str(meta.get("text") or ""),
        "to_user": "",
        "to_text": "",
        "bvid": target if target.startswith("BV") else "",
        "title": str(meta.get("title") or ""),
        "result": "unknown" if row.get("state") == "unknown" else "sent",
        "rpid": "",
        "action_key": str(row.get("key") or ""),
    }


async def _comments(plugin: Any, day: str, replies: list, proactive: list) -> list[dict[str, Any]]:
    comments = [_reply(item) for item in replies if item.get("channel") not in {"private", "live"}]
    comments += [_video_comment(item) for item in proactive]
    layered = app_of(plugin).layered_runtime
    if layered is not None and layered.is_open:
        start = datetime.strptime(day, "%Y-%m-%d")
        try:
            rows = await layered.actions_between(
                COMMENT_ACTION_KINDS, start.timestamp(), (start + timedelta(days=1)).timestamp()
            )
        except Exception as exc:
            logger.warning(f"[BiliBot WebUI] 读评论动作失败: {exc}")
            rows = []
        # 发成了的回复和看片评论日志里已经有了；QQ 工具的只在动作表里
        comments += [
            _action_comment(row) for row in rows
            if row.get("state") == "unknown" or (row.get("kind") == "tool_comment" and row.get("state") == "succeeded")
        ]
        await _mark_deletable(layered, comments)
    for item in comments:
        item.pop("action_key", None)
        item.setdefault("deletable", False)
        item.setdefault("delete_state", "")
    comments.sort(key=lambda item: item["time"])
    return comments


async def _mark_deletable(layered: Any, comments: list[dict[str, Any]]) -> None:
    """对上 ``own_comments``：有行的才能删；``delete_state`` 是上次删的结果，删成了是 ``succeeded``。"""
    try:
        own = await layered.own_comments(
            rpids=[item["rpid"] for item in comments],
            action_keys=[item.get("action_key", "") for item in comments],
        )
    except Exception as exc:
        logger.warning(f"[BiliBot WebUI] 读自己的评论失败: {exc}")
        return
    by_key = {row["action_key"]: row for row in own.values() if row.get("action_key")}
    for item in comments:
        row = own.get(item["rpid"]) or by_key.get(item.get("action_key", ""))
        if row is None:
            continue
        item["rpid"] = str(row["rpid"])
        item["deletable"] = True
        item["delete_state"] = "succeeded" if row.get("deleted_at") else str(row.get("delete_state") or "")


def _dynamic(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": _clock(item.get("time")),
        "text": str(item.get("text") or ""),
        "has_image": bool(item.get("has_image")),
        "kind": "comment_sync" if item.get("comment_sync") else "repost" if item.get("repost") else str(item.get("kind") or ""),
        "bvid": str(item.get("bvid") or ""),
        "title": str(item.get("video_title") or ""),
    }


def _bangumi(item: dict[str, Any]) -> dict[str, Any]:
    failed = item.get("evaluation_status") == "failed"
    return {
        "time": _clock(item.get("time")),
        "title": str(item.get("title") or ""),
        "season_id": str(item.get("season_id") or ""),
        "ep_index": str(item.get("ep_index") or ""),
        "ep_title": str(item.get("ep_title") or ""),
        "score": None if failed else item.get("score"),
        "mood": "" if failed else str(item.get("mood") or ""),
        "review": str(item.get("review") or ""),
        "comment": "" if failed else str(item.get("comment") or ""),
        "failed": failed,
        "fail_reason": str(item.get("fail_reason") or "") if failed else "",
    }


async def handle_get_today(plugin: Any):
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        day = str(request.query.get("date", "") or "").strip() or today
        if not _DATE.fullmatch(day):
            return _failure("日期格式应为 YYYY-MM-DD")
        logs = app_of(plugin).activity_logs
        shown = (logs.watch, logs.reply, logs.proactive, logs.dynamic, logs.bangumi)
        return _response({
            "date": day,
            "today": today,
            "days": sorted(set().union(*(log.days() for log in shown)) | {today}, reverse=True),
            "videos": [_video(item) for item in logs.watch.on_day(day)],
            "comments": await _comments(plugin, day, logs.reply.on_day(day), logs.proactive.on_day(day)),
            "dynamics": [_dynamic(item) for item in logs.dynamic.on_day(day)],
            "bangumi": [_bangumi(item) for item in logs.bangumi.on_day(day)],
        })
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] today failed: {exc}")
        return _failure(str(exc), 500)


async def handle_delete_comment(plugin: Any):
    """删一条自己发的评论。走统一行为调度（算进全局每分钟、每天的次数）；
    结果未知不重试，页面让人去B站看。删成了只在 ``own_comments`` 标已删除。"""
    try:
        body = await request.json(default={})
        rpid = str((body or {}).get("rpid") or "").strip()
        if not rpid.isdigit():
            return _failure("rpid 无效")
        app = app_of(plugin)
        layered = app.layered_runtime
        if layered is None or not layered.is_open:
            return _failure("运行库没打开，删不了", 503)
        row = (await layered.own_comments(rpids=[rpid])).get(rpid)
        if row is None:
            return _failure("没找到这条评论的记录（更早发的没记编号，删不了）", 404)
        if row.get("deleted_at"):
            return _response({"rpid": rpid, "delete_state": "succeeded"}, "已经删过了")
        reason = {"text": ""}

        async def delete():
            ok, reason["text"] = await app.delete_comment(int(row["oid"]), int(row["type"]), int(rpid))
            return ok

        outcome = await app.event_runtime.execute(
            ActionRequest(
                key=f"comment_delete:{rpid}",
                kind="comment_delete",
                event_key=f"bilibili:webui:comment_delete:{rpid}",
                target_id=rpid,
                priority=EventPriority.DIRECT_MENTION,
                metadata={"webui": True, "oid": str(row["oid"]), "type": int(row["type"])},
            ),
            delete,
        )
        if outcome.success:
            await layered.mark_own_comment_deleted(rpid, "succeeded")
            return _response({"rpid": rpid, "delete_state": "succeeded"},
                             "B站说已经删过了" if reason["text"] == "already" else "已删除")
        if outcome.state == "unknown":
            await layered.mark_own_comment_deleted(rpid, "unknown")
            return _response({"rpid": rpid, "delete_state": "unknown"}, "结果未知，请到 B站确认")
        if outcome.state in {"queued", "running"}:
            return _response({"rpid": rpid, "delete_state": ""}, "已经在删了，等一下再刷新")
        if str(outcome.reason).startswith("budget_exhausted:"):
            return _failure("今天对外动作的次数用完了，没有删", 429)
        await layered.mark_own_comment_deleted(rpid, "failed")
        why = {"cookie": "Cookie 失效了", "risk": "被B站限制了（可能是风控）", "error": "请求出错"}.get(
            reason["text"], reason["text"] or str(outcome.reason))
        return _failure(f"删除失败：{why}", 502)
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] delete comment failed: {exc}")
        return _failure(str(exc), 500)
