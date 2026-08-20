"""Strict schema for every model-generated conversational reply.

Only ``reply`` may cross the platform boundary.  Everything else is internal
metadata and must pass validation before callers may send or commit state.
"""
from __future__ import annotations

import json
import re
from typing import Any


DECISIONS = {"reply", "ignore", "observe"}
INTERACTION_TYPES = {
    "normal", "advice", "correction", "criticism", "teasing", "attack",
    "threat", "other",
}
FEEDBACK_TYPES = {
    "none", "suggestion", "correction", "criticism", "teasing", "attack",
    "threat", "other",
}
TOP_LEVEL_KEYS = {
    "decision", "reply", "score_delta", "impression", "user_facts",
    "signals", "tool_request",
}
SIGNAL_KEYS = {
    "interaction_type", "feedback_type", "attack_level", "feedback_topic",
    "reflection_candidate", "confidence",
}
REFLECTION_KEYS = {"event", "possible_mistake", "next_time"}
TOOL_REQUEST_KEYS = {"name", "query"}


class ReplyProtocolError(ValueError):
    """The model returned data that must never be sent or committed."""


def _short_text(value: Any, field: str, limit: int, *, allow_empty=True) -> str:
    if not isinstance(value, str):
        raise ReplyProtocolError(f"{field}_must_be_string")
    text = value.strip()
    if not allow_empty and not text:
        raise ReplyProtocolError(f"{field}_must_not_be_empty")
    if len(text) > limit:
        raise ReplyProtocolError(f"{field}_too_long")
    return text


def _reply_limit(channel: str) -> int:
    return {"live": 60, "comment": 180, "private": 1200}.get(channel, 500)


def _validate_public_reply(text: str) -> None:
    lowered = text.lower()
    forbidden = (
        "```", "<think", "</think", "<analysis", "</analysis",
        "<internal", "</internal",
    )
    if any(marker in lowered for marker in forbidden):
        raise ReplyProtocolError("reply_contains_internal_markup")
    if re.search(
        r'(?i)["\']?(?:signals|score_delta|tool_request|feedback_type|attack_level)'
        r'["\']?\s*[:：]',
        text,
    ):
        raise ReplyProtocolError("reply_contains_internal_fields")


def _validate_signals(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != SIGNAL_KEYS:
        raise ReplyProtocolError("signals_schema_mismatch")
    interaction = str(value.get("interaction_type") or "").strip().lower()
    feedback = str(value.get("feedback_type") or "").strip().lower()
    if interaction not in INTERACTION_TYPES:
        raise ReplyProtocolError("invalid_interaction_type")
    if feedback not in FEEDBACK_TYPES:
        raise ReplyProtocolError("invalid_feedback_type")
    attack_level = value.get("attack_level")
    if isinstance(attack_level, bool) or not isinstance(attack_level, int):
        raise ReplyProtocolError("attack_level_must_be_integer")
    if not 0 <= attack_level <= 3:
        raise ReplyProtocolError("attack_level_out_of_range")
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ReplyProtocolError("confidence_must_be_number")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise ReplyProtocolError("confidence_out_of_range")
    topic = value.get("feedback_topic")
    if topic is not None:
        topic = _short_text(topic, "feedback_topic", 80, allow_empty=False)
    reflection = value.get("reflection_candidate")
    if reflection is not None:
        if feedback not in {"suggestion", "correction", "criticism"}:
            raise ReplyProtocolError("reflection_not_supported_for_feedback_type")
        if not isinstance(reflection, dict) or set(reflection) != REFLECTION_KEYS:
            raise ReplyProtocolError("reflection_candidate_schema_mismatch")
        reflection = {
            key: _short_text(reflection[key], f"reflection_{key}", 160, allow_empty=False)
            for key in ("event", "possible_mistake", "next_time")
        }
    return {
        "interaction_type": interaction,
        "feedback_type": feedback,
        "attack_level": attack_level,
        "feedback_topic": topic,
        "reflection_candidate": reflection,
        "confidence": confidence,
    }


def parse_reply_envelope(
    raw: str,
    *,
    channel: str,
    allowed_tools: set[str] | None = None,
    allow_tool_request: bool = False,
) -> dict[str, Any]:
    """Parse and validate one complete JSON object; never salvage partial text."""
    text = str(raw or "").strip()
    if not text:
        raise ReplyProtocolError("empty_model_output")
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ReplyProtocolError("invalid_json") from exc
    if not isinstance(value, dict) or set(value) != TOP_LEVEL_KEYS:
        raise ReplyProtocolError("reply_schema_mismatch")

    decision = str(value.get("decision") or "").strip().lower()
    if decision not in DECISIONS:
        raise ReplyProtocolError("invalid_decision")
    reply = _short_text(
        value.get("reply"), "reply", _reply_limit(channel),
        allow_empty=decision != "reply",
    )
    _validate_public_reply(reply)

    delta = value.get("score_delta")
    if isinstance(delta, bool) or not isinstance(delta, (int, float)):
        raise ReplyProtocolError("score_delta_must_be_number")
    if float(delta) != int(delta):
        raise ReplyProtocolError("score_delta_must_be_integer")
    delta = int(delta)
    if not -5 <= delta <= 2:
        raise ReplyProtocolError("score_delta_out_of_range")

    impression = _short_text(value.get("impression"), "impression", 160)
    facts = value.get("user_facts")
    if not isinstance(facts, list) or len(facts) > 5:
        raise ReplyProtocolError("user_facts_schema_mismatch")
    user_facts = [
        _short_text(item, "user_fact", 120, allow_empty=False) for item in facts
    ]
    signals = _validate_signals(value.get("signals"))

    tool_request = value.get("tool_request")
    if not isinstance(tool_request, dict) or set(tool_request) != TOOL_REQUEST_KEYS:
        raise ReplyProtocolError("tool_request_schema_mismatch")
    tool_name = str(tool_request.get("name") or "").strip().lower()
    tool_query = _short_text(tool_request.get("query"), "tool_query", 100)
    tools = {str(name).strip().lower() for name in (allowed_tools or set())}
    if not allow_tool_request:
        tools = set()
    if tool_name != "none" and tool_name not in tools:
        raise ReplyProtocolError("tool_not_allowed")
    if tool_name == "none" and tool_query:
        raise ReplyProtocolError("none_tool_must_not_have_query")

    if decision != "reply":
        if reply or delta or impression or user_facts or tool_name != "none":
            raise ReplyProtocolError("silent_decision_has_side_effects")
    elif not reply:
        raise ReplyProtocolError("reply_decision_has_no_reply")

    return {
        "decision": decision,
        "reply": reply,
        "score_delta": delta,
        "impression": impression,
        "user_facts": user_facts,
        "signals": signals,
        "tool_request": {"name": tool_name, "query": tool_query},
        "permanent_memory": "",
        "_protocol_validated": True,
    }


def reply_schema_instruction(*, tools: list[str] | None = None) -> str:
    tool_names = ["none", *(tools or [])]
    tool_union = "|".join(dict.fromkeys(tool_names))
    return (
        "只输出一个完整JSON对象，不要Markdown、解释或额外字段。格式必须严格为：\n"
        '{"decision":"reply|ignore|observe","reply":"唯一可发送文本",'
        '"score_delta":0,"impression":"","user_facts":[],'
        '"signals":{"interaction_type":"normal|advice|correction|criticism|teasing|attack|threat|other",'
        '"feedback_type":"none|suggestion|correction|criticism|teasing|attack|threat|other",'
        '"attack_level":0,"feedback_topic":null,"reflection_candidate":null,"confidence":0.9},'
        f'"tool_request":{{"name":"{tool_union}","query":""}}}}\n'
        "reply是唯一会发给用户的字段，绝不能把signals、评分、工具、分析或JSON写进reply。"
        "正常不想回复用ignore；只观察并记住但不回复用observe；这两种情况下reply、impression、"
        "user_facts均为空，score_delta为0，tool_request必须为none。"
        "reflection_candidate只有明确建议、纠错或重复反馈时才填写"
        '{"event":"发生了什么","possible_mistake":"哪里可能做错了","next_time":"以后怎么做"}，'
        "普通交流必须为null。熟人玩笑要结合关系判断，不要只凭敏感词当攻击。"
    )
