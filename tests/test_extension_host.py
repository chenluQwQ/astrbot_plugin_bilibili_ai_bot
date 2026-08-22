import asyncio
import json
from pathlib import Path

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
        "memory.creator.read",
        "memory.creator.write",
        "activity.write",
        "opportunities.read",
        "analytics.video.read",
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


class FakeMemoryAPI:
    def __init__(self):
        self.recorded = []

    def get_recent_memories(self, **_kwargs):
        return [{"rpid": "m1", "text": "公开创作记忆", "memory_type": "creator", "secret": "drop-me"}]

    def search(self, *_args, **_kwargs):
        return self.get_recent_memories()

    def record(self, text, **kwargs):
        self.recorded.append({"text": text, **kwargs})
        return "m2"


class HostPlugin:
    def __init__(self):
        self.config = Config(
            SESSDATA="top-secret-cookie",
            BILI_JCT="csrf-secret",
            DEDE_USER_ID="42",
            REFRESH_TOKEN="refresh-secret",
        )
        self.memory_api = FakeMemoryAPI()
        self._running = False

    async def _http_get(self, _url, params=None):
        return {"code": 0, "data": {"bvid": (params or {}).get("bvid", "BV1"), "aid": 1, "title": "Demo", "stat": {"view": 123, "like": 9, "coin": 4, "favorite": 5, "share": 2, "reply": 7, "danmaku": 8}}}


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


def make_dispatcher(context, plugin=None):
    registry = ExtensionRegistry(context, plugin or HostPlugin(), reject_write)
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
    assert description["account"] == {"logged_in": True, "uid": "42", "name": "", "running": False}
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


def test_new_creator_components_are_allowed_and_unknown_stays_denied():
    from core.extensions.contracts import ALLOWED_COMPONENT_TYPES, validate_page_schema

    expected = {"creator-production-timeline", "creator-signal-board", "creator-asset-library", "creator-workspace", "creator-opportunity-board", "creator-approval-center", "creator-permission-matrix", "creator-proposal-list"}
    assert expected <= ALLOWED_COMPONENT_TYPES
    validate_page_schema({"schema": "bilibot-schema-v1", "components": [{"type": item} for item in expected]})
    with pytest.raises(ValueError):
        validate_page_schema({"schema": "bilibot-schema-v1", "components": [{"type": "raw-html"}]})


def test_creator_memory_is_sanitized_and_namespaced():
    plugin = HostPlugin()
    creator = CreatorExtension()
    _registry, dispatcher = make_dispatcher(Context([Metadata(creator)]), plugin)
    asyncio.run(dispatcher.list_extensions())
    rows = asyncio.run(creator.host.read_creator_memory(limit=5))
    assert rows == [{"rpid": "m1", "text": "公开创作记忆", "memory_type": "creator"}]
    saved = asyncio.run(creator.host.write_creator_memory({"text": "风格实验", "entity_id": "p1", "kind": "retro", "tags": ["快节奏"]}))
    assert saved["stored"] is True
    call = plugin.memory_api.recorded[0]
    assert call["memory_type"] == "creator"
    assert call["source"] == "bilibot_extension:creator"
    assert call["extra"]["creator_extension_id"] == "creator"


def test_activity_and_signals_never_expose_credentials(tmp_path, monkeypatch):
    import core.config as config

    watch = tmp_path / "watch.json"
    bangumi = tmp_path / "bangumi.json"
    dynamic = tmp_path / "dynamic.json"
    watch.write_text(json.dumps([{"title": "AI 视频", "bvid": "BV1", "tags": ["AI"], "cookie": "secret"}]), encoding="utf-8")
    bangumi.write_text("[]", encoding="utf-8")
    dynamic.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "WATCH_LOG_FILE", watch)
    monkeypatch.setattr(config, "BANGUMI_WATCH_LOG_FILE", bangumi)
    monkeypatch.setattr(config, "DYNAMIC_LOG_FILE", dynamic)

    creator = CreatorExtension()
    _registry, dispatcher = make_dispatcher(Context([Metadata(creator)]))
    asyncio.run(dispatcher.list_extensions())
    creator.host.record_creator_activity({"kind": "idea.created", "entity_id": "i1", "title": "Demo", "actor": {"role": "admin", "cookie": "top-secret-cookie"}})
    activity = (tmp_path / "creator_extension_activity.json").read_text(encoding="utf-8")
    assert "top-secret-cookie" not in activity
    signals = creator.host.list_creator_signals()
    assert signals[0]["source_ref"] == "BV1"
    assert "cookie" not in signals[0]
    opportunities = creator.host.list_creator_opportunities()
    assert opportunities[0]["kind"] == "tag"
    assert opportunities[0]["eligibility"] == "review_required"


def test_signals_carry_the_judgement_a_browsing_pass_already_made(tmp_path, monkeypatch):
    """A pass scored the video and wrote down what it thought; pass that on.

    Sending only the summary made extensions re-watch to recover judgement this side
    had produced and then thrown away, and `actions` is the part that matters most:
    the score drives real interactions, and a coin or a favourite is a scarce
    commitment.
    """
    import core.config as config

    watch = tmp_path / "watch.json"
    memory = tmp_path / "video_memory.json"
    bangumi = tmp_path / "bangumi.json"
    dynamic = tmp_path / "dynamic.json"
    watch.write_text(json.dumps([{
        "title": "十分钟讲透扩散模型", "bvid": "BV1", "time": "2026-08-20 10:30",
        "score": 8, "mood": "震撼", "review": "比公式推导好懂", "tname": "科技",
        "up_name": "某个UP", "up_mid": "123", "pic": "https://i0.hdslb.com/c.jpg",
        "actions": ["👍点赞", "🪙投币", "⭐收藏"], "cookie": "secret",
    }]), encoding="utf-8")
    # video_memory is a dict keyed by bvid and holds the long analysis text.
    memory.write_text(json.dumps({"BV1": {
        "title": "十分钟讲透扩散模型", "bvid": "BV1",
        "analysis": "用动画讲清了加噪和去噪两个方向，" * 6,
    }}), encoding="utf-8")
    bangumi.write_text("[]", encoding="utf-8")
    dynamic.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "WATCH_LOG_FILE", watch)
    monkeypatch.setattr(config, "VIDEO_MEMORY_FILE", memory)
    monkeypatch.setattr(config, "BANGUMI_WATCH_LOG_FILE", bangumi)
    monkeypatch.setattr(config, "DYNAMIC_LOG_FILE", dynamic)

    creator = CreatorExtension()
    _registry, dispatcher = make_dispatcher(Context([Metadata(creator)]))
    asyncio.run(dispatcher.list_extensions())
    signals = creator.host.list_creator_signals()

    assert len(signals) == 1, "watch_log 和 video_memory 描述同一个视频，不该变成两条信号"
    signal = signals[0]
    assert signal["actions"] == ["👍点赞", "🪙投币", "⭐收藏"]
    assert signal["score"] == 8.0
    assert signal["mood"] == "震撼"
    assert signal["review"] == "比公式推导好懂"
    assert signal["tname"] == "科技"
    assert signal["up_name"] == "某个UP"
    assert signal["pic"].startswith("https://")
    # The long analysis wins over the shorter review as the summary.
    assert signal["summary"].startswith("用动画讲清了")
    assert "cookie" not in signal


def test_video_metrics_uses_public_sanitized_fields():
    creator = CreatorExtension()
    _registry, dispatcher = make_dispatcher(Context([Metadata(creator)]))
    asyncio.run(dispatcher.list_extensions())
    metrics = asyncio.run(creator.host.get_video_metrics(bvid="BV1"))
    assert metrics == {"bvid": "BV1", "aid": "1", "title": "Demo", "views": 123, "likes": 9, "coins": 4, "favorites": 5, "shares": 2, "comments": 7, "danmaku": 8}


def test_webui_mode_entry_is_manifest_driven_not_creator_hardcoded():
    source = (Path(__file__).resolve().parents[1] / "pages/bilibot/app.js").read_text(encoding="utf-8")
    assert "function availableModeExtensions()" in source
    assert "renderModeEntry(modeExtensions)" in source
    assert 'item.id === "creator"' not in source
    assert "creator-mode-switch" not in source
    brand_pos = source.index('sidebar.innerHTML = `<div class="sidebar-brand"')
    entry_pos = source.index("${renderModeEntry(modeExtensions)}", brand_pos)
    nav_pos = source.index('<nav class="nav-list">', brand_pos)
    assert brand_pos < entry_pos < nav_pos
