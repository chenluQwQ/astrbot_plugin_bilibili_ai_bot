"""伪流：一条已经校验过的私信回复按句子拆成几段，像打字一样分开发。

不是真流式：整条回复先生成、过校验，再拆。
"""
from __future__ import annotations

import re

MAX_PARTS = 3
_MIN_SPLIT_CHARS = 16  # 比这短的回复不拆
_MIN_PART_CHARS = 6  # 比这短的一段并进前一段
_ENDS = "。！？!?…~～"
# 一句：到句末标点为止；连着的标点、右括号、右引号、紧跟着的一个表情代码都算这一句的。换行也断开。
_SENTENCE = re.compile(rf"[^\n]*?(?:[{_ENDS}]+[」』）)”’]*(?:\[[^\[\]\s]{{1,16}}\])?|(?=\n)|$)")
_WORDS = re.compile(r"[0-9A-Za-z一-鿿]")
_EMOTE = re.compile(r"\[[^\[\]]{1,16}\]")


def _is_substantial(part):
    """去掉表情代码以后还有字、而且不太短。"""
    return len(_WORDS.findall(_EMOTE.sub("", part))) >= _MIN_PART_CHARS


def split_for_typing(text, max_parts=MAX_PARTS):
    text = str(text or "").strip()
    if max_parts < 2 or len(text) < _MIN_SPLIT_CHARS or "http" in text.lower():
        return [text] if text else []
    parts = []
    for piece in _SENTENCE.findall(text):
        piece = piece.strip()
        if not piece:
            continue
        if parts and (not _is_substantial(piece) or not _is_substantial(parts[-1])):
            parts[-1] += piece
        else:
            parts.append(piece)
    if len(parts) > max_parts:
        parts = parts[:max_parts - 1] + ["".join(parts[max_parts - 1:])]
    return parts


def typing_pause(part):
    """发下一段之前停多久（秒）：按这一段的字数，1～4 秒。"""
    return max(1.0, min(4.0, 0.6 + 0.15 * len(str(part or ""))))
