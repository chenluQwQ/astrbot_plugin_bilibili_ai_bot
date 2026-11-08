"""B站原生表情（小黄脸）：评论和私信回复里可以带一个，发送前清洗。

思路参考外部 PR #63，代码自己写。白名单里的代码 2026-11-06 在评论表情面板
（``x/emote/package?business=reply&ids=1``，小黄脸）里核对过都存在。
带游戏、作品名的表情不放进来，免得像打广告。
"""
from __future__ import annotations

import re

# 代码 → 什么语气用（写进提示词）。
EMOTES = {
    "[doge]": "调侃、开玩笑、轻微反讽",
    "[笑哭]": "好笑又无奈",
    "[吃瓜]": "看热闹、围观",
    "[打call]": "支持、夸人",
    "[妙啊]": "觉得巧妙、想法好",
    "[星星眼]": "期待、很喜欢",
    "[辣眼睛]": "开玩笑地说看不下去",
    "[滑稽]": "坏笑、调皮",
    "[给心心]": "温柔、感谢",
    "[捂脸]": "尴尬、不好意思",
    "[思考]": "认真想一想",
    "[酸了]": "开玩笑地羡慕",
    "[脸红]": "被夸了害羞",
    "[疑惑]": "没看懂、有点懵",
}

# 长得像表情代码的方括号：里面是汉字、字母、下划线，不含空格和纯数字（[1] 这种引用不动）。
_CODE = re.compile(r"\[[A-Za-z_一-鿿][0-9A-Za-z_一-鿿\-]{0,15}\]")


def emote_prompt() -> str:
    """给回复模型的说明：可以在句末带 0 或 1 个表情。"""
    listed = "；".join(f"{code} {tone}" for code, tone in EMOTES.items())
    return (
        "【B站表情】reply 里可以在句末带一个B站表情代码，也可以不带，不要为了用而用；最多一个，"
        "只能从这里选：" + listed + "。"
        "说到伤亡、生病、求助、别人难过、严肃纠正事实时不要用。"
    )


def tidy_emotes(text: str) -> str:
    """只留白名单里的第一个表情，其余像表情代码的方括号删掉。"""
    kept = False

    def keep_first(match):
        nonlocal kept
        code = match.group(0)
        if code in EMOTES and not kept:
            kept = True
            return code
        return ""

    cleaned = _CODE.sub(keep_first, str(text or ""))
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()
