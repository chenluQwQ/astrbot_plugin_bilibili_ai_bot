"""谁是主人、谁必回、谁永不被自动拉黑：都看配置，每次现读。"""
from __future__ import annotations

from typing import Callable


class Roles:
    def __init__(self, settings: Callable[[], object]):
        self._settings = settings

    def _uids(self, key: str) -> set[str]:
        return {str(x).strip() for x in self._settings().get(key) or [] if str(x).strip()}

    def is_owner(self, mid) -> bool:
        owner = str(self._settings().get("OWNER_MID") or "").strip()
        return bool(owner) and str(mid).strip() == owner

    def always_reply(self, mid) -> bool:
        """必回白名单：这些 UID 不受概率/语义去重影响，一定回复。"""
        return str(mid).strip() in self._uids("REPLY_ALWAYS_UIDS")

    def never_block(self, mid) -> bool:
        """拉黑白名单：主人 + 配置的 UID 永不被自动拉黑。"""
        return self.is_owner(mid) or str(mid).strip() in self._uids("BLOCK_WHITELIST_UIDS")
