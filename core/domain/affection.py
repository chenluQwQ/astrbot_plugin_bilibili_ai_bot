"""好感度：每个 B站用户一个分数，以及关系到了某个分数时的「里程碑」。

``affection.json``、``milestones.json`` 和 ``profile_reached.json``（现在在 sqlite 的 ``keyed_json`` 表里）只由这里读写；别的模块要读分数、改分数都调这里的方法。
各场景的加减规则（评论、私信的上下限）在各自的流程里算好再交给 ``set``。
"""

from __future__ import annotations

from astrbot.api import logger
from datetime import datetime
from typing import Callable

from ..storage.json_file import JsonFile


OWNER_SCORE = 100


class Affection:
    """``reached_file``：好感第一次到画像门槛（``profile_line()``，默认 20）的人，记下日期。
    到过的人才有画像，之后分数掉下去也不取消。"""

    def __init__(self, file: JsonFile, is_owner: Callable[[str], bool],
                 reached_file: JsonFile | None = None, profile_line: Callable[[], int] = lambda: 20):
        self._file = file
        self._is_owner = is_owner
        self._reached_file = reached_file
        self._profile_line = profile_line
        self.reload()

    def reload(self) -> None:
        """分数常驻内存；存储换了地方（启动时接上数据库）后重读一次。"""
        data = self._file.read({})
        self._scores: dict[str, int] = dict(data) if isinstance(data, dict) else {}

    # ── 读 ──
    def score(self, uid) -> int:
        return self._scores.get(str(uid), 0)

    def known(self, uid) -> bool:
        return str(uid) in self._scores

    def ranking(self, limit: int) -> list[tuple[str, int]]:
        return sorted(self._scores.items(), key=lambda item: item[1], reverse=True)[:limit]

    def total(self) -> int:
        return sum(int(value or 0) for value in self._scores.values())

    def snapshot(self) -> dict[str, int]:
        return dict(self._scores)

    def __len__(self) -> int:
        return len(self._scores)

    def reached_profile_line(self, uid) -> bool:
        if self._reached_file is None:
            return False
        reached = self._reached_file.read({})
        return isinstance(reached, dict) and str(uid) in reached

    def level(self, score: int, uid=None) -> str:
        if uid and self._is_owner(uid):
            return "special"
        if score <= -10:
            return "cold"
        if score >= 51:
            return "close"
        if score >= 31:
            return "friend"
        if score >= 11:
            return "normal"
        return "stranger"

    # ── 写 ──
    def set(self, uid, score: int) -> None:
        self._scores[str(uid)] = score
        self._file.write(self._scores)
        if score >= self._profile_line() and not self.reached_profile_line(uid):
            self._mark_reached([str(uid)])

    def mark_reached_from_scores(self) -> int:
        """启动时按现有分数补标记（升级上来的老用户、门槛调低了）；返回新标了几个。"""
        line = self._profile_line()
        return self._mark_reached([uid for uid, score in self._scores.items() if int(score or 0) >= line])

    def _mark_reached(self, uids) -> int:
        if self._reached_file is None or not uids:
            return 0
        added = []

        def mark(reached):
            for uid in uids:
                if uid not in reached:
                    reached[uid] = datetime.now().strftime("%Y-%m-%d")
                    added.append(uid)

        self._reached_file.update(mark, {})
        return len(added)

    def pin_owner(self, owner_uid: str) -> None:
        """主人固定满分；启动时调用一次。"""
        if owner_uid:
            self.set(owner_uid, OWNER_SCORE)


class Milestones:
    """好感度第一次跨过 10/30/50/80/99 分时，Bot 说一句特别的话；每人每个阈值只说一次。"""

    MESSAGES = {
        10: "「{name}」，你对我来说不再是陌生人了哦。",
        30: "不知不觉就和「{name}」变熟了呢。",
        50: "「{name}」...我们算是好朋友了吧？",
        80: "能和「{name}」走到这一步，我挺开心的。",
        99: "「{name}」，你是我最重要的人之一。",
    }

    def __init__(self, file: JsonFile):
        self._file = file

    def peek(self, uid, old_score: int, new_score: int, username: str):
        """只看这次加分会不会触发里程碑，不记录。返回 (阈值, 那句话) 或 None。"""
        reached = self._file.read({}).get(str(uid), [])
        for threshold, message in self.MESSAGES.items():
            if old_score < threshold <= new_score and threshold not in reached:
                return threshold, message.format(name=username)
        return None

    def commit(self, uid, threshold: int, username: str = "") -> None:
        """那句话确实发出去之后再记下来；发送失败时不记，以后再达标还会说。"""
        def mark(data):
            reached = data.setdefault(str(uid), [])
            if threshold not in reached:
                reached.append(threshold)
                logger.info(f"[BiliBot] 🏆 里程碑！{username} 达到 {threshold} 分")

        self._file.update(mark, {})
