"""Strict video-evaluation schema and preference-signal normalization."""
from __future__ import annotations

import json
import math
from typing import Any

from .domain.dynamic_ideas import DEFAULT_KIND, KIND_ALIASES, VIDEO_KINDS as DYNAMIC_KINDS


PREFERENCE_TYPES = {
    "up", "partition", "work", "character", "food", "theme", "music",
    "game", "technology", "activity", "location", "other",
}
PREFERENCE_POLARITIES = {"like", "dislike", "fatigue", "curious"}
MOODS = {"开心", "平静", "无聊", "感动", "好笑", "震撼", "困惑"}
EVALUATION_KEYS = {
    "score", "score_reason", "mood", "review", "want_follow",
    "recommend_owner", "recommend_reason", "partition", "preference_signals",
    "search_keywords",
}
# 看完想发动态的标注是后加字段；旧模型输出缺省时按“不想发”处理，缺类型按「分享」。
# impression 也是后加的：缺省留空，记忆淡忘前由每晚清算补一句。
# comment 以前要模型顺手写一条「想评论什么」，但真发的评论是另外一次调用写的，这格只进日志；
# 现在不再要，旧输出带着也认，结果里照旧有这个键（空字符串）。
OPTIONAL_EVALUATION_KEYS = {"want_dynamic", "dynamic_angle", "dynamic_kind", "impression", "interactions", "comment"}
# 看完想做的互动，模型自己选（评分只是参考）；关注用 want_follow。
INTERACTIONS = ("like", "coin", "favorite", "comment")
SIGNAL_KEYS = {"type", "value", "polarity", "strength", "evidence"}


class VideoEvaluationError(ValueError):
    pass


def _text(value: Any, field: str, limit: int, *, empty=True) -> str:
    if not isinstance(value, str):
        raise VideoEvaluationError(f"{field}_must_be_string")
    value = value.strip()
    if not empty and not value:
        raise VideoEvaluationError(f"{field}_empty")
    if len(value) > limit:
        raise VideoEvaluationError(f"{field}_too_long")
    return value


def parse_dynamic_kind(value: dict, want_dynamic: bool, kinds) -> str:
    """想发时类型必须是 ``kinds`` 之一（没写按「分享」）；不想发时必须留空。"""
    kind = value.get("dynamic_kind", "")
    if not isinstance(kind, str):
        raise ValueError("dynamic_kind_must_be_string")
    kind = kind.strip()
    if not want_dynamic:
        if kind:
            raise ValueError("dynamic_kind_without_wish")
        return ""
    kind = KIND_ALIASES.get(kind, kind) or DEFAULT_KIND
    if kind not in kinds:
        raise ValueError("invalid_dynamic_kind")
    return kind


def parse_interactions(value: Any) -> list[str]:
    """``interactions`` 必须是 ``INTERACTIONS`` 里的字符串、不重复；不合格抛 ``VideoEvaluationError``。"""
    if not isinstance(value, list) or len(value) > len(INTERACTIONS):
        raise VideoEvaluationError("interactions_must_be_list")
    chosen = []
    for item in value:
        if not isinstance(item, str) or item.strip() not in INTERACTIONS:
            raise VideoEvaluationError("invalid_interaction")
        if item.strip() in chosen:
            raise VideoEvaluationError("duplicate_interaction")
        chosen.append(item.strip())
    return chosen


def parse_video_evaluation(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise VideoEvaluationError("invalid_json") from exc
    if not isinstance(value, dict) or not (
        EVALUATION_KEYS <= set(value) <= EVALUATION_KEYS | OPTIONAL_EVALUATION_KEYS
    ):
        raise VideoEvaluationError("evaluation_schema_mismatch")

    score = value.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise VideoEvaluationError("score_must_be_number")
    score = round(float(score), 1)
    if not math.isfinite(score) or not 1.0 <= score <= 10.0:
        raise VideoEvaluationError("score_out_of_range")
    score_reason = _text(value.get("score_reason"), "score_reason", 120, empty=False)
    comment = _text(value.get("comment", ""), "comment", 80)
    mood = _text(value.get("mood"), "mood", 10, empty=False)
    if mood not in MOODS:
        raise VideoEvaluationError("invalid_mood")
    review = _text(value.get("review"), "review", 180, empty=False)
    impression = _text(value.get("impression", ""), "impression", 60)
    partition = _text(value.get("partition"), "partition", 30)
    want_follow = value.get("want_follow")
    recommend_owner = value.get("recommend_owner")
    if not isinstance(want_follow, bool) or not isinstance(recommend_owner, bool):
        raise VideoEvaluationError("boolean_field_invalid")
    recommend_reason = _text(
        value.get("recommend_reason"), "recommend_reason", 80
    )
    if recommend_owner and not recommend_reason:
        raise VideoEvaluationError("owner_recommendation_needs_reason")
    if not recommend_owner and recommend_reason:
        raise VideoEvaluationError("owner_reason_without_recommendation")
    want_dynamic = value.get("want_dynamic", False)
    if not isinstance(want_dynamic, bool):
        raise VideoEvaluationError("boolean_field_invalid")
    dynamic_angle = _text(value.get("dynamic_angle", ""), "dynamic_angle", 80)
    if want_dynamic and not dynamic_angle:
        raise VideoEvaluationError("dynamic_wish_needs_angle")
    if not want_dynamic and dynamic_angle:
        raise VideoEvaluationError("dynamic_angle_without_wish")
    try:
        dynamic_kind = parse_dynamic_kind(value, want_dynamic, DYNAMIC_KINDS)
    except ValueError as exc:
        raise VideoEvaluationError(str(exc)) from exc

    # 互动选择不合格只作废这一项（这次不互动），评分和感想照常记；没写也是不互动
    interactions_error = ""
    try:
        interactions = parse_interactions(value.get("interactions", []))
    except VideoEvaluationError as exc:
        interactions, interactions_error = [], str(exc)

    raw_signals = value.get("preference_signals")
    if not isinstance(raw_signals, list) or len(raw_signals) > 5:
        raise VideoEvaluationError("preference_signals_schema_mismatch")
    signals = []
    seen_signals = set()
    for item in raw_signals:
        if not isinstance(item, dict) or set(item) != SIGNAL_KEYS:
            raise VideoEvaluationError("preference_signal_schema_mismatch")
        signal_type = str(item.get("type") or "").strip().lower()
        polarity = str(item.get("polarity") or "").strip().lower()
        if signal_type not in PREFERENCE_TYPES:
            raise VideoEvaluationError("invalid_preference_type")
        if polarity not in PREFERENCE_POLARITIES:
            raise VideoEvaluationError("invalid_preference_polarity")
        signal_value = _text(item.get("value"), "preference_value", 50, empty=False)
        strength = item.get("strength")
        if isinstance(strength, bool) or not isinstance(strength, (int, float)):
            raise VideoEvaluationError("preference_strength_must_be_number")
        strength = round(float(strength), 2)
        if not 0.0 <= strength <= 1.0:
            raise VideoEvaluationError("preference_strength_out_of_range")
        evidence = _text(item.get("evidence"), "preference_evidence", 100, empty=False)
        dedupe_key = (signal_type, signal_value.casefold(), polarity)
        if dedupe_key in seen_signals:
            continue
        seen_signals.add(dedupe_key)
        signals.append({
            "type": signal_type, "value": signal_value, "polarity": polarity,
            "strength": strength, "evidence": evidence,
        })

    raw_keywords = value.get("search_keywords")
    if not isinstance(raw_keywords, list) or len(raw_keywords) > 5:
        raise VideoEvaluationError("search_keywords_schema_mismatch")
    keywords = []
    for item in raw_keywords:
        keyword = _text(item, "search_keyword", 40, empty=False)
        if keyword not in keywords:
            keywords.append(keyword)

    return {
        "score": score, "score_reason": score_reason, "comment": comment,
        "mood": mood, "review": review, "impression": impression, "want_follow": want_follow,
        "recommend_owner": recommend_owner, "recommend_reason": recommend_reason,
        "want_dynamic": want_dynamic, "dynamic_angle": dynamic_angle,
        "dynamic_kind": dynamic_kind, "partition": partition, "preference_signals": signals,
        "search_keywords": keywords, "interactions": interactions, "interactions_error": interactions_error,
    }


VIDEO_EVALUATION_SCHEMA_PROMPT = """只输出一个完整JSON对象，不要Markdown、解释或额外字段，按这个顺序写（先写感受，再打分）：
{"review":"看完心里的话","mood":"开心|平静|无聊|感动|好笑|震撼|困惑","impression":"一句话印象","score":7.0,"score_reason":"给自己的一句备注","interactions":["like"],"want_follow":false,"recommend_owner":false,"recommend_reason":"","want_dynamic":false,"dynamic_angle":"","dynamic_kind":"","partition":"实际分区","preference_signals":[{"type":"up|partition|work|character|food|theme|music|game|technology|activity|location|other","value":"具体对象","polarity":"like|dislike|fatigue|curious","strength":0.0,"evidence":"本视频中的具体依据"}],"search_keywords":["以后真能拿去B站搜索的具体词"]}
所有字符串里都不要出现英文双引号"：引用原话、标题、梗或弹幕一律用「」；字符串里也不要换行。出现英文双引号会让整段JSON无法解析，整条评价作废。
字数有上限，超了整条存不下、会作废，所以宁可写短：score_reason 一句、60字以内，review 两三句、100字以内，impression 一句、30字以内，recommend_reason 50字以内，dynamic_angle 50字以内，喜好信号的value 20字、evidence 50字以内，搜索词20字以内；整段JSON不超过600字。写长了整条评价会作废。
want_dynamic 为 false 时 dynamic_angle、dynamic_kind 都留空；为 true 时 dynamic_kind 填吐槽|段子|分享|转发之一。
partition 写视频实际属于的分区。
preference_signals 和 search_keywords 是给以后挑视频用的，各最多 5 个，宁缺毋滥：写具体的作品、人物、作者、菜名、歌手、技术、活动或主题，「动漫」「游戏」这种太宽，拿去搜什么都有。
喜好信号只记这次看出来的感受，一次高分不等于长期爱好；也别把人设或主人的爱好照抄成自己的，那样以后挑视频会越挑越偏。"""
