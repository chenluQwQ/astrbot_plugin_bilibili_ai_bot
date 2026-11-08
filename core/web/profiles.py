"""用户页：画像加好感度，按好感排前 50。"""
from __future__ import annotations

from typing import Any

from astrbot.api import logger
from ..app import app_of
from ._common import _failure, _response


async def handle_get_profiles(plugin: Any):
    try:
        app = app_of(plugin)
        profiles = app.user_profiles.all()
        affection = app.affection.snapshot()
        # 没到过画像门槛的人，画像要等每晚清算才删（好感度关着时不删），这之前标出来
        pending_delete = "tonight" if app.settings.get("ENABLE_AFFECTION") else "affection_off"
        data = []
        for uid, raw in (profiles.items() if isinstance(profiles, dict) else []):
            profile = app_of(plugin).normalize_user_profile(raw)
            live = profile.get("live", {}) if isinstance(profile.get("live"), dict) else {}
            refs = profile.get("video_refs", []) if isinstance(profile.get("video_refs"), list) else []
            last_values = [str(item.get("time", "")) for item in refs if isinstance(item, dict) and item.get("time")]
            if live.get("last_seen"):
                last_values.append(str(live["last_seen"]))
            score = int(affection.get(str(uid), 0) or 0)
            data.append({
                "user_id": str(uid),
                "name": profile.get("username") or f"UID {uid}",
                "affection": score,
                "relationship": app_of(plugin).relationship_level(score, uid),
                "impression": profile.get("impression", ""),
                "tags": profile.get("tags", [])[-6:],
                "facts_count": len(profile.get("facts", []) or []),
                "video_refs_count": len(refs),
                "last_interaction": max(last_values, default=""),
                "has_profile": True,
                "below_line": None if app.affection.reached_profile_line(uid) else pending_delete,
            })
        known = {item["user_id"] for item in data}
        for uid, score in affection.items() if isinstance(affection, dict) else []:
            if str(uid) not in known:
                data.append({"user_id": str(uid), "name": f"UID {uid}", "affection": int(score or 0), "relationship": app_of(plugin).relationship_level(score, uid), "impression": "", "tags": [], "facts_count": 0, "video_refs_count": 0, "last_interaction": "", "has_profile": False})
        data.sort(key=lambda item: (item["affection"], item["last_interaction"]), reverse=True)
        return _response(data[:50])
    except Exception as exc:
        logger.exception(f"[BiliBot WebUI] profiles failed: {exc}")
        return _failure(str(exc), 500)
