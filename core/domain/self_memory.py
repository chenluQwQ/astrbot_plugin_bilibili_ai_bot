"""永久记忆：Bot 关于自己和主人的几条长期认知，进所有回复和动态的提示词。

``permanent_memory.json`` 只由这里写；回复上下文和写动态直接读文件（只读），用 ``format_numbered`` 过滤和编号。
最多 ``MAX_ENTRIES`` 条，满了要先改或删一条（由 Bot 自己决定换掉哪条）。
旧版上限是 20，升级上来超过 8 条的照样读、照样能改和删，只是加不进新的。

**隔离**：每条记着在哪里写的（``bili_comment`` / ``bili_dm``，旧的没记算公开评论），
读的时候按 ``security.scopes.can_read`` 过滤：私信里记的不会出现在评论区和动态里。
编号按「这个场合看得到的」从 1 排，所以在评论区改不了、删不了私信里记的；在私信里改了一条公开的，它就变成私信的。
``reader=None`` 是管理员（``/bili永久记忆``）看全部。
"""
from __future__ import annotations

from datetime import datetime

from ..security.scopes import can_read
from ..storage.json_file import JsonFile

MAX_ENTRIES = 8
TEXT_LIMIT = 60
DEFAULT_SCOPE = "bili_comment"


def _rows(entries) -> list[dict]:
    return [e for e in entries if isinstance(e, dict) and str(e.get("text") or "").strip()] if isinstance(entries, list) else []


def _visible(entry, reader) -> bool:
    return reader is None or can_read(reader, entry.get("scope") or DEFAULT_SCOPE)


def format_numbered(entries, reader=None) -> str:
    """给提示词用：``#1 文字`` 一行一条，只放 ``reader`` 看得到的；编号和工具里的一致。"""
    rows = [e for e in _rows(entries) if _visible(e, reader)]
    return "\n".join(f"#{i} {e['text']}" for i, e in enumerate(rows, 1))


class SelfMemoryRejected(ValueError):
    """做不了，原因是给模型看的一句话。"""


class SelfMemories:
    def __init__(self, file: JsonFile):
        self._file = file

    def entries(self, reader=None) -> list[dict]:
        return [e for e in _rows(self._file.read([])) if _visible(e, reader)]

    @staticmethod
    def _text(text) -> str:
        text = " ".join(str(text or "").split())
        if not text:
            raise SelfMemoryRejected("text 不能为空。")
        if len(text) > TEXT_LIMIT:
            raise SelfMemoryRejected(f"太长了，最多{TEXT_LIMIT}字。")
        return text

    def _locate(self, rows, number, scope) -> int:
        """``scope`` 看得到的第 number 条在整份里的下标。"""
        visible = [i for i, e in enumerate(rows) if _visible(e, scope)]
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= len(visible):
            raise SelfMemoryRejected(f"没有第 {number} 条，现在有 {len(visible)} 条。")
        return visible[number - 1]

    def add(self, text, scope=None) -> int:
        """记一条，返回它在 ``scope`` 看得到的里面是第几条。"""
        text = self._text(text)
        rows = _rows(self._file.read([]))
        if any(e["text"] == text for e in rows if _visible(e, scope)):
            raise SelfMemoryRejected("已经记着这一条了。")
        if len(rows) >= MAX_ENTRIES:
            hidden = len(rows) - len([e for e in rows if _visible(e, scope)])
            note = f"其中 {hidden} 条是私信里记的，这里看不到也改不了；" if hidden else ""
            raise SelfMemoryRejected(f"已经满{MAX_ENTRIES}条了，{note}想记新的就先改掉（edit）或删掉（delete）一条不那么重要的。")
        rows.append({"text": text, "time": datetime.now().strftime("%Y-%m-%d %H:%M"), "scope": scope or DEFAULT_SCOPE})
        self._file.write(rows)
        return len([e for e in rows if _visible(e, scope)])

    def edit(self, number, text, scope=None) -> str:
        text = self._text(text)
        rows = _rows(self._file.read([]))
        index = self._locate(rows, number, scope)
        old = rows[index]["text"]
        rows[index] = {"text": text, "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "scope": scope or rows[index].get("scope") or DEFAULT_SCOPE}
        self._file.write(rows)
        return old

    def delete(self, number, scope=None) -> str:
        rows = _rows(self._file.read([]))
        removed = rows.pop(self._locate(rows, number, scope))
        self._file.write(rows)
        return removed["text"]
