"""评论图片的历史快照契约：仅保存事实，不保存文件、密钥或向量。"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

MAX_IMAGES = 9


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def public_bili_url(value) -> str:
    """只接受 B站公开地址，丢弃查询凭证；此处不下载图片。"""
    try:
        text = str(value or "").strip()
        parts = urlsplit("https:" + text if text.startswith("//") else text)
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or parts.username or parts.password:
            return ""
        if parts.port not in {None, 80, 443}:
            return ""
        if not any(host == d or host.endswith("." + d) for d in (
            "hdslb.com", "biliimg.com", "bilibili.com",
        )):
            return ""
        return urlunsplit(("https", host, parts.path, "", ""))
    except ValueError:
        return ""


def publication_status(rec: dict) -> str:
    explicit = str(rec.get("send_status") or "").lower()
    if explicit:
        return explicit if explicit in {"published", "draft", "failed", "unknown"} else "unknown"
    # 旧 proactive_log 仅在发送成功后写入，但没有图片发布凭据。
    return "legacy_confirmed"


def normalize_images(images, *, published: bool, clip) -> list[dict]:
    """同一评论内按文件 SHA256/asset_id/公开地址去重。"""
    result: dict[str, dict] = {}
    for raw in images if isinstance(images, list) else []:
        if not isinstance(raw, dict):
            continue
        sha = str(raw.get("sha256") or "").lower()
        sha = sha if re.fullmatch(r"[0-9a-f]{64}", sha) else ""
        asset_id = str(raw.get("asset_id") or sha).strip()[:128]
        uploaded = public_bili_url(raw.get("published_url")) if published else ""
        source_url = public_bili_url(raw.get("source_url"))
        identity = sha or asset_id or uploaded or source_url
        if not identity:
            continue
        snapshot = {
            "asset_id": asset_id,
            "sha256": sha,
            "description": clip(raw.get("description"), 240),
            "emotion": clip(raw.get("emotion"), 40),
            "use_reason": clip(raw.get("use_reason"), 160),
            "source_url": source_url,
            "source_kind": clip(raw.get("source_kind"), 32),
            "source_bvid": clip(raw.get("source_bvid"), 32),
            "published_url": uploaded,
            "local_status": raw.get("local_status") if raw.get("local_status") in {
                "available", "evicted",
            } else "unknown",
        }
        if identity in result:
            old = result[identity]
            # 重复项补齐缺失字段，不覆盖已经保存的历史描述或地址。
            for key, value in snapshot.items():
                if value and not old.get(key):
                    old[key] = value
            if snapshot["local_status"] == "evicted":
                old["local_status"] = "evicted"
        elif len(result) < MAX_IMAGES:
            result[identity] = snapshot
    return list(result.values())


def merge_images(old, new) -> list[dict]:
    """后续日志缺字段/图片已淘汰时，仍保留发送时的完整快照。"""
    merged = [dict(x) for x in old if isinstance(x, dict)] if isinstance(old, list) else []
    for image in new:
        identities = {v for v in (image.get("sha256"), image.get("asset_id")) if v}
        match = next((x for x in merged if identities & {
            v for v in (x.get("sha256"), x.get("asset_id")) if v
        } or (image.get("published_url") and image["published_url"] == x.get("published_url"))), None)
        if match is None:
            if len(merged) < MAX_IMAGES:
                merged.append(dict(image))
        else:
            for key, value in image.items():
                if key == "local_status" and match.get(key) == "evicted":
                    continue
                if value and (not match.get(key) or key == "local_status"):
                    match[key] = value
    return merged


def apply_evictions(images: list[dict], evicted: dict) -> list[dict]:
    result = []
    for image in images:
        image = dict(image)
        times = [evicted[k] for k in (image.get("asset_id"), image.get("sha256"))
                 if k and k in evicted]
        if times:
            image["local_status"] = "evicted"
            image["evicted_at"] = min(times)
        result.append(image)
    return result


def comment_key(rec: dict) -> str:
    bvid = str(rec.get("bvid") or "").strip()
    rpid = str(rec.get("rpid") or "").strip()
    if rpid.isdigit() and int(rpid) > 0:
        return f"comment|{bvid}|rpid:{rpid}"
    # 未记录 rpid 的旧日志采用事件时间和正文；图片/状态变化不改变事件身份。
    return f"comment|{bvid}|{rec.get('time')}|{fingerprint(str(rec.get('comment') or ''))[:20]}"
