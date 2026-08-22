import asyncio
import json

import pytest

from core.extensions.dispatcher import ExtensionDispatcher
from core.extensions.registry import ExtensionRegistry


BASE_MANIFEST = {
    "type": "bilibot-extension",
    "id": "creator",
    "name": "Creator",
    "version": "0.2.0",
    "extension_api": 1,
    "host_requires": ">=1.5.0,<2.0.0",
    "navigation": [{"page": "dashboard", "title": "创作总览"}],
    "pages": [{"id": "dashboard", "renderer": "bilibot-schema-v1"}],
    "actions": ["request-publish"],
    "permissions": [
        "account.identity.read",
        "storage.extension.read",
        "actions.video.publish",
    ],
}


class Metadata:
    def __init__(self, star_cls, activated=True):
        self.star_cls = star_cls
        self.activated = activated


class Context:
    def __init__(self, stars=None):
        self.stars = list(stars or [])

    def get_all_stars(self):
        return self.stars


class Config(dict):
    pass


class HostPlugin:
    def __init__(self):
        self.config = Config(
            SESSDATA="top-secret-cookie",
            BILI_JCT="csrf-secret",
            DEDE_USER_ID="42",
            REFRESH_TOKEN="refresh-secret",
        )


class CreatorExtension:
    def __init__(self, manifest=None):
        self.manifest = dict(manifest or BASE_MANIFEST)
        self.host = None
        self.unbound = 0
        self.last_request = None

    def get_bilibot_extension_manifest(self):
        return dict(self.manifest)

    def bind_bilibot_host(self, host):
        self.host = host

    def unbind_bilibot_host(self):
        self.host = None
        self.unbound += 1

    async def handle_bilibot_extension_request(self, request):
        self.last_request = request
        if request["operation"].startswith("page:"):
            data = {
                "page": {
                    "schema": "bilibot-schema-v1",
                    "page": "dashboard",
                    "title": "Creator",
                    "components": [{"type": "creator-hero", "title": "Create"}],
                }
            }
        else:
            data = {"accepted": True}
        return {"request_id": request["request_id"], "ok": True, "data": data, "error": None}


async def reject_write(**_kwargs):
    raise AssertionError("denied permission must not reach the action executor")


def make_dispatcher(context):
    registry = ExtensionRegistry(context, HostPlugin(), reject_write)
    return registry, ExtensionDispatcher(registry)


def test_no_extension_keeps_host_empty():
    _registry, dispatcher = make_dispatcher(Context())
    assert asyncio.run(dispatcher.list_extensions()) == []


def test_creator_is_discovered_bound_and_dispatched():
    creator = CreatorExtension()
    _registry, dispatcher = make_dispatcher(Context([Metadata(creator)]))
    manifests = asyncio.run(dispatcher.list_extensions())
    assert [item["id"] for item in manifests] == ["creator"]
    assert creator.host is not None

    response = asyncio.run(dispatcher.dispatch("creator", "page:dashboard", actor={"role": "admin"}))
    assert response["ok"] is True
    assert response["request_id"] == creator.last_request["request_id"]


def test_disabled_and_invalid_extensions_are_isolated():
    disabled = CreatorExtension({**BASE_MANIFEST, "enabled": False})
    invalid = CreatorExtension({**BASE_MANIFEST, "id": "bad id"})
    valid = CreatorExtension()
    _registry, dispatcher = make_dispatcher(Context([Metadata(disabled), Metadata(invalid), Metadata(valid)]))
    assert [item["id"] for item in asyncio.run(dispatcher.list_extensions())] == ["creator"]


def test_duplicate_id_is_skipped_without_breaking_first_extension():
    first = CreatorExtension()
    second = CreatorExtension()
    _registry, dispatcher = make_dispatcher(Context([Metadata(first), Metadata(second)]))
    manifests = asyncio.run(dispatcher.list_extensions())
    assert len(manifests) == 1
    assert first.host is not None
    assert second.host is None


def test_removed_extension_is_unbound():
    creator = CreatorExtension()
    context = Context([Metadata(creator)])
    registry, dispatcher = make_dispatcher(context)
    asyncio.run(dispatcher.list_extensions())
    context.stars = []
    asyncio.run(registry.refresh())
    assert creator.unbound == 1
    assert creator.host is None


def test_publish_permission_is_denied_by_default():
    creator = CreatorExtension()
    _registry, dispatcher = make_dispatcher(Context([Metadata(creator)]))
    asyncio.run(dispatcher.list_extensions())
    with pytest.raises(PermissionError):
        asyncio.run(
            creator.host.execute_action(
                extension_id="creator",
                permission="actions.video.publish",
                action="request-publish",
                payload={},
                actor={"role": "admin"},
            )
        )


def test_host_description_never_contains_credentials():
    creator = CreatorExtension()
    _registry, dispatcher = make_dispatcher(Context([Metadata(creator)]))
    asyncio.run(dispatcher.list_extensions())
    description = creator.host.describe()
    serialized = json.dumps(description).lower()
    assert description["account"] == {"logged_in": True, "uid": "42", "name": ""}
    for secret in ("top-secret-cookie", "csrf-secret", "refresh-secret", "sessdata", "bili_jct", "refresh_token"):
        assert secret not in serialized
    assert "actions.video.publish" in description["requested_permissions"]
    assert "actions.video.publish" not in description["granted_permissions"]


def test_unknown_component_is_rejected():
    creator = CreatorExtension()

    async def unsafe_handler(request):
        return {
            "request_id": request["request_id"],
            "ok": True,
            "data": {
                "page": {
                    "schema": "bilibot-schema-v1",
                    "components": [{"type": "raw-html", "html": "<script>alert(1)</script>"}],
                }
            },
            "error": None,
        }

    creator.handle_bilibot_extension_request = unsafe_handler
    _registry, dispatcher = make_dispatcher(Context([Metadata(creator)]))
    with pytest.raises(ValueError):
        asyncio.run(dispatcher.dispatch("creator", "page:dashboard"))
