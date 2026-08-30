"""Shared Extension API v1 constants and strict manifest validation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

_CONTRACT_PATH = Path(__file__).with_name("extension_api_v1.json")
CONTRACT: dict[str, Any] = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8-sig"))
EXTENSION_API_VERSION = int(CONTRACT["extension_api"])
EXTENSION_TYPE = str(CONTRACT["extension_type"])
PAGE_RENDERER = str(CONTRACT["renderer"])
PERMISSIONS = frozenset(CONTRACT["permissions"])
MANIFEST_REQUIRED = frozenset(CONTRACT["manifest_required"])

ALLOWED_COMPONENT_TYPES = frozenset({
    "creator-hero",
    "creator-pipeline",
    "creator-idea-list",
    "creator-project-grid",
    "creator-page-intro",
    "creator-idea-board",
    "creator-studio",
    "creator-insights",
    "creator-host-status",
    "creator-connector-grid",
    "creator-production-timeline",
    "creator-signal-board",
    "creator-asset-library",
    "creator-workspace",
    "creator-opportunity-board",
    "creator-approval-center",
    "creator-permission-matrix",
    "creator-proposal-list",
})


class HostContractError(ValueError):
    """Raised when an extension violates the public host contract."""


def validate_extension_manifest(manifest: Mapping[str, Any]) -> None:
    if not isinstance(manifest, Mapping):
        raise HostContractError("extension manifest must be an object")
    missing = MANIFEST_REQUIRED - set(manifest)
    if missing:
        raise HostContractError(f"manifest missing fields: {sorted(missing)}")
    if manifest.get("type") != EXTENSION_TYPE:
        raise HostContractError("not a BiliBot extension")
    if manifest.get("extension_api") != EXTENSION_API_VERSION:
        raise HostContractError("incompatible Extension API version")
    extension_id = str(manifest.get("id", ""))
    if not extension_id or not extension_id.replace("-", "").replace("_", "").isalnum():
        raise HostContractError("manifest id must be a non-empty slug")
    unknown = set(manifest.get("permissions") or []) - PERMISSIONS
    if unknown:
        raise HostContractError(f"unknown permissions: {sorted(unknown)}")
    pages = manifest.get("pages") or []
    page_ids = {str(item.get("id")) for item in pages if isinstance(item, Mapping)}
    if not page_ids:
        raise HostContractError("manifest must declare at least one page")
    for page in pages:
        if not isinstance(page, Mapping) or page.get("renderer") != PAGE_RENDERER:
            raise HostContractError("extension page uses an unsupported renderer")
    for item in manifest.get("navigation") or []:
        if not isinstance(item, Mapping) or str(item.get("page")) not in page_ids:
            raise HostContractError("navigation references an unknown page")


def validate_page_schema(page: Mapping[str, Any]) -> None:
    if not isinstance(page, Mapping) or page.get("schema") != PAGE_RENDERER:
        raise HostContractError("extension returned an unsupported page schema")
    components = page.get("components")
    if not isinstance(components, list):
        raise HostContractError("extension page components must be a list")
    for component in components:
        if not isinstance(component, Mapping):
            raise HostContractError("extension component must be an object")
        component_type = str(component.get("type", ""))
        if component_type not in ALLOWED_COMPONENT_TYPES:
            raise HostContractError(f"unsupported extension component: {component_type!r}")
