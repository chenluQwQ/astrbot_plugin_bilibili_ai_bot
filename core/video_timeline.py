"""视频模型按时间记下看到、听到的东西：输出格式、校验、拼成存档文本。

一段视频的输出是严格 JSON：``timeline``（每条带这段里的第几秒）、``impression``（整体印象）、
``audio``（这段听到了什么，用来在日志里看出模型到底有没有收到声音）。
程序把每条的秒数加上这段在原视频里的起点，拼成整条视频的时间线。

存档文本的格式：印象在最前面，后面是「【时间线】」和逐行 ``[分:秒] 内容``。
日志、记忆只取印象（``watch_digest``），完整时间线留给追问和回忆。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any


TIMELINE_MARK = "【时间线】"
PARTIAL_MARK = "【仅分析部分视频片段，不能确认全部内容】"
SEGMENT_KEYS = {"timeline", "impression", "audio", "sensitive", "comment_hint"}
MAX_ENTRIES = 24
ENTRY_LIMIT = 160
IMPRESSION_LIMIT = 300
AUDIO_LIMIT = 80
HINT_LIMIT = 150
# 视频模型收不到音轨时，audio 会写这个（提示词里要求的）。
NO_AUDIO_WORDS = ("听不到", "没有收到声音", "无法听到", "收不到声音")

_SEGMENT_RULES = """at 是这一段里的第几秒（数字，从 0 开始），按时间顺序，最多 24 条；text 每条 160 字以内，把看到的和听到的写在一起。text 用白描写。

这份记录之后要拿来聊这个视频、回答别人问的细节，所以记具体的东西：出现了什么人、什么东西，说了哪句话（尽量记原话），提到的名字、数字、结论，发生了什么意外或转折。「UP主开始讲解」「画面切换」「背景音乐响起」这种放在哪个视频里都成立的话，之后什么也回答不了，不用记；没有新内容的时刻就跳过，不用凑满条数。只写这段里确实看到、听到的，没听清的就不写，编出来的细节会被当真拿去说。"""

_RECORD_FIELDS = (
    '"timeline": [{"at": 秒数, "text": "这一刻画面里有什么、谁说了什么"}], '
    '"impression": "看完这一段的整体印象，一两句", '
    '"audio": "这段声音里有什么：有人讲话（大概在讲什么）/只有音乐/没声音；收不到声音就写「听不到」", '
    '"sensitive": 是不是政治、时事有争议的内容（true 或 false）, '
    '"comment_hint": "给之后写评论的自己留一两句：这个视频评论区适合说什么、哪里可以接、适合随手一句、段评（挑一段写感受）还是补充（视频没讲到的知识）"'
)

SEGMENT_SCHEMA_PROMPT = (
    "只输出一个 JSON 对象，不要代码块、不要别的文字：\n{" + _RECORD_FIELDS + "}\n" + _SEGMENT_RULES
)

# 视频模型和写评论的模型是同一个时，看最后一段那次顺便把评论写了：说的话在前，记录在后。
SEGMENT_WITH_COMMENT_SCHEMA_PROMPT = (
    "只输出一个 JSON 对象，不要代码块、不要别的文字，先写评论再写记录：\n{"
    '"comment": {"decision": "comment|skip", "text": "评论正文；长评分段用\\n换行；skip 时留空"}, '
    + _RECORD_FIELDS + "}\n" + _SEGMENT_RULES
)

def storyboard_schema_prompt(with_comment=False):
    """分镜只让模型选帧描述画面；文字、秒数由本地结果提供。"""
    fields = _RECORD_FIELDS.replace(
        '"timeline": [{"at": 秒数, "text": "这一刻画面里有什么、谁说了什么"}]',
        '"timeline": [{"frame_id": 宫格编号, "visual": "这一帧的人、物体及动作"}]',
    )
    comment = ('"comment": {"decision": "comment|skip", "text": "评论正文；skip 时留空"}, '
               if with_comment else "")
    return (
        "只输出一个 JSON 对象，不要代码块、不要别的文字：\n{" + comment + fields + "}\n"
        "frame_id 必须是清单里的整数编号，至少一条、每帧最多一条，不写秒数。visual 每条160字以内，"
        "只描述人、物体、动作或界面布局，不抄写或猜测字幕、菜单、名字、数字。"
        "画面文字由程序单独保存，本地OCR未确认的文字不得补全，也不能混入印象或评论。"
        "只收到截图，audio 固定写「听不到」。"
    )


class VideoTimelineError(ValueError):
    pass


def _strip_fence(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("```json") and text.endswith("```"):
        return text[7:-3].strip()
    if text.startswith("```") and text.endswith("```"):
        return text[3:-3].strip()
    return text


def clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse_segment_watch(raw: str, offset: float = 0.0, length: float | None = None, *,
                        with_comment: bool = False) -> dict[str, Any]:
    """校验一段的输出；秒数换成原视频里的秒数。超出这段长度的条目算模型编的，整段作废。

    ``with_comment``：这段还要顺便写评论。评论格式不对只丢评论（``comment`` 为 None），记录照收；
    想评论是正文，选了 skip 是空字符串。
    """
    try:
        value = json.loads(_strip_fence(raw))
    except (TypeError, ValueError) as exc:
        raise VideoTimelineError("invalid_json") from exc
    keys = SEGMENT_KEYS | ({"comment"} if with_comment else set())
    if not isinstance(value, dict) or not SEGMENT_KEYS <= set(value) <= keys:
        raise VideoTimelineError("segment_schema_mismatch")
    comment = None
    if with_comment and isinstance(value.get("comment"), dict):
        from .content_protocol import ContentProtocolError, parse_proactive_comment
        try:
            comment = parse_proactive_comment(json.dumps(value["comment"], ensure_ascii=False))["text"]
        except ContentProtocolError:
            comment = None
    timeline = value["timeline"]
    if not isinstance(timeline, list) or not timeline:
        raise VideoTimelineError("timeline_empty")
    if len(timeline) > MAX_ENTRIES:
        raise VideoTimelineError("timeline_too_long")
    entries = []
    for item in timeline:
        if not isinstance(item, dict) or set(item) != {"at", "text"}:
            raise VideoTimelineError("timeline_entry_mismatch")
        at, text = item["at"], item["text"]
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0:
            raise VideoTimelineError("timeline_at_invalid")
        # 模型标秒数常有一两秒误差，留 5 秒余量。
        if length is not None and at > length + 5:
            raise VideoTimelineError("timeline_at_outside_segment")
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > ENTRY_LIMIT:
            raise VideoTimelineError("timeline_text_invalid")
        entries.append((float(at) + float(offset or 0), text.strip()))
    entries.sort(key=lambda entry: entry[0])
    impression = value["impression"]
    if not isinstance(impression, str) or not impression.strip() or len(impression.strip()) > IMPRESSION_LIMIT:
        raise VideoTimelineError("impression_invalid")
    audio = value["audio"]
    if not isinstance(audio, str) or not audio.strip() or len(audio.strip()) > AUDIO_LIMIT:
        raise VideoTimelineError("audio_invalid")
    if not isinstance(value["sensitive"], bool):
        raise VideoTimelineError("sensitive_must_be_bool")
    hint = value["comment_hint"]
    if not isinstance(hint, str) or not hint.strip() or len(hint.strip()) > HINT_LIMIT:
        raise VideoTimelineError("comment_hint_invalid")
    out = {"timeline": entries, "impression": impression.strip(), "audio": audio.strip(),
           "sensitive": value["sensitive"], "comment_hint": hint.strip()}
    if with_comment:
        out["comment"] = comment
    return out


def parse_storyboard_watch(raw, board, offset=0.0, length=None, *, with_comment=False):
    """严格按采样帧绑定秒数；OCR原文直接入档，模型不能重写文字或重分配文字时间。"""
    try:
        value = json.loads(_strip_fence(raw))
    except (TypeError, ValueError) as exc:
        raise VideoTimelineError("invalid_json") from exc
    if not isinstance(value, dict) or not isinstance(value.get("timeline"), list):
        raise VideoTimelineError("segment_schema_mismatch")
    times = board.get("selected_timestamps") or []
    if not times or any(
        isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at)
        or at < 0 or (length is not None and at > length)
        for at in times
    ):
        raise VideoTimelineError("storyboard_times_invalid")
    mapped, used = [], set()
    for item in value["timeline"]:
        if not isinstance(item, dict) or set(item) != {"frame_id", "visual"}:
            raise VideoTimelineError("storyboard_entry_mismatch")
        frame_id = item["frame_id"]
        if isinstance(frame_id, bool) or not isinstance(frame_id, int) or not 1 <= frame_id <= len(times):
            raise VideoTimelineError("storyboard_frame_invalid")
        if frame_id in used:
            raise VideoTimelineError("storyboard_frame_duplicate")
        used.add(frame_id)
        visual = item["visual"]
        if not isinstance(visual, str) or not visual.strip() or len(visual.strip()) > ENTRY_LIMIT:
            raise VideoTimelineError("timeline_text_invalid")
        # 字幕/标签引用不允许绕过OCR；保留同条描述中独立的画面句，避免连动作一起丢掉。
        text_markers = r'字幕|字样|写着|写有|文字|标题|标注|标签|菜单|名称|名为|叫做|屏幕显示|[“”「」"A-Za-z0-9]'
        if re.search(text_markers, visual):
            visual = "。".join(part.strip() for part in re.split(r"[；;。]", visual)
                              if part.strip() and not re.search(text_markers, part))
            visual = visual or "画面含文字，原文见本地OCR记录"
        mapped.append({"at": times[frame_id - 1], "text": visual})
    # 复用旧解析器验证其余字段，保持视频直读/逐帧的原有契约。
    value["timeline"] = mapped
    out = parse_segment_watch(json.dumps(value, ensure_ascii=False), offset, length, with_comment=with_comment)
    out["audio"] = "听不到"
    ocr = board.get("ocr") or {}
    if len(ocr.get("frames") or []) != len(times):
        raise VideoTimelineError("storyboard_ocr_frames_mismatch")
    for frame_id, frame in enumerate(ocr["frames"], start=1):
        if type(frame.get("frame_id")) is not int or frame["frame_id"] != frame_id:
            raise VideoTimelineError("storyboard_ocr_frame_invalid")
        at = float(times[frame_id - 1]) + float(offset or 0)
        if not isinstance(frame.get("texts"), list):
            raise VideoTimelineError("storyboard_ocr_text_invalid")
        for text in frame["texts"]:
            if not isinstance(text, str) or not text.strip() or len(text) > ENTRY_LIMIT:
                raise VideoTimelineError("storyboard_ocr_text_invalid")
            out["timeline"].append((at, "画面文字（本地OCR）：「" + text + "」"))
        if frame.get("status") not in {"ok", "no_text"}:
            out["timeline"].append((at, ("其余画面文字" if frame["texts"] else "画面文字") + "：无法可靠辨认"))
    out["timeline"].sort(key=lambda entry: entry[0])
    return out


# 分区本身就是新闻时事的，不管模型怎么判都算（宁可少评）。
SENSITIVE_PARTITIONS = {"资讯", "热点", "环球", "社会", "时政"}


def sensitive_partition(tname: str) -> bool:
    return str(tname or "").strip() in SENSITIVE_PARTITIONS


def heard_nothing(audio: str) -> bool:
    return any(word in str(audio or "") for word in NO_AUDIO_WORDS)


def format_timeline(entries) -> str:
    return "\n".join(f"[{clock(at)}] {text}" for at, text in entries)


def format_watch(impression: str, entries, *, partial: bool = False) -> str:
    """存档文本：印象在前，时间线在后。"""
    head = (PARTIAL_MARK if partial else "") + str(impression or "").strip()
    body = format_timeline(entries)
    return f"{head}\n\n{TIMELINE_MARK}\n{body}" if body else head


def watch_digest(text: str, limit: int) -> str:
    """日志、记忆用的那一段：有时间线就只取前面的印象，旧格式的概括照旧截前面。"""
    text = str(text or "")
    if TIMELINE_MARK in text:
        text = text.split(TIMELINE_MARK, 1)[0].strip()
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def format_subtitle_lines(lines, start: float = 0.0, end: float | None = None, limit: int = 1500) -> str:
    """带时间的字幕，只取 [start, end) 里的，按行截到 limit 字。"""
    out, used = [], 0
    for line in lines or ():
        at = float(line.get("from", 0) or 0)
        if at < start or (end is not None and at >= end):
            continue
        row = f"[{clock(at)}] {line.get('content', '')}"
        if used + len(row) > limit:
            out.append("…（后面还有，没放进来）")
            break
        out.append(row)
        used += len(row) + 1
    return "\n".join(out)
