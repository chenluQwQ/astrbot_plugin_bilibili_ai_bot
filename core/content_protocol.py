"""Strict envelopes for proactive public content.

These messages do not carry relationship side effects, but they still cross a
real platform boundary.  Callers must never salvage prose from invalid JSON.
"""
from __future__ import annotations

import json
import re
from typing import Any


class ContentProtocolError(ValueError):
    """Generated content is incomplete, unsafe, or outside its schema."""


def _object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ContentProtocolError("invalid_json") from exc
    if not isinstance(value, dict):
        raise ContentProtocolError("not_an_object")
    return value


def _public_text(value: Any, *, field: str, limit: int, required: bool) -> str:
    if not isinstance(value, str):
        raise ContentProtocolError(f"{field}_must_be_string")
    text = re.sub(r"[\r\n]+", " ", value).strip()
    if required and not text:
        raise ContentProtocolError(f"{field}_required")
    if len(text) > limit:
        raise ContentProtocolError(f"{field}_too_long")
    lowered = text.lower()
    if any(marker in lowered for marker in ("```", "<think", "<analysis", "<internal")):
        raise ContentProtocolError(f"{field}_contains_internal_markup")
    if re.search(
        r'(?i)["\']?(?:decision|signals|score_delta|tool_request|need_image)'
        r'["\']?\s*[:：]', text,
    ):
        raise ContentProtocolError(f"{field}_contains_internal_fields")
    return text


def parse_proactive_comment(raw: str) -> dict[str, Any]:
    value = _object(raw)
    if set(value) != {"decision", "text"}:
        raise ContentProtocolError("comment_schema_mismatch")
    decision = str(value.get("decision") or "").strip().lower()
    if decision not in {"comment", "skip"}:
        raise ContentProtocolError("invalid_comment_decision")
    text = _public_text(value.get("text"), field="text", limit=80, required=decision == "comment")
    if decision == "skip" and text:
        raise ContentProtocolError("skipped_comment_has_text")
    return {"decision": decision, "text": text}


def parse_recommendation(raw: str) -> dict[str, Any]:
    value = _object(raw)
    if set(value) != {"decision", "text"}:
        raise ContentProtocolError("recommendation_schema_mismatch")
    decision = str(value.get("decision") or "").strip().lower()
    if decision not in {"share", "skip"}:
        raise ContentProtocolError("invalid_recommendation_decision")
    text = _public_text(value.get("text"), field="text", limit=80, required=decision == "share")
    if decision == "skip" and text:
        raise ContentProtocolError("skipped_recommendation_has_text")
    return {"decision": decision, "text": text}


def parse_dynamic_content(raw: str) -> dict[str, Any]:
    value = _object(raw)
    expected = {"decision", "text", "need_image", "image_prompt"}
    if set(value) != expected:
        raise ContentProtocolError("dynamic_schema_mismatch")
    decision = str(value.get("decision") or "").strip().lower()
    if decision not in {"post", "skip"}:
        raise ContentProtocolError("invalid_dynamic_decision")
    text = _public_text(value.get("text"), field="text", limit=200, required=decision == "post")
    need_image = value.get("need_image")
    if not isinstance(need_image, bool):
        raise ContentProtocolError("need_image_must_be_boolean")
    image_prompt = _public_text(
        value.get("image_prompt"), field="image_prompt", limit=500,
        required=decision == "post" and need_image,
    )
    if decision == "skip" and (text or need_image or image_prompt):
        raise ContentProtocolError("skipped_dynamic_has_content")
    if not need_image and image_prompt:
        raise ContentProtocolError("image_prompt_without_image")
    return {
        "decision": decision, "text": text,
        "need_image": need_image, "image_prompt": image_prompt,
    }


def parse_bangumi_evaluation(raw: str) -> dict[str, Any]:
    """Validate a bangumi evaluation before any embedded comment is sent."""
    value = _object(raw)
    expected = {"score", "comment", "mood", "review", "want_continue"}
    if set(value) != expected:
        raise ContentProtocolError("bangumi_schema_mismatch")
    score = value.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ContentProtocolError("bangumi_score_must_be_number")
    score = int(score)
    if not 1 <= score <= 10:
        raise ContentProtocolError("bangumi_score_out_of_range")
    comment = _public_text(
        value.get("comment"), field="comment", limit=80, required=False
    )
    mood = _public_text(value.get("mood"), field="mood", limit=24, required=True)
    review = _public_text(
        value.get("review"), field="review", limit=160, required=False
    )
    want_continue = value.get("want_continue")
    if not isinstance(want_continue, bool):
        raise ContentProtocolError("want_continue_must_be_boolean")
    return {
        "score": score,
        "comment": comment,
        "mood": mood,
        "review": review,
        "want_continue": want_continue,
    }


PROACTIVE_COMMENT_SCHEMA_PROMPT = (
    '只输出完整JSON：{"decision":"comment|skip","text":"评论正文"}。'
    "真有一个具体细节想回应才用comment；不想硬评或资料不足用skip且text留空。"
)

RECOMMENDATION_SCHEMA_PROMPT = (
    '只输出完整JSON：{"decision":"share|skip","text":"随手分享附言"}。'
    "仍然觉得适合分享才用share；理由牵强或写不自然就用skip且text留空。"
)

DYNAMIC_SCHEMA_PROMPT = (
    '只输出完整JSON：{"decision":"post|skip","text":"动态正文",'
    '"need_image":false,"image_prompt":""}。没有真实且具体的发布动机就用skip，'
    "此时text和image_prompt留空、need_image为false。"
)
