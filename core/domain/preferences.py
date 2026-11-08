"""视频偏好：读写都经过这里。

真身在 sqlite（``storage/stores.py`` 的 ``PreferenceStore``：观看证据 → 候选 / 近期 / 稳定）。
``preference_state.json`` 是每次同步后写的副本，只由这里写；数据库没开或读失败时读它。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..config import PREFERENCE_STATE_FILE
from ..infra.host_view import HostView


class Preferences(HostView):
    HOST_API = frozenset({"json_store", "layered_runtime"})

    def _store(self):
        layered = getattr(self, "layered_runtime", None)
        store = getattr(layered, "preferences", None)
        if store is None or not getattr(layered, "is_open", False):
            return None
        return store

    def is_live(self) -> bool:
        return self._store() is not None

    # ── 副本 ──
    def cached_state(self) -> dict[str, Any]:
        loader = getattr(getattr(self, "json_store", None), "_load_json", None)
        cached = loader(PREFERENCE_STATE_FILE, {}) if callable(loader) else {}
        return cached if isinstance(cached, dict) else {"current": [], "changes": []}

    def cached(self) -> list:
        """副本里的当前偏好（同步读，不碰数据库）。"""
        current = self.cached_state().get("current", [])
        return current if isinstance(current, list) else []

    # ── 数据库 ──
    async def current(self, *, limit: int) -> list[dict[str, Any]]:
        """数据库里的当前偏好；数据库没开就是空的，要兜底的调用方自己读 ``cached``。"""
        store = self._store()
        if store is None:
            return []
        return await store.current(limit=limit)

    async def sync(self, observations) -> dict[str, Any]:
        """写入观看证据并重算，写副本，返回 ``{"current", "changes"}``。

        ``observations`` 是 ``(source_ref, signals, occurred_at)``。数据库没开就原样返回副本。
        """
        store = self._store()
        if store is None:
            return self.cached_state()
        for source_ref, signals, occurred_at in observations:
            await store.record_video_signals(
                source_ref=source_ref, signals=signals, occurred_at=occurred_at,
            )
        state = await store.refresh()
        saver = getattr(getattr(self, "json_store", None), "_save_json", None)
        if callable(saver):
            saver(PREFERENCE_STATE_FILE, {
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "current": state.get("current", []),
                "changes": state.get("changes", []),
            })
        return state
