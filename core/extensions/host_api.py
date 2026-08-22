"""Capability-limited façade exposed to one discovered extension."""
from __future__ import annotations

import inspect
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


class BiliBotExtensionHostAPI:
    def __init__(self, grant: ExtensionGrant, plugin: Any, action_executor: ActionExecutor):
        self._grant = grant
        self._plugin = plugin
        self._action_executor = action_executor

    @property
    def extension_id(self) -> str:
        return self._grant.extension_id

    def describe(self) -> dict[str, Any]:
        account = {
            "logged_in": bool(_config_value(self._plugin, "SESSDATA", "")),
            "uid": str(_config_value(self._plugin, "DEDE_USER_ID", "") or ""),
            "name": "",
        }
        return {
            "bound": True,
            "status": "online",
            "host_version": "1.5.0",
            "extension_api": EXTENSION_API_VERSION,
            "services": {
                "bilibili.account": [1],
                "memory.creator": [1],
                "activity": [1],
            },
            "requested_permissions": sorted(self._grant.requested),
            "granted_permissions": sorted(self._grant.permissions),
            "account": account,
        }

    async def execute_action(
        self,
        *,
        extension_id: str,
        permission: str,
        action: str,
        payload: dict[str, Any],
        actor: dict[str, Any],
    ) -> Any:
        if extension_id != self._grant.extension_id:
            raise PermissionError("extension identity mismatch")
        self._grant.require(permission)
        result = self._action_executor(
            extension_id=extension_id,
            permission=permission,
            action=action,
            payload=dict(payload or {}),
            actor=dict(actor or {}),
        )
        return await result if inspect.isawaitable(result) else result
