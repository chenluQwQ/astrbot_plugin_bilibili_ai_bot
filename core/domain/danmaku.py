"""弹幕：从一集的弹幕里挑出「大家在哪儿激动」，给追番当素材。

弹幕是别人发的，交给模型时要标不可信资料（调用方负责）。取弹幕、解析 XML 在 ``infra/bili/bangumi.py``，这里只做纯计算：

- 弹幕多（超过 ``FILTER_ABOVE`` 条）时先滤掉和剧情无关的：纯数字（666、233）、打卡报到（来了、第一、前排、签到）、
  考古（2024 年还在看、几年了、还有人吗）、空降指路、乾杯；弹幕少时全留着，免得滤完没剩几条
- 先去重：去掉标点和空格、叠起来刷屏的收成一句（「梦开始的地方梦开始的地方…」→「梦开始的地方」）、
  「哈哈哈」「hhh」算一种；字差不多的短句（「梦的开始」「梦开始的地方」）并成一条，次数加起来
- 按 ``WINDOW_SECONDS`` 秒分段，挑弹幕最密的 ``HOT_WINDOWS`` 段，挑的段互不相邻（免得全落在片头）；
  每段给几条去重后最多的
- 再列整集刷得最多、前面没列过的几句
- 总长控制在 ``MAX_CHARS`` 以内
"""
from __future__ import annotations

import re
from collections import Counter

WINDOW_SECONDS = 30
HOT_WINDOWS = 3
LINES_PER_WINDOW = 4
TOP_REPEATED = 5
MAX_CHARS = 600
ITEM_CHARS = 20          # 一条弹幕最多显示这么长
SIMILAR = 0.6            # 两句短弹幕的字重合到这个比例就当成一句
SHORT = 12               # 只有这么短的才按字重合合并，长句子字重合不说明意思一样
FILTER_ABOVE = 200
_USELESS = re.compile(
    r"^\d+$|打卡|签到|报到|报道|前排|^第[一1]|^来了|^我来了|^来啦|\d{4}年|年了|还有人|考古|空降|指路|乾杯|合影|^[?？。.]+$"
)
_NOISE = re.compile(r"[^\w]|_", re.UNICODE)
_REPEAT = re.compile(r"(.{2,}?)\1+")
_LAUGH = re.compile(r"^(哈|h|w|233)+$")


def normalize(text: str) -> str:
    """去重用的样子：小写、去标点空格、叠起来的收成一句、笑声算一种。"""
    text = _NOISE.sub("", str(text or "").lower())
    text = _REPEAT.sub(r"\1", text)
    if _LAUGH.match(text):
        return "哈哈哈"
    return text


def useless(text: str) -> bool:
    """和剧情无关的弹幕：纯数字、打卡、考古、空降、乾杯，或者去掉标点后不到两个字。"""
    raw = str(text or "").strip()
    return len(normalize(raw)) < 2 or bool(_USELESS.search(raw))


def _similar(a: str, b: str) -> bool:
    if a == b:
        return True
    if not a or not b:
        return False
    if a in b or b in a:
        return min(len(a), len(b)) >= 2
    if max(len(a), len(b)) > SHORT:
        return False
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) >= SIMILAR


def merge(texts: list[str]) -> list[tuple[str, int]]:
    """[(代表的那句, 次数)]，次数多的在前；字差不多的并到次数多的那句上，显示原文里最短的写法。"""
    counts = Counter()
    shown: dict[str, str] = {}
    for text in texts:
        key = normalize(text)
        if not key:
            continue
        counts[key] += 1
        if key not in shown or len(text) < len(shown[key]):
            shown[key] = text
    groups: list[list] = []  # [代表 key, 次数]
    for key, count in counts.most_common():
        for group in groups:
            if _similar(group[0], key):
                group[1] += count
                break
        else:
            groups.append([key, count])
    groups.sort(key=lambda group: -group[1])
    return [(_short(shown[key]), count) for key, count in groups]


def _short(text: str) -> str:
    text = text.strip()
    return text if len(text) <= ITEM_CHARS else text[:ITEM_CHARS] + "…"


def _clock(second: float) -> str:
    second = int(second)
    return f"{second // 60:02d}:{second % 60:02d}"


def rough(count: int) -> str:
    """「60多条」这种说法：10 条以下照实说，整十照实说，其余按十位说「几十多条」。"""
    if count < 10 or count % 10 == 0:
        return f"{count}条"
    return f"{count // 10 * 10}多条"


def _labels(pairs: list[tuple[str, int]]) -> str:
    """「「哈哈哈」出现了60多条，「好帅」10多条」。"""
    parts = [f"「{text}」{rough(count)}" for text, count in pairs]
    if parts:
        parts[0] = parts[0].replace("」", "」出现了", 1)
    return "，".join(parts)


def danmaku_highlights(items: list[tuple[float, str]]) -> str:
    """弹幕最密的几段和刷得最多的几句，写成「「哈哈哈」出现了60多条」这样的几行；弹幕太少（不到 20 条）返回空。"""
    if len(items) < 20:
        return ""
    total_count = len(items)
    if total_count > FILTER_ABOVE:
        items = [(second, text) for second, text in items if not useless(text)]
        if len(items) < 20:
            return ""
    windows: dict[int, list[str]] = {}
    for second, text in items:
        windows.setdefault(int(second // WINDOW_SECONDS), []).append(text)
    chosen: list[int] = []
    for index, _ in sorted(windows.items(), key=lambda kv: -len(kv[1])):
        if all(abs(index - other) > 1 for other in chosen):
            chosen.append(index)
        if len(chosen) >= HOT_WINDOWS:
            break
    dropped = total_count - len(items)
    lines = [f"这集一共 {total_count} 条弹幕" + (f"（去掉打卡、刷数字这些和剧情无关的 {dropped} 条）" if dropped else "")
             + "。弹幕最密的几段："]
    listed: set[str] = set()
    for index in sorted(chosen):
        start = index * WINDOW_SECONDS
        top = merge(windows[index])[:LINES_PER_WINDOW]
        listed.update(normalize(text) for text, _ in top)
        lines.append(f"- {_clock(start)}-{_clock(start + WINDOW_SECONDS)}（这 30 秒 {len(windows[index])} 条）：{_labels(top)}")
    others = [(text, count) for text, count in merge([text for _, text in items])
              if count > 2 and not any(_similar(normalize(text), seen) for seen in listed)][:TOP_REPEATED]
    if others:
        lines.append(f"整集刷得多的还有：{_labels(others)}")
    out, total = [], 0
    for line in lines:
        if total + len(line) > MAX_CHARS:
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)
