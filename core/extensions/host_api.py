"""Capability-limited façade exposed to one discovered extension."""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from .contracts import EXTENSION_API_VERSION
from .permissions import ExtensionGrant

ActionExecutor = Callable[..., Awaitable[Any]]


def _config_value(plugin: Any, key: str, default: Any = None) -> Any:
    config = getattr(plugin, "config", None)
    try:
        return config.get(key, default)
    except Exception:
        data = getattr(config, "data", {})
        return data.get(key, default) if isinstance(data, dict) else default


def _public_memory(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {"rpid", "text", "time", "memory_type", "level", "importance", "source", "bvid", "video_title", "score"}
    return {key: item.get(key) for key in allowed if item.get(key) not in (None, "")}


def _read_json(path: str | Path, default: Any) -> Any:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8-sig"))
        return value
    except Exception:
        return default


class BiliBotExtensionHostAPI:
    def __init__(self, grant: ExtensionGrant, plugin: Any, action_executor: ActionExecutor):
        self._grant = grant
        self._plugin = plugin
        self._action_executor = action_executor

    @property
    def extension_id(self) -> str:
        return self._grant.extension_id

    def get_account_snapshot(self) -> dict[str, Any]:
        self._grant.require("account.identity.read")
        return {
            "logged_in": bool(_config_value(self._plugin, "SESSDATA", "")),
            "uid": str(_config_value(self._plugin, "DEDE_USER_ID", "") or ""),
            "name": str(getattr(self._plugin, "_bilibili_username", "") or ""),
            "running": bool(getattr(self._plugin, "_running", False)),
        }

    def describe(self) -> dict[str, Any]:
        account = self.get_account_snapshot() if "account.identity.read" in self._grant.permissions else {"logged_in": False, "uid": "", "name": ""}
        return {
            "bound": True, "status": "online", "host_version": "1.5.0", "extension_api": EXTENSION_API_VERSION,
            "services": {"bilibili.account": [1], "memory.creator": [1], "activity": [1], "creator.signals": [1], "creator.analytics": [1], "creator.opportunities": [1]},
            "requested_permissions": sorted(self._grant.requested), "granted_permissions": sorted(self._grant.permissions), "account": account,
        }

    async def read_creator_memory(self, query: str = "", *, limit: int = 10) -> list[dict[str, Any]]:
        self._grant.require("memory.creator.read")
        memory_api = getattr(self._plugin, "memory_api", None)
        if memory_api is None:
            return []
        limit = max(1, min(int(limit or 10), 50))
        if str(query or "").strip():
            rows = memory_api.search(str(query), memory_types={"video", "dynamic", "bangumi", "creator"}, reader_scope="admin", limit=limit, score_threshold=0.2)
            rows = await rows if inspect.isawaitable(rows) else rows
        else:
            rows = memory_api.get_recent_memories(memory_types={"video", "dynamic", "bangumi", "creator"}, hours=24 * 30, limit=limit, reader_scope="admin")
        return [_public_memory(dict(item)) for item in (rows or []) if isinstance(item, dict)]

    async def write_creator_memory(self, entry: dict[str, Any]) -> dict[str, Any]:
        self._grant.require("memory.creator.write")
        memory_api = getattr(self._plugin, "memory_api", None)
        if memory_api is None:
            raise RuntimeError("BiliBot memory service is unavailable")
        text = str(entry.get("text", "")).strip()[:2000]
        if not text:
            raise ValueError("creator memory text is required")
        extra = {"creator_extension_id": self.extension_id, "creator_entity_id": str(entry.get("entity_id", ""))[:120], "creator_kind": str(entry.get("kind", ""))[:80], "creator_tags": [str(v)[:40] for v in list(entry.get("tags") or [])[:20]]}
        result = memory_api.record(text, user_id="self", username="Creator", source=f"bilibot_extension:{self.extension_id}", memory_type="creator", level="recent", importance=6, extra=extra)
        rpid = await result if inspect.isawaitable(result) else result
        return {"rpid": str(rpid), "stored": True}

    def record_creator_activity(self, event: dict[str, Any]) -> dict[str, Any]:
        self._grant.require("activity.write")
        from core.config import DATA_DIR
        target = Path(DATA_DIR) / "creator_extension_activity.json"
        rows = _read_json(target, [])
        if not isinstance(rows, list): rows = []
        safe = {
            "extension_id": self.extension_id,
            "kind": str(event.get("kind", "event"))[:80],
            "entity_id": str(event.get("entity_id", ""))[:120],
            "title": str(event.get("title", ""))[:240],
            "actor": {"role": str((event.get("actor") or {}).get("role", ""))[:40], "source": str((event.get("actor") or {}).get("source", ""))[:80]},
        }
        from datetime import datetime, timezone
        safe["created_at"] = datetime.now(timezone.utc).isoformat()
        rows.append(safe); rows = rows[-1000:]
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
        return {"recorded": True}

    def list_creator_signals(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self._grant.require("memory.creator.read")
        from core.config import BANGUMI_WATCH_LOG_FILE, DYNAMIC_LOG_FILE, VIDEO_MEMORY_FILE, WATCH_LOG_FILE
        rows: list[dict[str, Any]] = []
        # video_memory holds the long `analysis` text; watch_log holds the score and
        # the interactions.  Both describe the same bvid, so they are merged below
        # rather than surfaced as two signals for one video.
        sources = ((WATCH_LOG_FILE, "watch", "bvid"), (VIDEO_MEMORY_FILE, "watch", "bvid"), (BANGUMI_WATCH_LOG_FILE, "bangumi", "episode_id"), (DYNAMIC_LOG_FILE, "dynamic", "dynamic_id"))
        for path, source, ref_key in sources:
            data = _read_json(path, [])
            if isinstance(data, dict): data = list(data.values())
            for item in data if isinstance(data, list) else []:
                if not isinstance(item, dict): continue
                title = str(item.get("title") or item.get("video_title") or item.get("season_title") or item.get("summary") or item.get("text") or "").strip()
                if not title: continue
                rows.append({
                    "title": title[:240],
                    "summary": str(item.get("summary") or item.get("analysis") or item.get("review") or item.get("reason") or "")[:600],
                    "source": f"bilibot-{source}",
                    "source_ref": str(item.get(ref_key) or item.get("bvid") or item.get("id") or ""),
                    "tags": [str(v)[:40] for v in list(item.get("tags") or [])[:12]],
                    "heat_score": float(item.get("score") or 0),
                    "captured_at": str(item.get("time") or item.get("created_at") or ""),
                    # A browsing pass already scored the video and wrote down what it
                    # thought; passing only the summary made extensions re-watch to
                    # recover judgement this side had produced and thrown away.
                    "score": float(item.get("score") or 0),
                    "mood": str(item.get("mood") or "")[:40],
                    "review": str(item.get("review") or "")[:320],
                    "up_name": str(item.get("up_name") or "")[:120],
                    "up_mid": str(item.get("up_mid") or ""),
                    "tname": str(item.get("tname") or "")[:80],
                    "pic": str(item.get("pic") or "")[:400],
                    # The score drives real interactions (点赞 6 / 评论 7 / 投币 8 /
                    # 收藏 8 / 关注 9). Spending a coin or a favourite slot is a scarce
                    # commitment, so this says more about interest than the score.
                    "actions": [str(v)[:40] for v in list(item.get("actions") or [])[:12]],
                })
        merged: dict[str, dict[str, Any]] = {}
        loose: list[dict[str, Any]] = []
        for row in rows:
            ref = row.get("source_ref") or ""
            if not ref:
                loose.append(row)
                continue
            current = merged.get(ref)
            if current is None:
                merged[ref] = row
                continue
            # Keep whichever field is actually populated: neither source has all of
            # them, and an empty string must never overwrite real content.
            for key, value in row.items():
                if value in (None, "", [], 0.0) or key == "source_ref":
                    continue
                if current.get(key) in (None, "", [], 0.0):
                    current[key] = value
                elif key == "summary" and len(str(value)) > len(str(current[key])):
                    current[key] = value
        combined = list(merged.values()) + loose
        combined.sort(key=lambda item: item.get("captured_at", ""), reverse=True)
        return combined[:max(1, min(int(limit or 20), 100))]

    def list_creator_opportunities(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self._grant.require("opportunities.read")
        signals = self.list_creator_signals(limit=100)
        counts: dict[str, int] = {}
        for signal in signals:
            for tag in signal.get("tags", []): counts[tag] = counts.get(tag, 0) + 1
        return [{"title": f"近期内容标签：{tag}", "kind": "tag", "source": "bilibot-observation", "source_ref": f"tag:{tag}", "summary": f"近期香港内容中出现 {count} 次，可作为投稿标签候选；仍需人工判断相关性。", "tags": [tag], "eligibility": "review_required", "confidence": min(0.95, 0.35 + count * 0.1)} for tag, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:max(1, min(int(limit or 20), 50))]]

    async def get_video_metrics(self, *, bvid: str = "", aid: str = "") -> dict[str, Any]:
        self._grant.require("analytics.video.read")
        bvid, aid = str(bvid).strip(), str(aid).strip()
        if not bvid and not aid:
            raise ValueError("bvid or aid is required")
        getter = getattr(self._plugin, "_http_get", None)
        if not callable(getter):
            raise RuntimeError("BiliBot public API client is unavailable")
        endpoint = "https://api.bilibili.com/x/web-interface/view"
        params = {"bvid": bvid} if bvid else {"aid": aid}
        result = getter(endpoint, params=params)
        result = await result if inspect.isawaitable(result) else result
        payload = result[0] if isinstance(result, tuple) else result
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise RuntimeError(str((payload or {}).get("message", "video metrics unavailable")))
        data = payload.get("data") or {}; stat = data.get("stat") or {}
        return {"bvid": str(data.get("bvid", bvid)), "aid": str(data.get("aid", aid)), "title": str(data.get("title", ""))[:240], "views": int(stat.get("view", 0) or 0), "likes": int(stat.get("like", 0) or 0), "coins": int(stat.get("coin", 0) or 0), "favorites": int(stat.get("favorite", 0) or 0), "shares": int(stat.get("share", 0) or 0), "comments": int(stat.get("reply", 0) or 0), "danmaku": int(stat.get("danmaku", 0) or 0)}

    async def execute_action(self, *, extension_id: str, permission: str, action: str, payload: dict[str, Any], actor: dict[str, Any]) -> Any:
        if extension_id != self._grant.extension_id:
            raise PermissionError("extension identity mismatch")
        self._grant.require(permission)
        result = self._action_executor(extension_id=extension_id, permission=permission, action=action, payload=dict(payload or {}), actor=dict(actor or {}))
        return await result if inspect.isawaitable(result) else result
