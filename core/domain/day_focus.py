"""每日计划里「这段看什么」：模型给的重点在这里按规矩过一遍。

主动看片的一段可以带一个重点：备忘（memo，视频或 UP 的备忘编号）、关注（follow）、话题（topic）、池子（pool，
只能是管理员配置过的）。追番的一个时刻可以带一条番剧备忘。不合格的重点只去掉，时间照样留着。

不要太单一：同一个重点一天最多一段；当天有两段以上时至少留一段不带重点。
"""
from __future__ import annotations

import re

FOCUS_KINDS = ("memo", "follow", "topic", "pool")
TOPIC_LIMIT = 20
DEFAULT_MEMO_CAP = 3


def _same_key(focus):
    """「同一个重点」怎么算：备忘按编号，话题按去掉空白小写，池子按名字，关注只有一个。"""
    kind, detail = focus["focus"], focus.get("detail", "")
    if kind == "topic":
        return kind, re.sub(r"\s+", "", str(detail)).lower()
    return kind, str(detail)


def check_window_focus(raw, *, memos, pools, used, memo_count, memo_cap):
    """一段主动看片的重点 → 合格的 ``{"focus":…, "detail":…}`` 或 None。

    ``memos``：待办的备忘 {编号: 备忘}；``used``：今天已经用过的重点；``memo_count``：[今天已排几条备忘]。
    """
    if not isinstance(raw, dict):
        return None
    kind = raw.get("focus")
    detail = raw.get("detail", "")
    if kind not in FOCUS_KINDS:
        return None
    if kind == "memo":
        memo = memos.get(detail) if isinstance(detail, int) and not isinstance(detail, bool) else None
        if memo is None or memo["kind"] not in ("video", "up") or memo_count[0] >= memo_cap:
            return None
        focus = {"focus": "memo", "detail": detail}
    elif kind == "follow":
        focus = {"focus": "follow", "detail": ""}
    elif kind == "topic":
        detail = str(detail or "").strip() if isinstance(detail, str) else ""
        if not detail or len(detail) > TOPIC_LIMIT:
            return None
        focus = {"focus": "topic", "detail": detail}
    else:
        if not isinstance(detail, str) or detail.strip() not in pools:
            return None
        focus = {"focus": "pool", "detail": detail.strip()}
    key = _same_key(focus)
    if key in used:
        return None
    used.add(key)
    if kind == "memo":
        memo_count[0] += 1
    return focus


def check_bangumi_memo(raw, *, memos, used, memo_count, memo_cap):
    """追番时刻带的备忘编号 → 合格的编号或 None。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    memo = memos.get(raw)
    if memo is None or memo["kind"] != "bangumi" or memo_count[0] >= memo_cap or ("memo", str(raw)) in used:
        return None
    used.add(("memo", str(raw)))
    memo_count[0] += 1
    return raw


def leave_one_unfocused(focuses):
    """两段以上全都带了重点：去掉最后一个不是备忘的重点（都是备忘就去掉最后一个）。"""
    if len(focuses) < 2 or any(focus is None for focus in focuses):
        return focuses
    order = [i for i in reversed(range(len(focuses))) if focuses[i]["focus"] != "memo"] or [len(focuses) - 1]
    focuses = list(focuses)
    focuses[order[0]] = None
    return focuses
