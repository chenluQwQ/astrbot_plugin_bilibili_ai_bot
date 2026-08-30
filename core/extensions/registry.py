"""Lazy, failure-isolated discovery of duck-typed BiliBot extensions."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Iterable

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - test fallback
    class _Logger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None
    logger = _Logger()

from .contracts import validate_extension_manifest
from .host_api import BiliBotExtensionHostAPI
from .permissions import ExtensionGrant


@dataclass(slots=True)
class RegisteredExtension:
    extension_id: str
    plugin: Any
    manifest: dict[str, Any]
    host_api: BiliBotExtensionHostAPI


def _plugin_instances(stars: Any) -> Iterable[Any]:
    values = stars.values() if isinstance(stars, dict) else (stars or [])
    for value in values:
        if getattr(value, "activated", True) is False:
            continue
        instance = getattr(value, "star_cls", getattr(value, "star", getattr(value, "instance", value)))
        if instance is not None:
            yield instance


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class ExtensionRegistry:
    def __init__(self, context: Any, plugin: Any, action_executor: Any):
        self.context = context
        self.plugin = plugin
        self.action_executor = action_executor
        self.extensions: dict[str, RegisteredExtension] = {}

    async def _unbind(self, record: RegisteredExtension) -> None:
        unbind = getattr(record.plugin, "unbind_bilibot_host", None)
        if callable(unbind):
            try:
                await _maybe_await(unbind())
            except Exception as exc:
                logger.warning(f"[BiliBot Extensions] unbind {record.extension_id} failed: {exc}")

    async def refresh(self) -> dict[str, RegisteredExtension]:
        getter = getattr(self.context, "get_all_stars", None)
        if not callable(getter):
            for record in self.extensions.values():
                await self._unbind(record)
            self.extensions = {}
            return self.extensions
        try:
            stars = await _maybe_await(getter())
        except Exception as exc:
            logger.warning(f"[BiliBot Extensions] discovery unavailable: {exc}")
            return self.extensions

        discovered: dict[str, RegisteredExtension] = {}
        for extension in _plugin_instances(stars):
            manifest_getter = getattr(extension, "get_bilibot_extension_manifest", None)
            handler = getattr(extension, "handle_bilibot_extension_request", None)
            if not callable(manifest_getter) or not callable(handler):
                continue
            try:
                manifest = await _maybe_await(manifest_getter())
                validate_extension_manifest(manifest)
                manifest = dict(manifest)
                if manifest.get("enabled", True) is False:
                    continue
                extension_id = str(manifest["id"])
                if extension_id in discovered:
                    logger.warning(f"[BiliBot Extensions] duplicate id skipped: {extension_id}")
                    continue
                previous = self.extensions.get(extension_id)
                if previous is not None and previous.plugin is extension:
                    previous.manifest = manifest
                    discovered[extension_id] = previous
                    continue
                grant = ExtensionGrant.create(extension_id, manifest.get("permissions") or [])
                host_api = BiliBotExtensionHostAPI(grant, self.plugin, self.action_executor)
                binder = getattr(extension, "bind_bilibot_host", None)
                if callable(binder):
                    await _maybe_await(binder(host_api))
                discovered[extension_id] = RegisteredExtension(extension_id, extension, manifest, host_api)
            except Exception as exc:
                logger.warning(f"[BiliBot Extensions] skipped incompatible extension: {exc}")

        for extension_id, record in self.extensions.items():
            replacement = discovered.get(extension_id)
            if replacement is None or replacement.plugin is not record.plugin:
                await self._unbind(record)
        self.extensions = discovered
        return discovered

    async def get(self, extension_id: str) -> RegisteredExtension:
        await self.refresh()
        try:
            return self.extensions[extension_id]
        except KeyError as exc:
            raise KeyError(f"BiliBot extension not found: {extension_id}") from exc

    async def close(self) -> None:
        for record in list(self.extensions.values()):
            await self._unbind(record)
        self.extensions = {}
