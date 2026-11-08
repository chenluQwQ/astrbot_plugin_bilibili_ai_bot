"""今天的心情和节日：``mood.json`` 只由这里读写。

心情每天醒来时定一次（主干的例行检查调 ``settle``），别处只读 ``today``。以前是当天第一个来问的
流程顺手随机抽一个，今天的心情取决于先跑的是回复、看片还是发动态。
"""
from __future__ import annotations

import logging
import random
from datetime import datetime
from typing import Callable

from ..storage.json_file import JsonFile

logger = logging.getLogger("astrbot")

CALM = ("🌙 平静如常", "按正常性格回复。")
MOODS = (
    ("☀️ 心情不错", "语气稍微轻快一点。"),
    ("🌙 平静如常", "按正常性格回复。"),
    ("🌧️ 有点安静", "话少一点。"),
    ("😏 有点皮", "偶尔多一点调侃。"),
    ("🧊 懒得废话", "回复更简洁。"),
)
# 关掉心情时显示的名字，不带提示词
DISABLED = ("🌙 平静如常", "")

_FESTIVALS = {
    "01-01": "今天是元旦！语气温暖。",
    "02-14": "今天是情人节。",
    "04-01": "今天是愚人节！可以开小玩笑。",
    "05-01": "今天是劳动节。",
    "10-31": "今天是万圣节，语气神秘。",
    "12-25": "今天是圣诞节，语气温柔。",
    "12-31": "今天是跨年夜。",
}
_LUNAR_FESTIVALS = {
    "01-01": "今天是春节！热情说新年快乐。",
    "01-15": "今天是元宵节。",
    "05-05": "今天是端午节。",
    "08-15": "今天是中秋节。",
}


class Mood:
    def __init__(self, file: JsonFile, enabled: Callable[[], bool]):
        self._file = file
        self._enabled = enabled

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def today(self) -> tuple[str, str]:
        """只读：今天醒来时定下的心情；今天还没定（比如还没醒）就按平静。"""
        if not self._enabled():
            return DISABLED
        data = self._file.read({})
        if isinstance(data, dict) and data.get("date") == self._today():
            return data["mood"], data["mood_prompt"]
        return CALM

    def settle(self) -> None:
        """醒着的时候由主干调用：今天还没定心情，就随机定一个并记下。"""
        if not self._enabled():
            return
        today = self._today()
        data = self._file.read({})
        if isinstance(data, dict) and data.get("date") == today:
            return
        mood, prompt = random.choice(MOODS)
        self._file.write({"date": today, "mood": mood, "mood_prompt": prompt})
        logger.info(f"[BiliBot] 🌅 今天的心情：{mood}")

    @staticmethod
    def festival() -> str:
        now = datetime.now()
        try:
            from lunardate import LunarDate
            lunar = LunarDate.fromSolarDate(now.year, now.month, now.day)
            lunar_md = f"{lunar.month:02d}-{lunar.day:02d}"
        except Exception:
            lunar_md = ""
        return _FESTIVALS.get(now.strftime("%m-%d"), "") or _LUNAR_FESTIVALS.get(lunar_md, "")
