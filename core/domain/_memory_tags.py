"""记忆标签：一条记忆挂几个「种类:值」，按东西找记忆用。

标签都从记录现有的字段算出来，不调模型；别处直接放进 ``tags`` 的（发动态时挂的 ``视频:``、``发过动态``）照留。
存的时候（``MemoryEntry._prepare_memory_entry``）算好放进记录的 ``tags``，sqlite 那边另有一张 ``memory_tags`` 表跟着写。
标签只用来缩小范围，读的时候照旧按域过滤，隔离不变。
"""
from __future__ import annotations

import re

VIDEO = "视频"
UP = "UP"
USER = "用户"
BANGUMI = "番剧"
POSTED_DYNAMIC = "发过动态:是"  # 挂在为它发过动态的那条视频记忆上

_BV = re.compile(r"BV[0-9A-Za-z]{10}")
_NOT_PEOPLE = {"", "self", "summary", "0"}


def tag(kind: str, value) -> str:
    return f"{kind}:{value}"


def _bvid(value) -> str:
    match = _BV.search(str(value or ""))
    return match.group(0) if match else ""


def tags_for(record: dict) -> list[str]:
    """这条记忆该挂的标签（去重、排好序）。"""
    tags = {str(t) for t in record.get("tags") or [] if isinstance(t, str) and ":" in t}
    bvid = str(record.get("bvid") or "").strip()
    if bvid[:2].lower() != "bv":
        bvid = ""
    thread = str(record.get("thread_id") or "")
    if not bvid and thread.startswith("video:"):
        bvid = _bvid(thread)
    if bvid:
        tags.add(tag(VIDEO, bvid))
    for key in ("owner_mid", "author_mid"):
        uid = str(record.get(key) or "").strip()
        if uid not in _NOT_PEOPLE:
            tags.add(tag(UP, uid))
    # 用 actor_id（``bili:UID`` / ``qq:号``）：QQ 号和 B站 UID 不混在一起，QQ 那边不打
    actor = str(record.get("actor_id") or "")
    if actor.startswith("bili:") and actor[5:] not in _NOT_PEOPLE:
        tags.add(tag(USER, actor[5:]))
    season = str(record.get("season_id") or "").strip()
    if season and season.isdigit():
        tags.add(tag(BANGUMI, f"ss{season}"))
    return sorted(tags)
