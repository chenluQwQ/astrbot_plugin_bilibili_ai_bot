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
    """HTTP 状态码：各家 SDK 放的地方不一样（status_code / status / code / response.status_code）。"""
    for owner in (exc, getattr(exc, "response", None)):
        for attr in ("status_code", "status", "code"):
            value = getattr(owner, attr, None)
            if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
                return value
    return None


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
