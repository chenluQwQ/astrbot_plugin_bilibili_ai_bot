"""Strict video-evaluation schema and preference-signal normalization."""
from __future__ import annotations

import json
from typing import Any


PREFERENCE_TYPES = {
    "up", "partition", "work", "character", "food", "theme", "music",
    "game", "technology", "activity", "location", "other",
}
PREFERENCE_POLARITIES = {"like", "dislike", "fatigue", "curious"}
MOODS = {"开心", "平静", "无聊", "感动", "好笑", "震撼", "困惑"}
EVALUATION_KEYS = {
    "score", "score_reason", "comment", "mood", "review", "want_follow",
    "recommend_owner", "recommend_reason", "partition", "preference_signals",
    "search_keywords",
}
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
    if not isinstance(value, dict) or set(value) != EVALUATION_KEYS:
        raise VideoEvaluationError("evaluation_schema_mismatch")

    score = value.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise VideoEvaluationError("score_must_be_number")
    score = round(float(score), 1)
    if not 1.0 <= score <= 10.0:
        raise VideoEvaluationError("score_out_of_range")
    score_reason = _text(value.get("score_reason"), "score_reason", 120, empty=False)
    comment = _text(value.get("comment"), "comment", 80)
    mood = _text(value.get("mood"), "mood", 10, empty=False)
    if mood not in MOODS:
        raise VideoEvaluationError("invalid_mood")
    review = _text(value.get("review"), "review", 180, empty=False)
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
        "mood": mood, "review": review, "want_follow": want_follow,
        "recommend_owner": recommend_owner, "recommend_reason": recommend_reason,
        "partition": partition, "preference_signals": signals,
        "search_keywords": keywords,
    }


VIDEO_EVALUATION_SCHEMA_PROMPT = """只输出一个完整JSON对象，不要Markdown、解释或额外字段：
{"score":7.0,"score_reason":"评分理由","comment":"评论区短句或空字符串","mood":"开心|平静|无聊|感动|好笑|震撼|困惑","review":"个人感想","want_follow":false,"recommend_owner":false,"recommend_reason":"","partition":"实际分区","preference_signals":[{"type":"up|partition|work|character|food|theme|music|game|technology|activity|location|other","value":"具体对象","polarity":"like|dislike|fatigue|curious","strength":0.0,"evidence":"本视频中的具体依据"}],"search_keywords":["以后真能拿去B站搜索的具体词"]}
最多5个喜好信号和5个搜索词，宁缺毋滥。不要只写“动漫”“游戏”这种过宽词；优先作品、人物、作者、菜名、歌手、技术、活动或具体主题。一次高分只代表本次信号，不要声称已形成永久喜好。"""
