"""哪些评论和 @ 处理过：评论 rpid、@ 通知 id 两份账本只由这里写（存储见 ``storage/processed_keys.py``）。

认领一条评论时、在生成回复之前就记下，之后失败也不会再回。评论 rpid 每轮现读；
@ 通知 id 先记在内存里，``save_at`` 时一起写。
"""
from __future__ import annotations

from ..storage.processed_keys import KeySet


class RepliedComments:
    def __init__(self, replied: KeySet, at: KeySet):
        self._replied = replied
        self._at = at
        self._at_pending: set[str] = set()

    # ── 评论 ──
    def load(self) -> set:
        return self._replied.all()

    def save(self, replied: set) -> None:
        """记下这些 rpid；账本只增不减（旧的由每晚清理删）。"""
        self._replied.add(replied)

    def trim(self) -> tuple[int, int] | None:
        """评论只留最近 2000 条，返回 (原来, 现在)；没超就 None。@ 也顺手截一次。"""
        self._at.trim()
        return self._replied.trim()

    # ── @ 通知 ──
    def at_seen(self, at_id: str) -> bool:
        return at_id in self._at_pending or self._at.has(at_id)

    def remember_at(self, at_id: str) -> None:
        """只记在内存里，下次 ``save_at`` 一起写。"""
        self._at_pending.add(at_id)

    def save_at(self) -> None:
        self._at.add(self._at_pending)
        self._at_pending.clear()
