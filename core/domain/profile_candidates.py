"""画像候选：回复 JSON 里的 ``impression``、``user_facts`` 先存这里，不直接进画像。

学备忘录：回复时顺手记（回复本来就输出 JSON，不多花一次调用），每晚清算由总结模型对着现有画像
挑着写进去，其余丢掉。``profile_candidates.json`` 只由这里读写。

- 只给好感到过画像门槛的人存；每人每天最多 ``DAILY_LIMIT`` 条（一条事实、一句印象各算一条），主人不限
- 按（人，地方）记，评论和私信分开，夜里也分开整理
- 记下 ``KEEP_DAYS`` 天还没整理掉的过期
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from ..storage.json_file import JsonFile

DAILY_LIMIT = 5
KEEP_DAYS = 3


class ProfileCandidates:
    def __init__(self, file: JsonFile, reached: Callable[[str], bool], is_owner: Callable[[str], bool]):
        self._file = file
        self._reached = reached
        self._is_owner = is_owner

    def note(self, uid, scope, impression="", facts=(), username="", now: float | None = None) -> int:
        """存下这次回复里的印象和事实；返回存了几条。"""
        uid = str(uid or "").strip()
        items = [("impression", str(impression or "").strip())] + [("fact", str(f or "").strip()) for f in facts or ()]
        items = [(kind, text) for kind, text in items if text]
        if not uid or not items or not self._reached(uid):
            return 0
        now = datetime.now().timestamp() if now is None else now
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        unlimited = self._is_owner(uid)
        added = []

        def change(data):
            entries = [e for e in data.get(uid, []) if isinstance(e, dict) and now - e.get("ts", 0) < KEEP_DAYS * 86400]
            used = sum(1 for e in entries if e.get("date") == today)
            for kind, text in items:
                if not unlimited and used >= DAILY_LIMIT:
                    break
                if any(e["kind"] == kind and e["text"] == text and e["scope"] == scope for e in entries):
                    continue
                entries.append({"scope": str(scope), "kind": kind, "text": text, "date": today, "ts": now,
                                "name": str(username or "")})
                added.append(text)
                used += 1
            data[uid] = entries

        self._file.update(change, {})
        return len(added)

    def pending(self, now: float | None = None) -> list[dict]:
        """没过期的候选，按（人，地方）分组：``[{"uid", "scope", "username", "impressions", "facts", "ts"}]``，新的组在前。"""
        now = datetime.now().timestamp() if now is None else now
        data = self._file.read({})
        groups: dict[tuple[str, str], dict] = {}
        for uid, entries in (data.items() if isinstance(data, dict) else ()):
            for entry in entries if isinstance(entries, list) else ():
                if not isinstance(entry, dict) or now - entry.get("ts", 0) >= KEEP_DAYS * 86400:
                    continue
                key = (str(uid), str(entry.get("scope") or ""))
                group = groups.setdefault(key, {"uid": key[0], "scope": key[1], "username": "",
                                                "impressions": [], "facts": [], "ts": []})
                group["impressions" if entry.get("kind") == "impression" else "facts"].append(entry["text"])
                group["ts"].append(entry["ts"])
                group["username"] = entry.get("name") or group["username"]
        result = sorted(groups.values(), key=lambda g: max(g["ts"]), reverse=True)
        for group in result:
            group["ts"] = max(group["ts"])
        return result

    def done(self, keys, now: float | None = None) -> None:
        """这些（人，地方）整理完了，删掉它们的候选；顺带清掉过期的。"""
        keys = {(str(uid), str(scope)) for uid, scope in keys}
        now = datetime.now().timestamp() if now is None else now

        def change(data):
            for uid in list(data):
                kept = [
                    e for e in data[uid] if isinstance(e, dict)
                    and (uid, str(e.get("scope") or "")) not in keys
                    and now - e.get("ts", 0) < KEEP_DAYS * 86400
                ] if isinstance(data[uid], list) else []
                if kept:
                    data[uid] = kept
                else:
                    data.pop(uid)

        self._file.update(change, {})
