"""Host-owned extension permission grants."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .contracts import PERMISSIONS

SAFE_DEFAULT_PERMISSIONS = frozenset({
    "account.identity.read", "memory.creator.read", "memory.creator.write", "activity.write",
    "analytics.video.read", "opportunities.read", "storage.extension.read", "storage.extension.write",
})


@dataclass(frozen=True, slots=True)
class ExtensionGrant:
    extension_id: str
    requested: frozenset[str]
    permissions: frozenset[str]

    @classmethod
    def create(cls, extension_id: str, permissions: Iterable[str]) -> "ExtensionGrant":
        requested = frozenset(str(item) for item in permissions)
        unknown = requested - PERMISSIONS
        if unknown:
            raise ValueError(f"unknown permissions: {sorted(unknown)}")
        return cls(extension_id=extension_id, requested=requested, permissions=requested & SAFE_DEFAULT_PERMISSIONS)

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise PermissionError(f"extension {self.extension_id!r} was not granted {permission!r}")
