"""B站原生小黄脸：按心情与语义挑选，避免所有回复都挂着同一个 doge。

设计原则：
1. 代码全部来自 B 站 `x/emote/package?business=reply&ids=1`（小黄脸包，225 项）实测清单，
   不自己编造；只保留跨圈通用的情绪表情，游戏联动限定款不参与自动挑选。
2. 先按当前心情（mood）给候选池，再按回复语义信号做加权，避免情绪与表情打架。
3. 提示词里带上「最近用过哪些」，要求换着用，别连续 doge。
"""
import re
from datetime import datetime

# 与评价系统一致的心情取值：开心/平静/无聊/感动/好笑/震撼/困惑（另含无语、难过作兜底）
EMOTE_LIBRARY = {
    # code: (含义, 适配心情集合, 语义信号词, 基础分偏移)
    # doge 曾占 85% 使用率，给负基础分，只有语义明确调侃时才可能胜出。
    "[doge]":        ("调侃、反讽、玩梗",     {"好笑", "开心", "平静"}, ("反讽", "调侃", "吐槽", "玩笑", "阴阳"), -2.0),
    "[吃瓜]":        ("围观、看热闹",         {"好笑", "无聊", "平静"}, ("围观", "八卦", "热闹", "瓜"), 0.0),
    "[嗑瓜子]":      ("看戏、慢慢看好玩",     {"好笑", "无聊"},         ("看戏", "围观", "等着看"), 0.0),
    "[笑哭]":        ("笑到流泪",             {"好笑", "开心"},         ("笑死", "爆笑", "太搞"), 0.0),
    "[大笑]":        ("开怀大笑",             {"好笑", "开心"},         ("好笑", "太乐", "哈哈"), 0.0),
    "[偷笑]":        ("偷着乐、会心一笑",     {"好笑", "开心", "平静"}, ("逗", "心照不宣", "憋笑"), 0.0),
    "[呲牙]":        ("咧嘴笑、得意",         {"开心", "好笑"},         ("得意", "嘿嘿", "美滋滋"), 0.0),
    "[喜极而泣]":    ("笑着哭、太感动",       {"感动", "开心"},         ("感动", "太好了", "泪目"), 0.0),
    "[滑稽]":        ("滑稽、揶揄",           {"好笑", "无语"},         ("滑稽", "揶揄", "不正经"), 0.0),
    "[歪嘴]":        ("歪嘴一笑、戏谑",       {"好笑", "平静"},         ("戏谑", "坏笑", "得意"), 0.0),
    "[调皮]":        ("调皮、耍宝",           {"开心", "好笑"},         ("调皮", "耍宝", "闹"), 0.0),
    "[妙啊]":        ("妙、佩服",             {"开心", "震撼", "平静"}, ("妙", "绝了", "高明"), 0.0),
    "[点赞]":        ("认可、赞同",           {"平静", "开心"},         ("认可", "赞同", "有道理"), 0.0),
    "[鼓掌]":        ("掌声、称赞",           {"开心", "感动"},         ("佩服", "优秀", "厉害"), 0.0),
    "[打call]":      ("兴奋支持",             {"开心", "震撼"},         ("支持", "太强", "冲"), 0.0),
    "[星星眼]":      ("崇拜、超喜欢",         {"开心", "震撼"},         ("喜欢", "崇拜", "太爱"), 0.0),
    "[惊喜]":        ("意外之喜",             {"震撼", "开心"},         ("惊喜", "没想到", "意外"), 0.0),
    "[惊讶]":        ("吃惊",                 {"震撼"},                 ("吃惊", "竟然", "居然"), 0.0),
    "[灵魂出窍]":    ("震惊到失神",           {"震撼"},                 ("震惊", "看傻", "离谱到"), 0.0),
    "[哦呼]":        ("赞叹、起哄",           {"震撼", "开心"},         ("哇", "赞叹", "绝"), 0.0),
    "[捂脸]":        ("尴尬、无奈",           {"困惑", "无语", "平静"}, ("尴尬", "无奈", "没眼看"), 0.0),
    "[尴尬]":        ("尴尬",                 {"困惑", "无语"},         ("尴尬", "社死"), 0.0),
    "[疑惑]":        ("困惑、追问",           {"困惑", "无聊"},         ("疑问", "不解", "为什么"), 0.0),
    "[思考]":        ("思考、琢磨",           {"困惑", "平静"},         ("思考", "琢磨", "想想"), 0.0),
    "[呆]":          ("愣住",                 {"困惑", "无聊"},         ("愣", "懵", "反应不过来"), 0.0),
    "[嫌弃]":        ("嫌弃、吐槽",           {"无语", "平静"},         ("嫌弃", "难绷", "不行"), 0.0),
    "[无语]":        ("无话可说",             {"无语", "无聊", "困惑"}, ("无语", "离谱", "没辙"), 0.0),
    "[翻白眼]":      ("翻白眼、受不了",       {"无语", "无聊"},         ("翻白眼", "受不了", "服了"), 0.0),
    "[撇嘴]":        ("撇嘴、不以为然",       {"无聊", "无语"},         ("不以为然", "撇嘴", "就这"), 0.0),
    "[抠鼻]":        ("无所谓、不上心",       {"无聊", "无语"},         ("无所谓", "懒得", "随便"), 0.0),
    "[哈欠]":        ("犯困、无聊",           {"无聊"},                 ("无聊", "困", "没意思"), 0.0),
    "[大哭]":        ("难过、破防",           {"感动", "震撼"},         ("难过", "破防", "心酸"), 0.0),
    "[委屈]":        ("委屈巴巴",             {"感动", "困惑"},         ("委屈", "可怜", "心疼"), 0.0),
    "[难过]":        ("难过",                 {"感动"},                 ("难过", "遗憾", "可惜"), 0.0),
    "[给心心]":      ("比心、喜欢",           {"感动", "开心"},         ("喜欢", "比心", "谢谢"), 0.0),
    "[抱拳]":        ("抱拳、多谢",           {"平静"},                 ("多谢", "承让", "佩服"), 0.0),
    "[加油]":        ("加油打气",             {"开心", "感动"},         ("加油", "打气", "挺住"), 0.0),
    "[微笑]":        ("礼貌、温和",           {"平静"},                 ("温和", "礼貌", "客气"), 0.0),
}

# 去重（上面若重复定义以最后一次为准）
EMOTES = {code: value[0] for code, value in EMOTE_LIBRARY.items()}

# 模型常见写错的表情名 → 官方代码的近似映射。
# 只收「同一个表情被叫错名字」或「情绪几乎等同」的情况；拿不准的一律不映射，
# 交给 normalize 丢弃，宁可少一个表情，也不要发出语义不符的。
EMOTE_ALIASES = {
    # ① 同物异名：官方叫法不同，指的是同一个表情
    "[震惊]": "[惊讶]",
    "[吃惊]": "[惊讶]",
    "[憨笑]": "[呲牙]",
    "[捂脸哭]": "[捂脸]",
    "[破涕为笑]": "[喜极而泣]",
    "[流汗]": "[无语]",
    "[汗]": "[无语]",
    "[白眼]": "[翻白眼]",
    # ② 口语/网络常用叫法
    "[滑稽脸]": "[滑稽]",
    "[狗头]": "[doge]",
    "[doge头]": "[doge]",
    "[吃瓜群众]": "[吃瓜]",
    "[笑cry]": "[笑哭]",
    "[笑死]": "[笑哭]",
    "[赞]": "[点赞]",
    "[比心]": "[给心心]",
    "[爱心]": "[给心心]",
    "[加油鸭]": "[加油]",
    "[问号]": "[疑惑]",
    "[疑问]": "[疑惑]",
    "[懵逼]": "[呆]",
    "[无语子]": "[无语]",
    "[嫌弃脸]": "[嫌弃]",
    "[委屈巴巴]": "[委屈]",
    "[难受]": "[难过]",
    "[抱拳了]": "[抱拳]",
    # ③ 语义邻近：情绪非常接近，换成库内最贴近的那个
    "[喷血]": "[灵魂出窍]",
    "[裂开]": "[灵魂出窍]",
    "[跪了]": "[灵魂出窍]",
    "[awsl]": "[星星眼]",
    "[爱了]": "[星星眼]",
    "[绝了]": "[妙啊]",
    "[强]": "[点赞]",
    "[OK]": "[点赞]",
    "[微笑脸]": "[微笑]",
    "[尬笑]": "[尴尬]",
    "[社死]": "[尴尬]",
    "[叹气]": "[哈欠]",
    "[困]": "[哈欠]",
    "[哼]": "[撇嘴]",
    "[就这]": "[撇嘴]",
    "[无所谓]": "[抠鼻]",
    "[思考中]": "[思考]",
    "[琢磨]": "[思考]",
}

# 严肃语境一律不给表情：伤亡、求助、事实纠正等
SERIOUS_PATTERN = re.compile(
    r"伤亡|遇难|死亡|去世|离世|自杀|战火|战争|灾难|地震|洪水|事故|抢救|救命|急救|报警|报案|求助|"
    r"捐献|筹款|骗捐|事实纠正|辟谣|造谣|造假|违法|犯罪|诈骗|隐私泄露|未成年|校园暴力|医疗事故"
)

_BRACKETS = re.compile(r"\[[^\[\]\r\n]{1,40}\]")
# 模型偶尔写全角【表情】，先统一成半角再判定
_FULLWIDTH = str.maketrans({"【": "[", "】": "]"})


def to_halfwidth_brackets(text):
    """把【xxx】统一成 [xxx]，只影响方括号字符本身。"""
    return str(text or "").translate(_FULLWIDTH)


def appropriate(context):
    """严肃语境不配表情。"""
    return not SERIOUS_PATTERN.search(str(context or ""))


def _score(entry, mood, text):
    _code, (_meaning, moods, signals, bias) = entry
    score = bias
    if mood and mood in moods:
        score += 3.0
    elif not mood:
        score += 1.0
    hits = sum(1 for s in signals if s in text)
    score += hits * 1.5
    if hits:
        score += 1.0
    return score


def _ranked(mood, context=""):
    text = str(context or "")
    rows = [( _score((c, v), mood, text), c, v[0]) for c, v in EMOTE_LIBRARY.items()]
    rows.sort(key=lambda x: (-x[0], x[1]))
    return rows


def prompt(mood="", recent=None, context=""):
    """给模型的表情选择说明：按心情给候选，并要求避开最近用过的。"""
    recent = [c for c in (recent or []) if c in EMOTES]
    ranked = _ranked(mood, context)
    shortlist = [code for _s, code, _m in ranked[:10]]
    if recent:
        # 最近用过的挪到末尾，优先推荐没出现的
        shortlist = [c for c in shortlist if c not in recent] + [c for c in shortlist if c in recent]

    options = "、".join(f"{code}（{EMOTES[code]}）" for code in shortlist)
    parts = [
        "如确实需要，可在 reply 正文末尾放 0 或 1 个B站原生小黄脸。",
        f"优先候选：{options}。",
    ]
    if mood:
        parts.append(f"你此刻心情是「{mood}」，优先选与这个心情一致的那个。")
    parts.append(
        "选择依据：先判断这句话想表达的是一种什么情绪（玩梗调侃／无语嫌弃／佩服称赞／震惊／"
        "尴尬无奈／难过感动／围观看戏／疑惑思考），再挑语义最接近的一个；"
        "文字语气和心情冲突时以文字为准。"
    )
    parts.append("不要每条都加，多数时候不加更自然；一条最多一个，只放句末。")
    if recent:
        parts.append("最近用过：" + "、".join(recent[-3:]) + "，本次尽量换一个，不要连续重复。")
    else:
        parts.append("不要反复使用同一个表情。")
    parts.append("只写方括号代码本身，不要编造代码，不要用图片或颜文字代替具体回复。")
    return "".join(parts)


def pick(mood, context="", recent=None):
    """程序侧兜底：模型没给表情时按心情+语义选一个（仍受严肃语境约束）。"""
    if not appropriate(context):
        return None
    recent = [c for c in (recent or []) if c in EMOTES]
    for score, code, _meaning in _ranked(mood, context):
        if score <= 0:
            continue
        if code in recent[-2:]:
            continue
        return code
    return None


def _is_emote_intent(code):
    """这个方括号是不是在当表情用（含模型写错名字的近似写法）。"""
    return code in EMOTES or code in EMOTE_ALIASES


def _resolve(code):
    """把近似写法解析成库内真实代码；解析不出来返回 None。"""
    if code in EMOTES:
        return code
    return EMOTE_ALIASES.get(code)


# ── 未知表情采样 ──
# normalize 在每次回复的高频路径上，绝不在这里写磁盘；只把「丢弃了哪些代码」
# 记进内存计数器，由主循环定时 flush_unknown() 落盘，用于事后补 EMOTE_ALIASES。
_UNKNOWN_SEEN = {}
_UNKNOWN_DROPPED_TOTAL = 0
# 由宿主注入的日志回调（native_emotes 本身不依赖 astrbot logger，保持可单测）
_UNKNOWN_LOGGER = None


def set_unknown_logger(callback):
    """注入一条 `callback(message)` 用于记录未知表情；传 None 可关闭。"""
    global _UNKNOWN_LOGGER
    _UNKNOWN_LOGGER = callback


def note_unknown(code, dropped=True):
    """记录一个既不在库、也无法映射的方括号写法。"""
    global _UNKNOWN_DROPPED_TOTAL
    key = str(code or "").strip()
    if not key:
        return
    row = _UNKNOWN_SEEN.get(key)
    first_time = row is None
    if row is None:
        row = {"count": 0, "dropped": 0, "first": None, "last": None}
        _UNKNOWN_SEEN[key] = row
    row["count"] += 1
    if dropped:
        row["dropped"] += 1
        _UNKNOWN_DROPPED_TOTAL += 1
    now = datetime.now().isoformat(timespec="seconds")
    if not row["first"]:
        row["first"] = now
    row["last"] = now
    # 只在某个写法第一次出现时打一条日志，避免高频回复把日志刷爆；
    # 后续次数由统计面板呈现。
    if first_time and _UNKNOWN_LOGGER:
        try:
            tail = "（已丢弃）" if dropped else "（保留为正文）"
            _UNKNOWN_LOGGER(f"[BiliBot] 未知表情采样：{key}{tail}，共 {row['count']} 次")
        except Exception:
            pass


def unknown_stats():
    """当前进程内累计的未知表情（按次数降序）。"""
    rows = [{"code": k, **v} for k, v in _UNKNOWN_SEEN.items()]
    rows.sort(key=lambda r: (-r["count"], r["code"]))
    return {"total_samples": sum(r["count"] for r in rows),
            "total_dropped": _UNKNOWN_DROPPED_TOTAL,
            "items": rows}


def clear_unknown():
    """flush 成功后清空进程内计数。"""
    global _UNKNOWN_DROPPED_TOTAL
    _UNKNOWN_SEEN.clear()
    _UNKNOWN_DROPPED_TOTAL = 0


# 明显是正文/功能标记的方括号，不当作误写表情
_NON_EMOTE_TOKENS = {
    "重点", "图片", "图", "视频", "链接", "来源", "注", "备注", "笑", "哭",
    "鼓掌", "笑哭", "音乐", "字幕", "中字", "中配", "转载", "转", "广告", "已编辑",
    "展开", "收起", "回复", "置顶", "热评", "前排", "沙发", "楼主", "补充",
    "完", "待续", "更新", "预约", "直播", "回放", "预告", "投票", "抽奖",
}

# 像误写表情的形状：1~6 个字符，中文/英文/数字混排，且不含标点空格换行
_UNKNOWN_EMOTE_SHAPE = re.compile(r"^\[[0-9A-Za-z\u4e00-\u9fff_\-＋+]{1,6}\]$")


def _looks_like_unknown_emote(code):
    """判断一个方括号是否像「模型想用表情但代码不存在」。

    只做保守判定：形状像、又不是常见正文标记，才计入采样。
    宁可漏记，也不要让 [重点]、[1] 这种把统计污染成噪音。
    """
    raw = str(code or "").strip()
    if not _UNKNOWN_EMOTE_SHAPE.match(raw):
        return False
    inner = raw[1:-1]
    if inner in _NON_EMOTE_TOKENS:
        return False
    # 编号类（[1] [12] [2026]）多半是正文序号，不采样
    if inner.isdigit() and len(inner) > 1:
        return False
    return True


def normalize(text, limit=200, *, allow=True):
    """保留原句，删掉未知/多余表情；截断不会留下半个表情代码。

    - 全角【doge】先转半角；
    - 模型写错名字时（[震惊]/[滑稽脸]）按 EMOTE_ALIASES 换成官方代码；
    - 正文里的普通方括号（[重点]）不是表情，原样保留；
    - allow=False（严肃语境）一个表情都不留，也不会因为能映射就复活。
    """
    found = []

    def keep(match):
        raw = match.group()
        if not _is_emote_intent(raw):
            # 判断是不是「像表情但认不出来」：纯中文/英文短词、没空格、
            # 且不是常见正文标记，才算；否则当正文保留。
            if _looks_like_unknown_emote(raw):
                note_unknown(raw, dropped=True)
            return raw  # 正文内容，不动
        if not allow or found:
            return ""  # 严肃语境，或本条已有一个表情
        final = _resolve(raw)
        if not final:
            return ""
        found.append(final)
        return final

    text = to_halfwidth_brackets(text).strip()
    # 模型可能写成「正文。)[doge]」这种表情在标点外的形式，
    # 先把游离在句末的合法表情收回来，再统一做去重。
    trailing = re.findall(r"(\[[^\[\]\r\n]{1,40}\])\s*$", text)
    if trailing and _is_emote_intent(trailing[0]) and not _BRACKETS.search(
        text[: -len(trailing[0])]
    ):
        code = trailing[0]
        body = text[: -len(code)].rstrip()
        body = re.sub(r"[)）\s]+$", "", body).rstrip()
        text = body + code
    # 模型也常把表情写在句首（「[吃瓜] 这个…」），统一挪到句末更像真人。
    lead = re.match(r"^(\[[^\[\]\r\n]{1,40}\])\s*", text)
    if lead:
        code = lead.group(1)
        rest = text[lead.end():].lstrip()
        if rest and _is_emote_intent(code):
            text = rest.rstrip() + code
        elif rest and not _is_emote_intent(code) and allow:
            # 句首的方括号短词几乎都是在当表情用，但库里没有对应代码，
            # 直接去掉，避免发出去成了奇怪的正文。
            text = rest
    text = _BRACKETS.sub(keep, text)
    text = text.strip()
    text = text[:max(1, int(limit))]
    if text.rfind("[") > text.rfind("]"):
        text = text[:text.rfind("[")].rstrip()
    return text


def extract(text):
    text = to_halfwidth_brackets(text)
    codes = (c for c in _BRACKETS.findall(text) if _is_emote_intent(c))
    return list(dict.fromkeys(_resolve(c) for c in codes if _resolve(c)))


def repair_escaped_emote(raw, reply_key="reply"):
    """修模型把表情写到 JSON 引号外面的情况。

    实测 deepseek 会输出 ``{"reply":"这句话"[doge]}`` 或 ``{"reply":"...[困惑]"}``，
    导致整个 JSON 解析失败、回复作废。这里把引号外的表情挪回 reply 字符串内。
    """
    text = str(raw or "").strip()
    if not text or text.count('"') < 4:
        return raw

    value_pat = re.compile(r'("' + reply_key + r'"\s*:\s*")((?:[^"\\]|\\.)*)(")')

    # 情形一：结束引号之后紧跟一个合法表情
    def merge_trailing(m):
        body, code = m.group(2), m.group(4)
        if not _is_emote_intent(code):
            return m.group(0)
        return m.group(1) + body + code + m.group(3)

    # `"reply":"正文"[表情]` —— 表情落在收尾引号之后、但逗号/右括号之前。
    # 只在后面紧跟 , 或 } 时才动，避免误判正文里本来就有的方括号内容。
    fixed = re.sub(
        r'("' + reply_key + r'"\s*:\s*")((?:[^"\\]|\\.)*)(")\s*(\[[^\[\]\r\n]{1,40}\])(?=\s*[,}])',
        merge_trailing,
        text,
        count=1,
    )
    if fixed != text:
        return fixed

    # 情形二：表情被挤到最外层右括号前
    tail = re.search(r"(\[[^\[\]\r\n]{1,40}\])[\s}]*$", text)
    if not (tail and _is_emote_intent(tail.group(1))):
        return raw
    vm = value_pat.search(text)
    if not vm:
        return raw
    body = vm.group(2) + tail.group(1)
    rebuilt = vm.group(1) + body + vm.group(3)
    return text[:vm.start()] + rebuilt + text[vm.end():tail.start()] + text[tail.end():]


def repair_truncated_quote(raw, reply_key="reply"):
    """修 `{"reply":"正文[表情]}` 这类：有表情代码但少了收尾引号。

    只在缺的确实是一个引号、且表情合法时补；不猜正文内容。
    """
    text = str(raw or "").strip()
    if not text or text.count('"') % 2 == 0:
        return raw
    m = re.search(
        r'("' + reply_key + r'"\s*:\s*")((?:[^"\\]|\\.)*\[[^\[\]\r\n]{1,40}\])\s*\}\s*$',
        text,
    )
    if not m:
        return raw
    code = re.search(r"(\[[^\[\]\r\n]{1,40}\])$", m.group(2))
    if not code or not _is_emote_intent(code.group(1)):
        return raw
    return text[:m.end(2)] + '"' + text[m.end(2):]


def repair_reply_json(raw, reply_key="reply"):
    """尝试修两类会让整条回复作废的表情写法（都不猜正文）：

    1. 表情写在 reply 引号外：``{"reply":"文本"[吃瓜]}``
    2. 表情后面少了收尾引号：``{"reply":"文本[滑稽]}``
    修不好就原样返回，交给调用方按 invalid_json 处理。
    """
    for fixer in (repair_escaped_emote, repair_truncated_quote):
        try:
            fixed = fixer(raw, reply_key=reply_key)
        except Exception:
            continue
        if fixed != raw:
            return fixed
    return raw


def recent_emotes(records, key="reply", limit=3):
    """从历史记录里取最近用过的表情，用于避免重复。"""
    used = []
    for item in reversed(list(records or [])):
        if not isinstance(item, dict):
            continue
        codes = extract(item.get(key, ""))
        if codes:
            used.extend(codes)
        if len(used) >= limit:
            break
    return used[:limit]
