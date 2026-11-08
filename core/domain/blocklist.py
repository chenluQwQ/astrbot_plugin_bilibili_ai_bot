"""本地黑名单和「恶意次数」计数。

``block_log.json``（谁被拉黑、为什么）和 ``block_count.json``（每人累计几次明显恶意）
只由这里读写。B站那边的拉黑接口不在这里，这里只记 Bot 自己的判断。
"""

from __future__ import annotations

from ..storage.json_file import JsonFile


class BlockList:
    def __init__(self, entries_file: JsonFile, strikes_file: JsonFile):
        self._entries = entries_file
        self._strikes = strikes_file

    # ── 黑名单 ──
    def contains(self, uid) -> bool:
        return str(uid) in self._entries.read({})

    def entries(self) -> dict[str, dict]:
        data = self._entries.read({})
        return dict(data) if isinstance(data, dict) else {}

    def add(self, uid, info: dict) -> None:
        def put(data):
            data[str(uid)] = dict(info)
        self._entries.update(put, {})

    def remove(self, uid) -> bool:
        removed = {}

        def drop(data):
            removed["hit"] = data.pop(str(uid), None) is not None
        self._entries.update(drop, {})
        return removed["hit"]

    # ── 恶意次数 ──
    def strikes(self, uid) -> int:
        return int(self._strikes.read({}).get(str(uid), 0) or 0)

    def has_strikes_record(self, uid) -> bool:
        return str(uid) in self._strikes.read({})

    def set_strikes(self, uid, count: int) -> None:
        def put(data):
            data[str(uid)] = count
        self._strikes.update(put, {})
