"""现在在干嘛：分支报告自己进行到哪一步，QQ 聊天时拼进提示词。

按活动类型分开记：看片这一轮结束时会接着发动态，追番和看片也可能同时跑，
一个活动结束不能把别的活动的状态也清掉。只存在内存里，重启后本来就什么都没在做。

``activity_runs`` 的 running 替代不了它：表里没有「进行到哪一步、在看哪个」，
看片时接着发的那条动态也不是单独的一次活动。
"""

from __future__ import annotations

# 每种活动的叫法。
DOING_LABELS = {
    "proactive": "主动浏览",
    "bangumi": "追番",
    "dynamic": "发布动态",
    "dynamic_watch": "巡视关注动态",
    "special_follow": "看特别关注",
}


class NowDoing:
    def __init__(self):
        self._items: dict[str, dict] = {}

    def report(self, kind: str, stage: str, **details) -> None:
        """同一种活动再报一次就是换阶段，详情整份换掉。"""
        self._items[kind] = {
            "stage": stage,
            **{key: str(value)[:160] for key, value in details.items() if value not in (None, "")},
        }

    def finish(self, kind: str) -> None:
        self._items.pop(kind, None)

    def describe(self, labels: dict[str, str]) -> str:
        """一句话说现在在干嘛；什么都没做时返回空字符串。"""
        return "；同时".join(self._clause(kind, item, labels) for kind, item in self._items.items())

    def prompt(self) -> str:
        """拼进 QQ 聊天提示词的那句；什么都没做时返回空字符串。"""
        doing = self.describe(DOING_LABELS)
        if not doing:
            return ""
        return f"【当前活动】{doing}。仅在用户询问当前活动或自然相关时提及；不得猜测未记录的进度。"

    @staticmethod
    def _clause(kind: str, item: dict, labels: dict[str, str]) -> str:
        text = f"正在{labels.get(kind, kind)}：{item['stage']}"
        if item.get("title"):
            text += f"；内容：《{item['title']}》"
        if item.get("episode"):
            text += f"；{item['episode']}"
        return text
