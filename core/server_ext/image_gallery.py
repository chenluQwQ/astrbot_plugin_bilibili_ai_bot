"""图库淘汰与历史保留；文件/选图向量归 BiliBot，历史不随缓存清理。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from ..config import DATA_DIR, PROACTIVE_LOG_FILE
from .compat import activity_entries
from .comment_media import comment_key, fingerprint, merge_images, normalize_images, publication_status

GALLERY_LIMIT = 150
_LOCK = asyncio.Lock()


def _paths():
    root = Path(DATA_DIR) / "liked_images"
    files = root / "files"
    for path in (Path(DATA_DIR), root, files):
        if path.is_symlink():
            raise ValueError("图库目录不能是符号链接")
    return root, files


@contextmanager
def _db(*, create=False):
    root, files = _paths()
    if create:
        files.mkdir(parents=True, exist_ok=True)
    path = root / "gallery.sqlite3"
    if path.is_symlink():
        raise ValueError("图库数据库不能是符号链接")
    if not path.exists() and not create:
        yield None
        return
    new = not path.exists()
    db = sqlite3.connect(str(path), timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA synchronous=FULL")
        if create or not new:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL UNIQUE, snapshot TEXT NOT NULL,
                    embedding TEXT, status TEXT NOT NULL DEFAULT 'available'
                );
                CREATE TABLE IF NOT EXISTS evictions (
                    asset_id TEXT PRIMARY KEY, evicted_at REAL NOT NULL,
                    reason TEXT NOT NULL, cleanup_done INTEGER NOT NULL DEFAULT 0,
                    notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS history_events (
                    scope TEXT NOT NULL, event_key TEXT NOT NULL,
                    record TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(scope, event_key)
                );
                CREATE TABLE IF NOT EXISTS captions (
                    sha256 TEXT PRIMARY KEY, day TEXT NOT NULL, result TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS collection_urls (
                    url TEXT PRIMARY KEY, sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS comment_attempts (
                    target TEXT PRIMARY KEY, state TEXT NOT NULL,
                    rpid TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL
                );
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(assets)")}
            for name, definition in (
                ("model_key", "TEXT NOT NULL DEFAULT ''"),
                ("likes", "REAL NOT NULL DEFAULT 0"),
                ("uses", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "REAL NOT NULL DEFAULT 0"),
                ("last_used", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE assets ADD COLUMN {name} {definition}")
            attempt_columns = {r[1] for r in db.execute("PRAGMA table_info(comment_attempts)")}
            if "selection" not in attempt_columns:
                db.execute("ALTER TABLE comment_attempts ADD COLUMN selection TEXT NOT NULL DEFAULT '{}'")
            db.commit()
            if new:
                os.chmod(path, 0o600)
        yield db
    finally:
        db.close()


def _filename(name):
    # 只允许图库专用 files 下的单层文件名；不接受绝对路径或目录穿越。
    if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{64}\.(jpg|jpeg|png|webp|gif)", name):
        raise ValueError("图片文件名必须是 SHA256.图片扩展名")
    return name


@contextmanager
def _file_dir():
    _, files = _paths()
    # Linux/macOS 下通过目录句柄操作，避免校验后路径被替换为链接。
    fd = os.open(files, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def register_asset(bot, *, file_name: str, image: dict, embedding=None, model_key="") -> dict:
    """未来采集器的入库入口；调用者先把图片放到 liked_images/files。"""
    from .livingmemory_bridge import _clip
    name = _filename(file_name)
    snapshots = normalize_images([image], published=False, clip=_clip)
    if not snapshots or not snapshots[0]["asset_id"]:
        raise ValueError("图片缺少 asset_id")
    snapshot = snapshots[0]
    with _file_dir() as directory:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("图片必须是普通文件")
            sha = hashlib.file_digest(source, "sha256").hexdigest()
    if name.split(".", 1)[0] != sha or (snapshot["sha256"] and snapshot["sha256"] != sha):
        raise ValueError("文件内容与 SHA256 不一致")
    snapshot.update(sha256=sha, local_status="available")
    snapshot["source_bvid"] = _clip(image.get("source_bvid"), 32)
    if embedding is not None and (not isinstance(embedding, list) or
            not embedding or any(type(x) not in (int, float) for x in embedding)):
        raise ValueError("选图向量必须是数值列表")
    # 墓碑不可自动复活；历史采集重试也不能重新使用已淘汰图片。
    from .livingmemory_bridge import _eviction_state
    tombstones = _eviction_state(bot)
    if snapshot["asset_id"] in tombstones or sha in tombstones:
        raise ValueError("该图片已淘汰，不能自动重新入库")
    with _db(create=True) as db, db:
        old = db.execute("SELECT * FROM assets WHERE asset_id=? OR sha256=?",
                         (snapshot["asset_id"], sha)).fetchone()
        if old:
            if old["status"] != "available":
                raise ValueError("该图片正在淘汰或已淘汰")
            if old["asset_id"] != snapshot["asset_id"] or old["sha256"] != sha:
                raise ValueError("图片身份冲突")
            return {"ok": True, "existing": True, "asset_id": old["asset_id"]}
        count = db.execute("SELECT count(*) FROM assets WHERE status='available'").fetchone()[0]
        if count >= GALLERY_LIMIT:
            raise ValueError("图库已满 150 张，请先通过安全淘汰入口释放名额")
        db.execute("INSERT INTO assets(asset_id,sha256,file_name,snapshot,embedding,model_key,likes,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (snapshot["asset_id"], sha, name, json.dumps(snapshot, ensure_ascii=False),
                    json.dumps(embedding, allow_nan=False) if embedding is not None else None,
                    str(model_key)[:128], max(0, min(10, float(image.get("liking", 5)))), time.time()))
    return {"ok": True, "existing": False, "asset_id": snapshot["asset_id"]}


def _read_comment_log(bot):
    # 正式删除前严格读取；优先读 2.0.2 的 sqlite 活动日志。
    entries = activity_entries(bot, "proactive", PROACTIVE_LOG_FILE)
    if entries:
        return entries
    path = Path(PROACTIVE_LOG_FILE)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 日志损坏不能被空列表兜底掩盖，否则会带着不完整历史删文件。
        raise ValueError(f"主动评论日志读取失败，暂停图库淘汰: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("主动评论日志格式无效，暂停图库淘汰")
    return data


async def _prepare(bot, asset_id, reason):
    from . import livingmemory_bridge as bridge
    with _db() as db:
        row = db.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone() if db else None
        if row is None:
            raise ValueError("图片尚未登记到图库")
        if db.execute("SELECT 1 FROM evictions WHERE asset_id=?", (asset_id,)).fetchone():
            return
        asset = dict(row)
    _filename(asset["file_name"])
    snapshot = json.loads(asset["snapshot"])
    if not isinstance(snapshot, dict):
        raise ValueError("图库历史快照格式无效")
    records = []
    for rec in _read_comment_log(bot):
        if not isinstance(rec, dict) or publication_status(rec) != "published":
            continue
        if str(rec.get("root") or "0") != "0" or str(rec.get("parent") or "0") != "0":
            continue
        images = normalize_images(rec.get("images"), published=True, clip=bridge._clip)
        if not any(x.get("asset_id") == asset_id or x.get("sha256") == asset["sha256"] for x in images):
            continue
        # 保存评论当时的完整语义和全组图片，不能用图库当前描述改写旧经历。
        event = {key: bridge._clip(rec.get(key), limit) for key, limit in (
            ("comment", 4000), ("title", 120), ("bvid", 32), ("rpid", 32), ("time", 32))}
        event.update(send_status="published", root=0, parent=0, images=images)
        records.append(event)
    umo = bridge.target_umo(bot)
    if records and not umo:
        raise ValueError("有待保存的已发布图片评论，请先配置记忆目标会话")
    persona = await bridge._resolve_persona_id(bot.context, umo) if records else None
    scope = fingerprint([umo, persona])
    # 一个本地事务先保存历史及恢复任务，然后才允许任何文件清理。
    with _db() as db, db:
        for rec in records:
            key = comment_key(rec)
            old = db.execute("SELECT record FROM history_events WHERE scope=? AND event_key=?",
                             (scope, key)).fetchone()
            if old:
                retained = json.loads(old["record"])
                retained["images"] = merge_images(retained.get("images"), rec["images"])
                rec = retained
            db.execute("INSERT INTO history_events(scope,event_key,record) VALUES(?,?,?) "
                       "ON CONFLICT(scope,event_key) DO UPDATE SET "
                       "delivered=CASE WHEN record<>excluded.record THEN 0 ELSE delivered END,"
                       "record=excluded.record",
                       (scope, key, json.dumps(rec, ensure_ascii=False)))
        now = time.time()
        snapshot.update(local_status="evicted", evicted_at=now)
        db.execute("INSERT INTO evictions(asset_id,evicted_at,reason) VALUES(?,?,?)",
                   (asset_id, now, bridge._clip(reason, 160)))
        db.execute("UPDATE assets SET status='evicting',snapshot=? WHERE asset_id=?",
                   (json.dumps(snapshot, ensure_ascii=False), asset_id))


async def _finish(bot, asset_id):
    from .livingmemory_bridge import mark_asset_evicted
    with _db() as db:
        row = db.execute("SELECT a.*,e.evicted_at,e.cleanup_done,e.notified FROM assets a JOIN evictions e "
                         "USING(asset_id) WHERE asset_id=?", (asset_id,)).fetchone()
        asset = dict(row)
    if asset["cleanup_done"] and asset["notified"]:
        return {"ok": True, "asset_id": asset_id, "local_status": "evicted", "pending": False}
    # 墓碑落盘失败时抛出异常，文件原地保留；LM 离线不影响已经保存的本地历史。
    result = await mark_asset_evicted(bot, asset_id, sha256=asset["sha256"], evicted_at=asset["evicted_at"])
    if not result.get("ok"):
        raise RuntimeError(result.get("reason") or "淘汰标记保存失败")
    if not asset["cleanup_done"]:
        name = _filename(asset["file_name"])
        with _file_dir() as directory:
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("拒绝清理符号链接或非普通文件")
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                with os.fdopen(fd, "rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != asset["sha256"]:
                        raise ValueError("图片内容已变化，暂停清理")
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
            except FileNotFoundError:
                pass  # 断电发生在 unlink 后、状态更新前，重试仍可完成。
    with _db() as db, db:
        db.execute("UPDATE assets SET status='evicted',embedding=NULL WHERE asset_id=?", (asset_id,))
        db.execute("UPDATE evictions SET cleanup_done=1,notified=? WHERE asset_id=?",
                   (int(not result.get("pending")), asset_id))
    return {"ok": True, "asset_id": asset_id, "local_status": "evicted",
            "pending": bool(result.get("pending")), "updated": result.get("updated", 0)}


async def evict_asset(bot, asset_id: str, *, reason="manual") -> dict:
    """手动后台和未来自动淘汰共用入口；不允许直接 unlink 绕过历史保存。"""
    async with _LOCK:
        try:
            if not isinstance(asset_id, str) or not asset_id.strip():
                raise ValueError("asset_id 不能为空")
            await _prepare(bot, asset_id, reason)
            result = await _finish(bot, asset_id)
            result["history_pending"] = status()["history_pending"]
            return result
        except Exception as exc:
            return {"ok": False, "asset_id": asset_id, "pending": True, "reason": str(exc)[:200]}


async def recover(bot) -> dict:
    """自动同步前/手动重试时续跑，已落盘任务不会因插件重载消失。"""
    async with _LOCK:
        with _db() as db:
            ids = [r[0] for r in db.execute(
                "SELECT asset_id FROM evictions WHERE cleanup_done=0 OR notified=0")] if db else []
        failed, pending = 0, 0
        for asset_id in ids:
            try:
                result = await _finish(bot, asset_id)
                pending += int(result.get("pending", False))
            except Exception:
                failed += 1
        return {"ok": failed == 0, "retried": len(ids), "failed": failed, "pending": pending}


def pending_events(scope: str) -> list[dict]:
    with _db() as db:
        return [json.loads(r[0]) for r in db.execute(
            "SELECT record FROM history_events WHERE scope=? AND delivered=0", (scope,))] if db else []


def acknowledge_event(scope: str, key: str):
    with _db() as db:
        if db:
            with db:
                db.execute("UPDATE history_events SET delivered=1 WHERE scope=? AND event_key=?", (scope, key))


def list_assets(*, include_evicted=False):
    with _db() as db:
        if not db:
            return []
        rows = db.execute("SELECT * FROM assets " + ("" if include_evicted else "WHERE status='available' ")
                          + "ORDER BY created_at DESC LIMIT 300").fetchall()
        return [dict(r) | {"image": json.loads(r["snapshot"])} for r in rows]


async def retain_publication(bot, record, *, scope=None):
    """确认发送成功后立即留存，避免短期日志截断导致旧配图经历丢失。"""
    from . import livingmemory_bridge as bridge
    if publication_status(record) != "published" or not record.get("images"):
        return
    umo = bridge.target_umo(bot)
    if not umo:
        raise ValueError("配图评论需要配置 LivingMemory 目标会话")
    if scope is None:
        persona = await bridge._resolve_persona_id(bot.context, umo)
        scope = fingerprint([umo, persona])
    images = normalize_images(record["images"], published=True, clip=bridge._clip)
    record = dict(record, images=images, root=0, parent=0)
    with _db(create=True) as db, db:
        key = comment_key(record)
        existed = db.execute("SELECT 1 FROM history_events WHERE scope=? AND event_key=?", (scope, key)).fetchone()
        db.execute("INSERT INTO history_events(scope,event_key,record) VALUES(?,?,?) "
                   "ON CONFLICT(scope,event_key) DO UPDATE SET record=excluded.record,delivered=0",
                   (scope, key, json.dumps(record, ensure_ascii=False)))
        if not existed:
            for image in images:
                db.execute("UPDATE assets SET uses=uses+1,last_used=? WHERE asset_id=?",
                           (time.time(), image["asset_id"]))


def status() -> dict:
    """只读统计；不会为查看状态创建空图库，也不暴露本地路径。"""
    with _db() as db:
        if db is None:
            return {"limit": GALLERY_LIMIT, "available": 0, "evicted": 0,
                    "cleanup_pending": 0, "notify_pending": 0, "history_events": 0,
                    "history_pending": 0, "send_unknown": 0}
        count = lambda sql: db.execute(sql).fetchone()[0]
        return {"limit": GALLERY_LIMIT,
                "available": count("SELECT count(*) FROM assets WHERE status='available'"),
                "evicted": count("SELECT count(*) FROM assets WHERE status='evicted'"),
                "cleanup_pending": count("SELECT count(*) FROM evictions WHERE cleanup_done=0"),
                "notify_pending": count("SELECT count(*) FROM evictions WHERE notified=0"),
                "history_events": count("SELECT count(*) FROM history_events"),
                "send_unknown": count("SELECT count(*) FROM comment_attempts WHERE state='unknown'"),
                "history_pending": count("SELECT count(*) FROM history_events WHERE delivered=0")}
