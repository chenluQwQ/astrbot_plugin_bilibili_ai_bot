"""低分 UP 跳过名单：总在搜索、视频池里刷到、又总是打低分的 UP，Bot 自己一段时间不看。

只影响搜索和视频池选片；关注、特别关注、主人推荐、备忘都不过这里。不拉黑、不对外做任何操作。
``up_skip.json`` 只由这里读写：``{UID: {"name", "until", "count", "average", "added"}}``，到期自己出来。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from ..storage.json_file import JsonFile

WINDOW_DAYS = 14   # 看最近这么多天的观看记录
MIN_VIDEOS = 3     # 同一个 UP 至少看了这么多个（不同的视频）
MAX_AVERAGE = 4.0  # 平均分不超过这个
SKIP_DAYS = 30     # 进名单这么多天
# 只数这些来源看的：自己刷到的。关注、特别关注、备忘、主人推荐来的不算
COUNTED_SOURCES = {"search", "taste", "pool", "explore"}


def low_score_ups(watch_log, *, now: float, exempt=()) -> dict[str, dict]:
    """最近 ``WINDOW_DAYS`` 天里刷到 ``MIN_VIDEOS`` 个以上、平均分 ≤ ``MAX_AVERAGE`` 的 UP。"""
    cutoff = (datetime.fromtimestamp(now) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M")
    exempt = {str(uid) for uid in exempt}
    per_up: dict[str, dict] = {}
    for entry in watch_log or []:
        if not isinstance(entry, dict) or str(entry.get("time") or "") < cutoff:
            continue
        uid = str(entry.get("up_mid") or "").strip()
        if not uid or uid == "0" or uid in exempt or entry.get("source") not in COUNTED_SOURCES:
            continue
        try:
            score = float(entry.get("score") or 0)
        except (TypeError, ValueError):
            continue
        if not 1 <= score <= 10:  # 评价失败的记 0，不算
            continue
        item = per_up.setdefault(uid, {"name": "", "scores": {}})
        item["name"] = str(entry.get("up_name") or item["name"])
        item["scores"][str(entry.get("bvid") or len(item["scores"]))] = score
    result = {}
    for uid, item in per_up.items():
        scores = list(item["scores"].values())
        average = sum(scores) / len(scores)
        if len(scores) >= MIN_VIDEOS and average <= MAX_AVERAGE:
            result[uid] = {"name": item["name"], "count": len(scores), "average": round(average, 1)}
    return result


class UpSkip:
    def __init__(self, file: JsonFile):
        self._file = file

    def entries(self, now: float | None = None) -> dict[str, dict]:
        """还在名单上的（过期的不算）。"""
        now = time.time() if now is None else now
        data = self._file.read({})
        if not isinstance(data, dict):
            return {}
        return {uid: dict(info) for uid, info in data.items()
                if isinstance(info, dict) and float(info.get("until") or 0) > now}

    def review(self, watch_log, *, exempt=(), now: float | None = None) -> list[dict]:
        """按观看记录更新名单：过期的拿掉，新够条件的放进去（已经在的不延期）。返回新放进去的。没变就不写文件。"""
        now = time.time() if now is None else now
        exempt = {str(uid) for uid in exempt}
        found = low_score_ups(watch_log, now=now, exempt=exempt)
        added = []

        def change(data):
            data = data if isinstance(data, dict) else {}
            kept = {uid: info for uid, info in data.items()
                    if isinstance(info, dict) and float(info.get("until") or 0) > now and uid not in exempt}
            for uid, info in found.items():
                if uid in kept:
                    continue
                kept[uid] = {**info, "until": now + SKIP_DAYS * 86400,
                             "added": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")}
                added.append({"uid": uid, **kept[uid]})
            return kept

        current = self._file.read({})
        if change(current) != current:
            added.clear()
            self._file.update(change, {})
        return added
