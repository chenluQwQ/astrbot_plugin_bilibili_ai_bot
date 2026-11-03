"""B站原生表情：只使用回复目录已确认的小黄脸代码，不把上传图片塞进楼中楼。"""
import re

EMOTES = {
    "[doge]": "调侃或玩梗", "[吃瓜]": "围观吃瓜", "[笑哭]": "好笑又无奈",
    "[捂脸]": "尴尬或无奈", "[疑惑]": "困惑追问", "[惊讶]": "意外惊讶",
    "[嫌弃]": "嫌弃吐槽", "[偷笑]": "会心一笑", "[点赞]": "认可赞同",
    "[打call]": "兴奋支持", "[无语]": "无语", "[大哭]": "难过",
}
_BRACKETS = re.compile(r"\[[^\[\]\r\n]{1,40}\]")


def appropriate(context):
    return not re.search(r"伤亡|遇难|死亡|自杀|战火|灾难|救命|急救|报警|认真求助|事实纠正",
                         str(context or ""))


def prompt():
    options = "、".join(f"{code}（{meaning}）" for code, meaning in EMOTES.items())
    return (
        "可按这条回复的语气选用0或1个B站原生表情，放进text正文末尾："
        + options + "。轻松玩梗、吃瓜或无奈时可以使用；严肃事实纠正、伤亡话题和认真求助不用。"
        "不要每条都加，不编造表情代码，不用图库图片，不用表情代替具体回复。"
    )


def normalize(text, limit=200, *, allow=True):
    """保留原句，删掉未知/多余表情；截断不会留下半个表情代码。"""
    found = []
    def keep(match):
        code = match.group()
        if not allow or code not in EMOTES or found:
            return ""
        found.append(code)
        return code
    text = _BRACKETS.sub(keep, str(text or "")).strip()
    text = text[:max(1, int(limit))]
    if text.rfind("[") > text.rfind("]"):
        text = text[:text.rfind("[")].rstrip()
    return text


def extract(text):
    return list(dict.fromkeys(code for code in _BRACKETS.findall(str(text or "")) if code in EMOTES))
