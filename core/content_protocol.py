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


def parse_share_watch_decision(raw: str) -> dict[str, Any]:
    value = _object(raw)
    if set(value) != {"decision"}:
        raise ContentProtocolError("share_watch_schema_mismatch")
    decision = str(value.get("decision") or "").strip().lower()
    if decision not in {"watch", "skip"}:
        raise ContentProtocolError("invalid_share_watch_decision")
    return {"decision": decision}


def parse_share_reaction(raw: str) -> dict[str, Any]:
    value = _object(raw)
    if set(value) != {"decision", "text"}:
        raise ContentProtocolError("share_reaction_schema_mismatch")
    decision = str(value.get("decision") or "").strip().lower()
    if decision not in {"comment", "skip"}:
        raise ContentProtocolError("invalid_share_reaction_decision")
    text = _public_text(value.get("text"), field="text", limit=60, required=decision == "comment")
    if decision == "skip" and text:
        raise ContentProtocolError("skipped_share_reaction_has_text")
    return {"decision": decision, "text": text}


def parse_memory_evaluation(raw: str, expected_ids) -> dict[str, int]:
    """Importance scores keyed by rpid; any malformed item rejects the batch.

    A memory the model leaves out is simply absent from the result, so the
    caller keeps it rather than guessing a score.
    """
    value = _object(raw)
    if set(value) != {"items"} or not isinstance(value["items"], list):
        raise ContentProtocolError("memory_evaluation_schema_mismatch")
    expected = {str(item) for item in expected_ids}
    scores: dict[str, int] = {}
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != {"rpid", "importance"}:
            raise ContentProtocolError("memory_evaluation_item_mismatch")
        rpid = item["rpid"]
        if not isinstance(rpid, str) or rpid not in expected:
            raise ContentProtocolError("memory_evaluation_unknown_rpid")
        if rpid in scores:
            raise ContentProtocolError("memory_evaluation_duplicate_rpid")
        importance = item["importance"]
        if isinstance(importance, bool) or not isinstance(importance, int) or not 1 <= importance <= 10:
            raise ContentProtocolError("memory_evaluation_invalid_importance")
        scores[rpid] = importance
    return scores


DYNAMIC_IMAGE_CHOICES = {"none", "generate", "video_cover"}


def parse_dynamic_content(
    raw: str, *, allow_search: bool = False, allow_video_cover: bool = False,
) -> dict[str, Any]:
    """Validate a dynamic decision.

    ``search`` only asks the host to run a read-only web query and feed the
    result back; it never publishes anything.  The legacy
    ``need_image``/``image_prompt`` envelope is still accepted.
    """
    value = _object(raw)
    legacy = {"decision", "text", "need_image", "image_prompt"}
    current = {"decision", "text", "search_query", "image", "image_prompt"}
    if set(value) == legacy:
        need_image = value.get("need_image")
        if not isinstance(need_image, bool):
            raise ContentProtocolError("need_image_must_be_boolean")
        value = {
            "decision": value.get("decision"), "text": value.get("text"),
            "search_query": "", "image": "generate" if need_image else "none",
            "image_prompt": value.get("image_prompt"),
        }
    elif set(value) != current:
        raise ContentProtocolError("dynamic_schema_mismatch")
    decision = str(value.get("decision") or "").strip().lower()
    if decision not in {"post", "skip", "search"}:
        raise ContentProtocolError("invalid_dynamic_decision")
    if decision == "search" and not allow_search:
        raise ContentProtocolError("dynamic_search_not_allowed")
    text = _public_text(value.get("text"), field="text", limit=200, required=decision == "post")
    search_query = _public_text(
        value.get("search_query"), field="search_query", limit=80,
        required=decision == "search",
    )
    image = str(value.get("image") or "").strip().lower()
    if image not in DYNAMIC_IMAGE_CHOICES:
        raise ContentProtocolError("invalid_dynamic_image")
    if image == "video_cover" and not allow_video_cover:
        raise ContentProtocolError("video_cover_not_available")
    image_prompt = _public_text(
        value.get("image_prompt"), field="image_prompt", limit=500,
        required=decision == "post" and image == "generate",
    )
    if decision != "post" and (text or image != "none" or image_prompt):
        raise ContentProtocolError(f"{decision}_dynamic_has_content")
    if decision != "search" and search_query:
        raise ContentProtocolError("search_query_without_search")
    if image != "generate" and image_prompt:
        raise ContentProtocolError("image_prompt_without_image")
    return {
        "decision": decision, "text": text, "search_query": search_query,
        "image": image, "need_image": image != "none", "image_prompt": image_prompt,
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

SHARE_WATCH_SCHEMA_PROMPT = (
    '只输出完整JSON：{"decision":"watch|skip"}。'
    "按你自己的兴趣和这段对话判断：真的想看才用watch，不感兴趣或没必要就用skip。"
)

SHARE_REACTION_SCHEMA_PROMPT = (
    '只输出完整JSON：{"decision":"comment|skip","text":"一句评价"}。'
    "看完有具体感受才用comment，text是一句口语化的短评；说不出具体内容就用skip且text留空。"
)

TOOL_WATCH_FOLLOWUP_SCHEMA_PROMPT = (
    '只输出完整JSON：{"decision":"comment|skip","text":"一句话"}。'
    "text是一句口语化的短句，60字以内；实在说不出来才用skip且text留空。"
)

DYNAMIC_SCHEMA_PROMPT = (
    '只输出完整JSON：{"decision":"post|skip|search","text":"动态正文",'
    '"search_query":"","image":"none|generate|video_cover","image_prompt":""}。'
    "没有真实且具体的发布动机就用skip；skip和search时text、image_prompt留空、image为none。"
    "search只在可用时使用，search_query写一条具体的搜索词，查到的资料会再交给你决定。"
    "image为generate时image_prompt写画面描述，其余情况留空。"
)
