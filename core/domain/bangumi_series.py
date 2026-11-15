"""番剧从第一季看起：同系列里下一部该补哪季。

规则：正篇按B站给的系列顺序排；已经看过的最靠后那季之前、还没看完的最早一季就是该补的。
正在看的这季自己还没看完就不补（接着看它）。剧场版、特别篇这类不在正篇顺序里的不算；拿不准的不跳。
"""
from __future__ import annotations

import re

_SIDE_STORY = re.compile(r"剧场版|特别篇|总集篇|番外|OVA|OAD|电影|(?<![A-Za-z])SP(?![A-Za-z])", re.I)


def main_seasons(seasons):
    """正篇各季（保持B站给的顺序）。"""
    return [season for season in seasons or []
            if season.get("season_id") and not _SIDE_STORY.search(str(season.get("title") or ""))]


def series_position(seasons, season_id) -> tuple[str, int]:
    """这一季在系列里的位置：``(系列编号, 第几个)``。系列编号取B站给的顺序里的第一季。

    番剧记忆进长期时，同系列各季按这个顺序放在一条记忆里。系列里找不到这一季（单季番、
    接口没给系列）就自己算一个系列。剧场版、特别篇也按B站给的位置排，不丢。
    """
    current = str(season_id)
    ids = [str(season.get("season_id")) for season in seasons or [] if season.get("season_id")]
    if current not in ids:
        return current, 0
    return ids[0], ids.index(current)


def season_to_backfill(seasons, current_id, *, started, completed):
    """该补的那一季（``{"season_id", "title"}``），没有就 None。

    ``started``：看过至少一集的 season_id；``completed``：标了看完的 season_id。
    """
    order = main_seasons(seasons)
    ids = [str(season["season_id"]) for season in order]
    current = str(current_id)
    if current not in ids:
        return None
    started = {str(sid) for sid in started} | {current}
    completed = {str(sid) for sid in completed}
    furthest = max(index for index, sid in enumerate(ids) if sid in started)
    for index in range(furthest):
        if ids[index] in completed:
            continue
        return None if ids[index] == current else order[index]
    return None
