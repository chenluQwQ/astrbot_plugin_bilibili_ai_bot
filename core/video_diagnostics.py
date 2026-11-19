"""Bounded, credential-free diagnostics; model calls are isolated per async task."""
from contextvars import ContextVar
from datetime import datetime
import json
import re

_call = ContextVar("bilibot_model_diagnostic", default=None)


def safe_detail(value, config=None, limit=300):
    text = str(value or "")
    for key, secret in (config or {}).items():
        if any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD", "SESSDATA", "JCT", "COOKIE", "PROXY_URL", "API_BASE")):
            if isinstance(secret, str) and secret:
                text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"https?://[^\s<>\"']+", "[地址已隐藏]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+|\bsk-[\w-]+", "[密钥已隐藏]", text)
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password|sessdata|bili_jct)[\"']?\s*[:=]\s*[\"']?[^\s,;\"']+", r"\1=[已隐藏]", text)
    return " ".join(text.split())[:limit]


def set_call_diagnostic(**values):
    _call.set(dict(values))


def get_call_diagnostic():
    return dict(_call.get() or {})


def exception_status_code(exc):
    """从异常对象读取 HTTP 状态码（兼容 ``.status_code``、``.code`` 与 ``.status``）。"""
    for attr in ("status_code", "code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and not isinstance(val, bool) and 100 <= val <= 599:
            return val
    return None


def merge_call_diagnostics(first, second):
    """双路由同时失败时合并诊断，保留主路由与备用路由的失败原因。"""
    first = dict(first or {})
    second = dict(second or {})
    if not first.get("reason"):
        return second
    if not second.get("reason"):
        return first
    merged = dict(second)
    if first["reason"] != second["reason"]:
        merged["reason"] = f"主路由: {first['reason']}；备用路由: {second['reason']}"
    r1 = first.get("rejection") or "other"
    r2 = second.get("rejection") or "other"
    if r1 == r2:
        merged["rejection"] = r1
    elif {r1, r2} == {"unavailable", "unsupported"}:
        merged["rejection"] = "unsupported"
    elif "too_large" in (r1, r2) and "other" not in (r1, r2):
        merged["rejection"] = "too_large"
    else:
        merged["rejection"] = "other"
    return merged


def exception_detail(exc, config):
    status = exception_status_code(exc)
    prefix = type(exc).__name__ + (f" HTTP {status}" if isinstance(status, int) else "")
    return safe_detail(f"{prefix}: {exc}", config)


def media_failure(info, stage, reason, config):
    rows = info.setdefault("_media_diagnostics", [])
    rows.append({"stage": stage, "reason": safe_detail(reason, config)})
    del rows[:-8]


def evaluation_failure(info, stage, reason, config, *, elapsed=0, raw=None):
    detail = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "stage": stage, "reason": safe_detail(reason, config),
        "analysis_basis": info.get("analysis_basis", "legacy"),
        "from_cache": bool(info.get("_analysis_from_cache")),
        "elapsed_seconds": round(elapsed, 2),
        "media_errors": list(info.get("_media_diagnostics", [])),
    }
    if stage != "content":
        detail["model_call"] = get_call_diagnostic()
    if raw is not None:
        # Store shape only: never save prompts, model prose, or response bodies.
        detail["response_chars"] = len(str(raw))
        text = str(raw).strip()
        if text.startswith("```") and text.endswith("```"):
            text = text[7:-3].strip() if text.startswith("```json") else text[3:-3].strip()
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                from .video_evaluation import EVALUATION_KEYS
                detail["missing_fields"] = sorted(EVALUATION_KEYS - value.keys())
                detail["extra_field_count"] = len(value.keys() - EVALUATION_KEYS)
            else:
                detail["response_type"] = type(value).__name__
        except ValueError as exc:
            detail["json_error_position"] = getattr(exc, "pos", None)
    info["_evaluation_diagnostic"] = detail
    return detail
