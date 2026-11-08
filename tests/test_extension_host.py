from base_parts import BaseParts
import asyncio
import json
import sys
import types
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
    "host_requires": ">=1.5.0,<3.0.0",
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


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Keep standalone tests independent of AstrBot and any user's stored data."""
    import core
    config = types.ModuleType("core.config")
    config.DATA_DIR = tmp_path
    for name in ("WATCH_LOG_FILE", "VIDEO_MEMORY_FILE", "BANGUMI_WATCH_LOG_FILE", "DYNAMIC_LOG_FILE"):
        path = tmp_path / (name.lower() + ".json")
        path.write_text("[]", encoding="utf-8")
        setattr(config, name, path)
    config.CREATOR_ACTIVITY_FILE = tmp_path / "creator_extension_activity.json"
    monkeypatch.setitem(sys.modules, "core.config", config)
    monkeypatch.setattr(core, "config", config, raising=False)
    return config


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


class HostPlugin(BaseParts):
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


def test_activity_and_signals_never_expose_credentials(tmp_path, monkeypatch, isolated_config):
    config = isolated_config

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


def test_signals_carry_the_judgement_a_browsing_pass_already_made(tmp_path, monkeypatch, isolated_config):
    """A pass scored the video and wrote down what it thought; pass that on.

    Sending only the summary made extensions re-watch to recover judgement this side
    had produced and then thrown away, and `actions` is the part that matters most:
    the score drives real interactions, and a coin or a favourite is a scarce
    commitment.
    """
    config = isolated_config

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
        "title": "十分钟讲透扩散模型",
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


@pytest.mark.parametrize("changes", [
    {"host_requires": ">=99.0"}, {"host_requires": "invalid"},
    {"pages": [{"renderer": "bilibot-schema-v1"}]},
    {"pages": [{"id": "x", "renderer": "bilibot-schema-v1"}] * 2},
    {"actions": ["publish", "publish"]}, {"permissions": "account.identity.read"},
    {"presentation": []},
])
def test_incompatible_manifests_are_rejected(changes):
    from core.extensions.contracts import validate_extension_manifest
    with pytest.raises(ValueError):
        validate_extension_manifest({**BASE_MANIFEST, **changes})


def test_dispatch_rejects_undeclared_operations_before_calling_extension():
    creator = CreatorExtension()
    _, dispatcher = make_dispatcher(Context([Metadata(creator)]))
    for operation in ("page:secret", "action:delete-all", "arbitrary"):
        with pytest.raises(ValueError):
            asyncio.run(dispatcher.dispatch("creator", operation))
    assert creator.last_request is None


def test_removed_and_closed_bindings_cannot_keep_using_host():
    async def scenario():
        creator = CreatorExtension()
        context = Context([Metadata(creator)])
        registry, dispatcher = make_dispatcher(context)
        await dispatcher.list_extensions()
        old_host = creator.host
        context.stars = []
        await registry.refresh()
        with pytest.raises(PermissionError):
            old_host.get_account_snapshot()
        assert old_host.describe()["bound"] is False
        context.stars = [Metadata(creator)]
        await registry.refresh()
        new_host = creator.host
        await registry.close()
        with pytest.raises(PermissionError):
            new_host.get_account_snapshot()
        assert await registry.refresh() == {}
    asyncio.run(scenario())


def test_permission_changes_revoke_the_previous_binding():
    async def scenario():
        creator = CreatorExtension()
        registry, dispatcher = make_dispatcher(Context([Metadata(creator)]))
        await dispatcher.list_extensions()
        old_host = creator.host
        creator.manifest["permissions"] = ["memory.creator.read"]
        await registry.refresh()
        assert creator.unbound == 1
        assert creator.host is not old_host
        with pytest.raises(PermissionError):
            old_host.get_account_snapshot()
        with pytest.raises(PermissionError):
            creator.host.get_account_snapshot()
    asyncio.run(scenario())


def test_concurrent_discovery_binds_each_instance_once():
    async def scenario():
        creator = CreatorExtension()
        calls = []
        async def bind(host):
            calls.append(host)
            await asyncio.sleep(0)
            creator.host = host
        creator.bind_bilibot_host = bind
        registry, _ = make_dispatcher(Context([Metadata(creator)]))
        await asyncio.gather(registry.refresh(), registry.refresh())
        assert len(calls) == 1
    asyncio.run(scenario())


def test_async_extension_timeout_does_not_block_healthy_extensions(monkeypatch):
    import core.extensions.registry as registry_module
    import core.extensions.dispatcher as dispatcher_module
    monkeypatch.setattr(registry_module, "HOOK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(dispatcher_module, "REQUEST_TIMEOUT_SECONDS", 0.01)
    async def never(*args):
        await asyncio.Event().wait()
    async def scenario():
        broken = CreatorExtension()
        broken.get_bilibot_extension_manifest = never
        healthy = CreatorExtension()
        registry, dispatcher = make_dispatcher(Context([Metadata(broken), Metadata(healthy)]))
        assert list(await registry.refresh()) == ["creator"]
        healthy.handle_bilibot_extension_request = never
        with pytest.raises(TimeoutError):
            await dispatcher.dispatch("creator", "page:dashboard")
    asyncio.run(scenario())


@pytest.mark.parametrize("query", ["", "draft"])
def test_creator_recall_excludes_private_media_and_other_extensions(query):
    class ScopedMemoryAPI:
        rows = [
            {"rpid": "public", "memory_type": "video", "scope": "proactive", "text": "public"},
            {"rpid": "private", "memory_type": "video", "scope": "bili_dm", "text": "private"},
            {"rpid": "own", "memory_type": "creator", "scope": "self", "source": "bilibot_extension:creator", "text": "own draft"},
            {"rpid": "other", "memory_type": "creator", "scope": "self", "source": "bilibot_extension:other", "text": "other draft"},
        ]
        def get_recent_memories(self, *, reader_scope, memory_types, source=None, **kwargs):
            return [item for item in self.rows if item["memory_type"] in memory_types
                    and (reader_scope == "admin" or item["scope"] in {"proactive", "bili_comment"})
                    and (source is None or item.get("source") == source)]
        def search(self, query, **kwargs):
            return self.get_recent_memories(**kwargs)
    plugin = HostPlugin()
    plugin.memory_api = ScopedMemoryAPI()
    creator = CreatorExtension()
    _, dispatcher = make_dispatcher(Context([Metadata(creator)]), plugin)
    asyncio.run(dispatcher.list_extensions())
    rows = asyncio.run(creator.host.read_creator_memory(query))
    assert {row["rpid"] for row in rows} == {"public", "own"}


def test_host_uses_plugin_relative_config_in_installed_namespace(tmp_path, monkeypatch, isolated_config):
    import importlib
    package_name = "deployed_bilibot_test"
    root = Path(__file__).resolve().parents[1]
    for name, folder in [(package_name, root), (package_name + ".core", root / "core")]:
        package = types.ModuleType(name)
        package.__path__ = [str(folder)]
        monkeypatch.setitem(sys.modules, name, package)
    config = types.ModuleType(package_name + ".core.config")
    config.DATA_DIR = tmp_path / "installed"
    config.DATA_DIR.mkdir()
    monkeypatch.setitem(sys.modules, config.__name__, config)
    module = importlib.import_module(package_name + ".core.extensions.host_api")
    permission_module = importlib.import_module(package_name + ".core.extensions.permissions")
    grant = permission_module.ExtensionGrant.create("creator", ["activity.write"])
    plugin = HostPlugin()
    host = module.BiliBotExtensionHostAPI(grant, plugin, reject_write)
    host.record_creator_activity({"title": "test"})
    # 扩展动态记在插件自己的小状态里（数据库或 json 退路），不再按 config 拼路径
    assert plugin.states.creator_activity.read([])[0]["title"] == "test"


def test_link_previews_are_not_reported_as_watched_signals(isolated_config):
    Path(isolated_config.VIDEO_MEMORY_FILE).write_text(json.dumps({
        "BVpreview": {"title": "preview only", "content_analyzed": False},
        "BVlegacy": {"title": "legacy preview", "source": "group_share"},
        "BVwatched": {"title": "watched", "content_analyzed": True, "owner_name": "UP", "owner_mid": "42"},
    }), encoding="utf-8")
    creator = CreatorExtension()
    _, dispatcher = make_dispatcher(Context([Metadata(creator)]))
    asyncio.run(dispatcher.list_extensions())
    signals = creator.host.list_creator_signals()
    assert len(signals) == 1
    assert signals[0]["source_ref"] == "BVwatched"
    assert signals[0]["up_name"] == "UP"
