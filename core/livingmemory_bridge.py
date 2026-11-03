"""BiliBot → LivingMemory 长期记忆联动。

只同步「已经过 BiliBot 安全过滤与内容评估」的记录：
- 看过的视频（含评分、想法、留下的公开评论）
- 评论区回复
- B站私信回复

写入复用 LivingMemory 自己的 ``MemoryProcessor`` + ``MemoryEngine``，
所以向量化、去重、原子与图谱索引都由它负责，BiliBot 只供给结构化事实。
同步以事件键和内容指纹去重；新评论/图片信息能补齐已经同步的记录。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta

from astrbot.api import logger

from .config import DATA_DIR, PROACTIVE_LOG_FILE, REPLY_LOG_FILE, WATCH_LOG_FILE
from .security.redact import redact_outbound, redact_pii
from .comment_media import (
    apply_evictions, comment_key, fingerprint, merge_images,
    normalize_images, publication_status,
)

LIVINGMEMORY_PLUGIN = "astrbot_plugin_livingmemory"
SYNC_STATE_FILE = os.path.join(DATA_DIR, "livingmemory_sync_state.json")
MEDIA_STATE_FILE = os.path.join(DATA_DIR, "comment_media_state.json")
_SYNC_LOCK = asyncio.Lock()
SYNC_SCHEMA = 3

DEFAULT_WINDOW_DAYS = 3
MAX_SYNC_KEYS = 4000
MAX_SUMMARY_CHARS = 400
MAX_FACT_CHARS = 160
BILI_VIDEO_URL = "https://www.bilibili.com/video/{bvid}"

# bilibot_kind → livingmemory 记忆类型(memory_type)。
# 统一到 livingmemory 的新命名(FACTUAL/EPISODIC/...)，避免与旧命名(FACT/EVENT)重复。
_BILIBOT_TYPE_MAP = {
    "watch": "OPINION",    # 看完视频的观后感 / 评分
    "reply": "EVENT",      # 评论区回复
    "private": "EVENT",    # B站私信回复
    "comment": "EVENT",    # 独立主动评论及配图快照
}


def _memory_type_for(item: dict) -> str:
    """根据 bilibot_kind 推断该记忆在 livingmemory 里的 memory_type。"""
    kind = str((item.get("metadata") or {}).get("bilibot_kind") or "").strip().lower()
    return _BILIBOT_TYPE_MAP.get(kind, "GENERAL")


# ══════════════════════════════════════
#  配置与状态
# ══════════════════════════════════════
def _raw_config(bot, key, default=None):
    try:
        value = bot.config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _bool(bot, key, default=False) -> bool:
    value = _raw_config(bot, key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "启用"}


def _int(bot, key, default=0) -> int:
    try:
        return int(float(_raw_config(bot, key, default) or 0))
    except (TypeError, ValueError):
        return default


def _load_state(bot) -> dict:
    try:
        state = bot._load_json(SYNC_STATE_FILE, {})
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(bot, state: dict) -> bool:
    try:
        bot._save_json(SYNC_STATE_FILE, state)
        return True
    except Exception as exc:
        logger.warning(f"[BiliBot] 记忆联动状态写入失败: {exc}")
        return False


def _livingmemory(bot):
    """拿到 LivingMemory 的插件实例。

    ``context.get_registered_star`` 返回的是 StarMetadata，插件实例在
    ``star_cls`` 上；未激活时 star_cls 为 None。
    """
    try:
        metadata = bot.context.get_registered_star(LIVINGMEMORY_PLUGIN)
    except Exception:
        return None
    if metadata is None:
        return None
    if getattr(metadata, "activated", True) is False:
        return None
    return getattr(metadata, "star_cls", None)


def _engine(lm) -> tuple[object | None, object | None]:
    initializer = getattr(lm, "initializer", None)
    engine = getattr(initializer, "memory_engine", None)
    processor = getattr(initializer, "memory_processor", None)
    if engine is None or processor is None:
        return None, None
    if getattr(engine, "index_writer", None) is None:
        return None, None
    return engine, processor


def target_umo(bot) -> str:
    """记忆写入的会话标识；默认跟随主人 QQ 私信会话。"""
    from .umo import normalize_umo

    configured = str(_raw_config(bot, "LIVINGMEMORY_SESSION_UMO", "") or "").strip()
    if not configured:
        configured = str(_raw_config(bot, "OWNER_QQ_UMO", "") or "").strip()
    if not configured:
        return ""
    return normalize_umo(bot.context, configured)


async def _resolve_persona_id(context, umo: str) -> str | None:
    """与 AstrBot 主流程一致的三级优先级，但没有 event 可用。"""
    try:
        from astrbot.api import sp

        session_cfg = await sp.get_async(
            scope="umo", scope_id=umo, key="session_service_config", default={}
        )
        persona_id = (session_cfg or {}).get("persona_id")
        if persona_id:
            return persona_id
    except Exception:
        pass
    try:
        manager = context.conversation_manager
        conversation_id = await manager.get_curr_conversation_id(umo)
        if conversation_id:
            conversation = await manager.get_conversation(umo, conversation_id)
            persona_id = getattr(conversation, "persona_id", None)
            if persona_id == "[%None]":
                return None
            if persona_id:
                return persona_id
    except Exception:
        pass
    try:
        default_persona = await context.persona_manager.get_default_persona_v3(umo=umo)
        return (default_persona or {}).get("name")
    except Exception:
        return None


# ══════════════════════════════════════
#  记录 → 结构化记忆
# ══════════════════════════════════════
def _to_datetime(value) -> datetime | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _clip(text, limit: int) -> str:
    content, _hits = redact_outbound(str(text or "").strip().replace("\n", " "), internal=False)
    return content[:limit]


def _video_url(bvid: str) -> str:
    return BILI_VIDEO_URL.format(bvid=bvid) if bvid else ""


def _watch_item(rec: dict) -> dict | None:
    """把一条观看记录转成记忆素材；分析失败或没内容的直接跳过。"""
    if str(rec.get("evaluation_status") or "") == "failed":
        return None
    title = _clip(rec.get("title"), 120)
    if not title:
        return None
    score = rec.get("score")
    try:
        score_value = float(score or 0)
    except (TypeError, ValueError):
        score_value = 0.0
    thought = _clip(rec.get("score_reason"), MAX_FACT_CHARS)
    review = _clip(rec.get("review"), MAX_FACT_CHARS)
    comment = _clip(rec.get("comment"), MAX_FACT_CHARS)
    if comment == "评价失败":
        comment = ""
    if score_value <= 0 and not (thought or review):
        return None

    up_name = _clip(rec.get("up_name"), 40)
    tname = _clip(rec.get("tname"), 24)
    bvid = str(rec.get("bvid") or "").strip()
    mood = _clip(rec.get("mood"), 12)
    url = _video_url(bvid)

    head = f"在B站看了《{title}》"
    if up_name:
        head += f"（UP主 {up_name}{f'，{tname}' if tname else ''}）"
    elif tname:
        head += f"（{tname}）"
    parts = [head + "。"]
    if score_value:
        parts.append(f"评分 {score_value:g}/10，看时心情{mood or '平静'}。")

    facts = []
    if thought:
        facts.append(f"看完的想法：{thought}")
    if review:
        facts.append(f"感受：{review}")
    if comment:
        label = "已发布主动评论" if rec.get("_comment_published") else "本地评论草稿（未确认发布）"
        facts.append(f"{label}：{comment}")
    if url:
        facts.append(f"视频链接：{url}")

    topics = [t for t in (tname, up_name) if t]
    for keyword in (rec.get("search_keywords") or [])[:2]:
        keyword = _clip(keyword, 20)
        if keyword:
            topics.append(keyword)

    importance = 0.3 + min(score_value, 10) / 20
    if comment:
        importance += 0.1

    return {
        "key": f"watch|{bvid}|{rec.get('time')}",
        "structured": {
            "summary": "".join(parts)[:MAX_SUMMARY_CHARS],
            "key_facts": facts[:5],
            "topics": topics[:5],
            "sentiment": "positive" if score_value >= 7 else "neutral",
            "importance": round(min(importance, 0.9), 2),
        },
        "metadata": {
            "bilibot_kind": "watch",
            "bilibot_bvid": bvid,
            "bilibili_url": url,
            "bilibot_score": score_value or None,
            "bilibot_source": str(rec.get("source") or "")[:24],
            "bilibot_event_time": str(rec.get("time") or ""),
        },
    }


def _reply_item(rec: dict, titles: dict[str, str]) -> dict | None:
    content = _clip(rec.get("content"), MAX_FACT_CHARS)
    reply = _clip(rec.get("reply"), MAX_FACT_CHARS)
    if not reply:
        return None
    bvid = str(rec.get("bvid") or "").strip()
    title = _clip(titles.get(bvid, ""), 120)
    username = _clip(rec.get("username"), 32) or "对方"
    is_private = str(rec.get("channel") or "").strip() == "private"
    url = _video_url(bvid)

    if is_private:
        summary = f"B站私信里，{username}说「{content}」，我回「{reply}」。"
        kind = "private"
        importance = 0.5
    else:
        where = f"《{title}》的评论区" if title else "B站评论区"
        summary = f"在{where}回复 {username}：对方说「{content}」，我回「{reply}」。"
        kind = "reply"
        importance = 0.45

    facts = [f"对方原文：{content}" if content else "对方原文未记录", f"我的回复：{reply}"]
    if title:
        facts.append(f"视频：{title}")
    if url:
        facts.append(f"视频链接：{url}")

    return {
        "key": f"{kind}|{rec.get('rpid') or bvid}|{rec.get('time')}",
        "structured": {
            "summary": summary[:MAX_SUMMARY_CHARS],
            "key_facts": facts[:5],
            "topics": [t for t in (title[:20], username) if t][:5],
            "sentiment": "neutral",
            "importance": importance,
        },
        "metadata": {
            "bilibot_kind": kind,
            "bilibot_bvid": bvid,
            "bilibili_url": url,
            "bilibot_mid": str(rec.get("mid") or "")[:24],
        },
    }


def _comment_map(bot, since: datetime) -> dict[str, dict]:
    """视频 → 该视频今天留下的主动评论（按时间取最后一条）。"""
    result: dict[str, dict] = {}
    try:
        entries = bot._load_json(PROACTIVE_LOG_FILE, [])
    except Exception:
        entries = []
    for rec in entries if isinstance(entries, list) else []:
        if not isinstance(rec, dict):
            continue
        if publication_status(rec) not in {"published", "legacy_confirmed"}:
            continue
        parsed = _to_datetime(rec.get("time"))
        if parsed is None or parsed < since:
            continue
        bvid = str(rec.get("bvid") or "").strip()
        if bvid and str(rec.get("time") or "") >= str(result.get(bvid, {}).get("time") or ""):
            result[bvid] = rec
    return result


def _collect(bot, since: datetime, synced: set[str]) -> list[dict]:
    items: list[dict] = []
    titles: dict[str, str] = {}

    try:
        watch_log = bot._load_json(WATCH_LOG_FILE, [])
    except Exception:
        watch_log = []
    comments = _comment_map(bot, since)

    for rec in watch_log if isinstance(watch_log, list) else []:
        if not isinstance(rec, dict):
            continue
        parsed = _to_datetime(rec.get("time"))
        if parsed is None or parsed < since:
            continue
        bvid = str(rec.get("bvid") or "").strip()
        if bvid and rec.get("title"):
            titles[bvid] = str(rec.get("title"))
        merged = dict(rec)
        if bvid and bvid in comments:
            merged["comment"] = comments[bvid].get("comment") or merged.get("comment")
            merged["_comment_published"] = True
        item = _watch_item(merged)
        if item:
            items.append(item)

    # 新协议评论各自建事件；不重复拆分只有纯文本的旧历史日志。
    for rec in _comment_entries(bot):
        parsed = _to_datetime(rec.get("time"))
        if parsed is not None and parsed >= since and (
            rec.get("images") or rec.get("send_status") or rec.get("rpid")
        ):
            item = _comment_item(rec)
            if item:
                items.append(item)

    try:
        reply_log = bot._load_json(REPLY_LOG_FILE, [])
    except Exception:
        reply_log = []
    for rec in reply_log if isinstance(reply_log, list) else []:
        if not isinstance(rec, dict):
            continue
        parsed = _to_datetime(rec.get("time"))
        if parsed is None or parsed < since:
            continue
        item = _reply_item(rec, titles)
        if item and item["key"] not in synced:
            items.append(item)

    return items


def _comment_entries(bot) -> list[dict]:
    entries = bot._load_json(PROACTIVE_LOG_FILE, [])
    return [x for x in entries if isinstance(x, dict)] if isinstance(entries, list) else []


def _comment_item(rec: dict, images: list[dict] | None = None) -> dict | None:
    comment = _clip(rec.get("comment"), MAX_FACT_CHARS)
    if not comment:
        return None
    status = publication_status(rec)
    # 被动回复/楼中楼不能携图；即使外部日志误填了 images 也不采信。
    top_level = str(rec.get("root") or "0") == "0" and str(rec.get("parent") or "0") == "0"
    if images is None:
        images = normalize_images(
            rec.get("images") if top_level else [],
            published=status == "published", clip=_clip,
        )
    title = _clip(rec.get("title"), 100)
    bvid = str(rec.get("bvid") or "").strip()
    where = f"《{title}》评论区" if title else "B站评论区"
    labels = {"published": "已发布", "legacy_confirmed": "旧日志确认成功",
              "draft": "本地草稿，未发布", "failed": "发送失败", "unknown": "发布结果未知"}
    facts = [f"{labels[status]}：{comment}"]
    if images:
        descriptions = []
        for image in images:
            parts = [image.get("description") or "图片内容未记录"]
            for label, key in (("情绪", "emotion"), ("使用原因", "use_reason")):
                if image.get(key):
                    parts.append(f"{label}：{image[key]}")
            descriptions.append("；".join(parts))
        facts.append("配图含义：" + " / ".join(descriptions)[:800])
    if bvid:
        facts.append(f"视频链接：{_video_url(bvid)}")
    return {
        "key": comment_key(rec),
        "structured": {
            "summary": f"在{where}的主动评论记录。",
            "key_facts": facts, "topics": [title[:20]] if title else ["B站评论"],
            "sentiment": "neutral", "importance": 0.55,
        },
        "metadata": {
            "bilibot_kind": "comment", "bilibot_bvid": bvid,
            "bilibili_url": _video_url(bvid),
            "bilibot_event_time": str(rec.get("time") or ""),
            "bilibot_rpid": str(rec.get("rpid") or ""),
            "bilibot_send_status": status, "bilibot_images": images,
        },
        "record": rec,
    }


def _metadata(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


async def _source_records(engine, umo: str, persona_id) -> list[dict]:
    """只读原始记录；所有写入仍经 LivingMemory 引擎，保持索引一致。"""
    db = getattr(engine, "db_connection", None)
    if db is None:
        raise RuntimeError("LivingMemory 数据库尚未就绪，暂停同步避免重复写入")
    cursor = await db.execute(
        "SELECT id, text, metadata FROM documents "
        "WHERE json_extract(metadata, '$.memory_origin') = 'bilibot' "
        "AND json_extract(metadata, '$.session_id') = ? "
        "AND COALESCE(json_extract(metadata, '$.persona_id'), '') = ?",
        (umo, persona_id or ""),
    )
    rows = await cursor.fetchall()
    return [dict(row, metadata=_metadata(row["metadata"])) for row in rows]


def _find_existing(item: dict, records: list[dict]) -> dict | None:
    exact = [r for r in records if r["metadata"].get("bilibot_sync_key") == item["key"]]
    if len(exact) > 1:
        raise RuntimeError("事件键存在多条记录，需人工核对")
    if exact:
        return exact[0]
    # 老版本没保存事件键：只在类型、视频、摘要均唯一匹配时接管原文档。
    meta = item["metadata"]
    legacy = [r for r in records if not r["metadata"].get("bilibot_sync_key")
              and r["metadata"].get("bilibot_kind") == meta["bilibot_kind"]
              and r["metadata"].get("bilibot_bvid") == meta.get("bilibot_bvid")
              and (r["metadata"].get("persona_summary") == item["structured"]["summary"]
                   or r["text"].split(" | ", 1)[0] == item["structured"]["summary"])]
    if len(legacy) > 1:
        if len({r["text"] for r in legacy}) == 1:
            # 旧版重复导入的同文副本：只接管最早一条，不删除其他历史记录。
            return min(legacy, key=lambda r: r["id"])
        raise RuntimeError("旧观看记录有多个候选，暂停自动更新")
    return legacy[0] if legacy else None


def _eviction_state(bot) -> dict:
    # 墓碑损坏不能按空图库处理，否则会丢失历史淘汰状态。
    if os.path.exists(MEDIA_STATE_FILE):
        with open(MEDIA_STATE_FILE, "r", encoding="utf-8") as source:
            raw = json.load(source)
    else:
        raw = bot._load_json(MEDIA_STATE_FILE, {})
    if not isinstance(raw, dict) or not isinstance(raw.get("evicted", {}), dict):
        raise ValueError("图片淘汰状态文件格式无效")
    evicted = raw.get("evicted", {})
    if any(not isinstance(v, (int, float)) for v in evicted.values()):
        raise ValueError("图片淘汰时间无效")
    return evicted


async def _reconcile_evictions(engine, records: list[dict], evicted: dict, *, dry_run=False) -> int:
    updated = 0
    for record in records:
        images = record["metadata"].get("bilibot_images")
        if not isinstance(images, list) or not images:
            continue
        retained = apply_evictions(images, evicted)
        if retained == images:
            continue
        if not dry_run and not await engine.update_memory(record["id"], {"metadata": {"bilibot_images": retained}}):
            raise RuntimeError(f"图片淘汰状态更新失败（memory_id={record['id']}）")
        record["metadata"]["bilibot_images"] = retained
        updated += 1
    return updated


async def mark_asset_evicted(bot, asset_id: str, *, sha256: str = "", evicted_at: float | None = None) -> dict:
    """图库删除文件前调用：先持久化墓碑，再补历史引用；不删除任何记忆。"""
    if not str(asset_id or "").strip():
        return {"ok": False, "reason": "asset_id 不能为空"}
    async with _SYNC_LOCK:
        evicted = _eviction_state(bot)
        now = evicted_at if evicted_at is not None else time.time()
        for key in (asset_id, sha256):
            if key:
                evicted.setdefault(str(key), now)
        # 保存失败必须阻止图库继续删除；LM 暂时离线则下一次同步补记状态。
        bot._save_json(MEDIA_STATE_FILE, {"schema": 1, "evicted": evicted})
        if os.path.exists(MEDIA_STATE_FILE):
            # 原子 replace 后同步目录项，确保墓碑先于图片删除持久化。
            directory = os.open(os.path.dirname(MEDIA_STATE_FILE) or ".", os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        lm = _livingmemory(bot)
        engine, _ = _engine(lm) if lm is not None else (None, None)
        if engine is None:
            return {"ok": True, "pending": True, "updated": 0}
        try:
            # 图库是共享资源，淘汰要覆盖所有会话/人格的历史引用。
            cursor = await engine.db_connection.execute(
                "SELECT id, text, metadata FROM documents "
                "WHERE json_extract(metadata, '$.memory_origin') = 'bilibot'"
            )
            records = [dict(r, metadata=_metadata(r["metadata"])) for r in await cursor.fetchall()]
            updated = await _reconcile_evictions(engine, records, evicted)
            return {"ok": True, "pending": False, "updated": updated}
        except Exception as exc:
            logger.warning(f"[BiliBot] 图片已记录淘汰，历史引用稍后补记: {exc}")
            return {"ok": True, "pending": True, "updated": 0}


# ══════════════════════════════════════
#  对外接口
# ══════════════════════════════════════
async def sync(bot, *, days: int | None = None, reason: str = "auto", dry_run: bool = False) -> dict:
    """串行同步，避免自动循环和手动同步同时创建相同事件。"""
    from . import image_gallery
    # 先拿图库锁再拿同步锁；recover 内部会调用 mark_asset_evicted，不能倒置。
    recovery = None if dry_run else await image_gallery.recover(bot)
    async with _SYNC_LOCK:
        result = await _sync_locked(bot, days=days, reason=reason, dry_run=dry_run)
        if recovery is not None:
            result["gallery_recovery"] = recovery
        return result


def _prepare_item(item, existing, processor, umo, evicted):
    if item["metadata"].get("bilibot_kind") == "comment":
        old_images = existing["metadata"].get("bilibot_images", []) if existing else []
        images = apply_evictions(merge_images(old_images, item["metadata"]["bilibot_images"]), evicted)
        rec = item["record"]
        if existing and existing["metadata"].get("bilibot_send_status") == "published":
            # 同一 rpid 已确认发布，重试/缺字段不能推翻这次历史成功事实。
            rec = {**rec, "send_status": "published"}
        item = _comment_item(rec, images)
    content, metadata, importance = processor.build_memory_from_structured_data(
        structured_data=item["structured"],
        is_group_chat=False,
        fallback_excerpt=item["structured"].get("summary", ""),
    )
    metadata.update(item["metadata"])
    metadata.update({
        "memory_origin": "bilibot",
        "memory_type": _memory_type_for(item),
        "source_session_id": umo,
        "source_window": {"session_id": umo, "triggered_by": "bilibot_sync"},
        "bilibot_sync_key": item["key"],
        "bilibot_sync_schema": SYNC_SCHEMA,
        "bilibot_content_fingerprint": fingerprint(content),
        "bilibot_sync_fingerprint": fingerprint(item["metadata"]),
    })
    return content, metadata, importance


async def _sync_locked(bot, *, days: int | None = None, reason: str = "auto", dry_run: bool = False) -> dict:
    if not _bool(bot, "LIVINGMEMORY_SYNC_ENABLED", True):
        return {"ok": False, "skipped": "disabled", "reason": "记忆联动已关闭"}

    umo = target_umo(bot)
    if not umo:
        return {"ok": False, "skipped": "no_target", "reason": "未配置 LIVINGMEMORY_SESSION_UMO / OWNER_QQ_UMO"}

    lm = _livingmemory(bot)
    if lm is None:
        return {"ok": False, "skipped": "no_plugin", "reason": "未安装或未启用 LivingMemory 插件"}

    engine, processor = _engine(lm)
    if engine is None or processor is None:
        return {"ok": False, "skipped": "not_ready", "reason": "LivingMemory 记忆引擎尚未就绪"}

    window = days if days is not None else _int(bot, "LIVINGMEMORY_SYNC_WINDOW_DAYS", DEFAULT_WINDOW_DAYS)
    window = max(1, min(int(window or DEFAULT_WINDOW_DAYS), 60))
    since = datetime.now() - timedelta(days=window)

    state = _load_state(bot)
    persona_id = await _resolve_persona_id(bot.context, umo)
    scope = fingerprint([umo, persona_id])
    scopes = state.get("scopes") if isinstance(state.get("scopes"), dict) else {}
    scoped = scopes.get(scope, {})
    # 旧状态缺会话维度，只在首次迁移当前目标时沿用。
    legacy = state.get("keys", []) if not scopes else []
    synced = [k for k in scoped.get("keys", legacy) if isinstance(k, str)]
    synced_set = set(synced)
    try:
        records = await _source_records(engine, umo, persona_id)
        if not scopes and not records:
            # 旧状态没保存目标；空的新会话不能继承别的会话的“已同步”标记。
            synced = []
            synced_set = set()
        evicted = _eviction_state(bot)
        # 即使观看日志超过窗口或已截断，旧图片引用仍应标记淘汰。
        cursor = await engine.db_connection.execute(
            "SELECT id, text, metadata FROM documents "
            "WHERE json_extract(metadata, '$.memory_origin') = 'bilibot' "
            "AND json_type(metadata, '$.bilibot_images') = 'array'"
        )
        media_records = [dict(r, metadata=_metadata(r["metadata"])) for r in await cursor.fetchall()]
        evictions_updated = await _reconcile_evictions(engine, media_records, evicted, dry_run=dry_run)
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:200], "written": 0, "failed": 1}
    from . import image_gallery
    pending = _collect(bot, since, synced_set)
    # 淘汰时保存的旧评论不受 3 天窗口限制；已交付的本地历史不自动重新导入。
    retained = image_gallery.pending_events(scope)
    by_key = {item["key"]: item for item in pending}
    for rec in retained:
        item = _comment_item(rec)
        if item:
            if item["key"] in by_key:
                fresh = by_key[item["key"]]
                images = merge_images(item["metadata"]["bilibot_images"],
                                      fresh["metadata"].get("bilibot_images", []))
                item = _comment_item(item["record"], images)
            by_key[item["key"]] = item
    pending = list(by_key.values())
    written, updated, unchanged, failed, errors = 0, 0, 0, 0, []
    for item in pending:
        try:
            existing = _find_existing(item, records)
            if existing is None and item["key"] in synced_set:
                # 已同步事件被用户删除/改写后，不自动复活或另建重复文档。
                unchanged += 1
                if not dry_run:
                    image_gallery.acknowledge_event(scope, item["key"])
                continue
            content, metadata, importance = _prepare_item(item, existing, processor, umo, evicted)
            if existing is not None:
                current = existing["metadata"]
                patch = {k: v for k, v in metadata.items() if current.get(k) != v}
                if not patch and existing["text"] == content:
                    unchanged += 1
                    if not dry_run:
                        image_gallery.acknowledge_event(scope, item["key"])
                    continue
                if dry_run:
                    doc_id = existing["id"]
                elif existing["text"] != content:
                    # replace_memory 返回新 ID，并保留时间轴、原文及非本插件字段。
                    doc_id = await engine.replace_memory(
                        existing["id"], content=content,
                        metadata=metadata, importance=importance,
                    )
                else:
                    if not await engine.update_memory(existing["id"], {"metadata": patch}):
                        raise RuntimeError("LivingMemory 元数据更新失败")
                    doc_id = existing["id"]
                existing.update(id=doc_id, text=content, metadata={**current, **metadata})
                updated += 1
            else:
                if dry_run:
                    doc_id = -len(records) - 1
                else:
                    doc_id = await engine.add_memory(
                        content=content, session_id=umo, persona_id=persona_id,
                        importance=importance, metadata=metadata,
                    )
                    if not isinstance(doc_id, int) or doc_id <= 0:
                        raise RuntimeError("LivingMemory 未返回有效记忆 ID")
                records.append({
                    "id": doc_id, "text": content,
                    "metadata": {**metadata, "session_id": umo, "persona_id": persona_id},
                })
                written += 1
            synced_set.add(item["key"])
            if not dry_run:
                image_gallery.acknowledge_event(scope, item["key"])
            if item["key"] not in synced:
                synced.append(item["key"])
        except Exception as exc:
            failed += 1
            if len(errors) < 3:
                errors.append(str(exc)[:200])
            logger.warning(f"[BiliBot] 记忆写入失败（{item['key']}）: {exc}")

    scopes[scope] = {"keys": synced[-MAX_SYNC_KEYS:]}
    state["scopes"] = scopes
    state["keys"] = synced[-MAX_SYNC_KEYS:]
    state["schema"] = SYNC_SCHEMA
    state["last_run"] = time.time()
    state["last_reason"] = reason
    state["last_written"] = written
    state["last_updated"] = updated
    state["last_unchanged"] = unchanged
    state["last_evictions_updated"] = evictions_updated
    state["last_failed"] = failed
    state["total_written"] = int(state.get("total_written") or 0) + written
    state_saved = False if dry_run else _save_state(bot, state)

    if not dry_run and (written or updated or failed):
        logger.info(
            f"[BiliBot] 🧠 记忆联动（{reason}）：新增 {written}，更新 {updated}，失败 {failed}，目标 {umo}"
        )
    return {
        "ok": True,
        "dry_run": dry_run,
        "written": written,
        "updated": updated,
        "unchanged": unchanged,
        "evictions_updated": evictions_updated,
        "state_saved": state_saved,
        "failed": failed,
        "pending": written + updated + failed,
        "target_umo": umo,
        "persona_id": persona_id,
        "window_days": window,
        "errors": errors,
    }


async def maybe_sync(bot) -> None:
    """主循环里的节流同步，失败不冒泡。"""
    if not _bool(bot, "LIVINGMEMORY_SYNC_ENABLED", True):
        return
    interval = max(2, _int(bot, "LIVINGMEMORY_SYNC_INTERVAL_MINUTES", 10)) * 60
    state = _load_state(bot)
    try:
        last_run = float(state.get("last_run") or 0)
    except (TypeError, ValueError):
        last_run = 0.0
    if time.time() - last_run < interval:
        return
    # 锁内落节流时间戳，避免和手动同步互相覆盖状态。
    async with _SYNC_LOCK:
        state = _load_state(bot)
        if time.time() - float(state.get("last_run") or 0) < interval:
            return
        state["last_run"] = time.time()
        _save_state(bot, state)
    try:
        await sync(bot, reason="auto")
    except Exception as exc:
        logger.warning(f"[BiliBot] 记忆联动自动同步失败: {exc}")


async def search(bot, query: str, *, k: int = 5) -> list[str]:
    """兼容旧调用点；公开背景统一走授权入口，禁止退回 owner 会话或全库。"""
    from .unified_knowledge import public_knowledge_context

    content = await public_knowledge_context(bot, query)
    return [content] if content and k > 0 else []


async def status(bot) -> dict:
    """面板用状态摘要。"""
    umo = target_umo(bot)
    lm = _livingmemory(bot)
    engine, processor = (_engine(lm) if lm is not None else (None, None))
    state = _load_state(bot)
    synced = set(k for k in (state.get("keys") or []) if isinstance(k, str))
    since = datetime.now() - timedelta(days=_int(bot, "LIVINGMEMORY_SYNC_WINDOW_DAYS", DEFAULT_WINDOW_DAYS))
    pending = 0
    if engine is not None:
        try:
            persona_id = await _resolve_persona_id(bot.context, umo)
            records = await _source_records(engine, umo, persona_id)
            evicted = _eviction_state(bot)
            for item in _collect(bot, since, synced):
                existing = _find_existing(item, records)
                if existing is None:
                    pending += int(item["key"] not in synced)
                else:
                    content, meta, _ = _prepare_item(item, existing, processor, umo, evicted)
                    pending += int(content != existing["text"] or any(
                        existing["metadata"].get(k) != v for k, v in meta.items()
                    ))
        except Exception:
            pending = int(state.get("last_failed") or 0)
    return {
        "enabled": _bool(bot, "LIVINGMEMORY_SYNC_ENABLED", True),
        "plugin_found": lm is not None,
        "engine_ready": engine is not None,
        "target_umo": umo,
        "owner_umo_raw": str(_raw_config(bot, "OWNER_QQ_UMO", "") or ""),
        "persona_ready": processor is not None,
        "pending": pending,
        "synced_total": int(state.get("total_written") or 0),
        "last_run": state.get("last_run"),
        "last_written": state.get("last_written"),
        "last_updated": state.get("last_updated"),
        "last_evictions_updated": state.get("last_evictions_updated"),
        "last_failed": state.get("last_failed"),
        "window_days": _int(bot, "LIVINGMEMORY_SYNC_WINDOW_DAYS", DEFAULT_WINDOW_DAYS),
        "interval_minutes": _int(bot, "LIVINGMEMORY_SYNC_INTERVAL_MINUTES", 10),
    }


async def probe(bot) -> dict:
    """连通性自检：BiliBot 自身向量接口 + LivingMemory 引擎状态。"""
    result = {"embedding": {"ok": False}, "livingmemory": {}, "umo": {}}
    model = str(_raw_config(bot, "EMBED_MODEL", "") or "")
    try:
        vector = await bot._get_embedding("BiliBot 向量连通性自检")
        if vector:
            result["embedding"] = {
                "ok": True,
                "model": model,
                "dimensions": len(vector),
            }
        else:
            result["embedding"] = {"ok": False, "model": model, "error": "返回空向量，检查 EMBED_API_KEY / EMBED_API_BASE"}
    except Exception as exc:
        result["embedding"] = {"ok": False, "model": model, "error": str(exc)[:200]}

    lm = _livingmemory(bot)
    if lm is None:
        result["livingmemory"] = {"ok": False, "error": "未找到 LivingMemory 插件实例"}
    else:
        engine, processor = _engine(lm)
        provider = ""
        try:
            provider = str(lm.config_manager.get("provider_settings.embedding_provider_id", ""))
        except Exception:
            try:
                provider = str((lm.config.get("provider_settings") or {}).get("embedding_provider_id", ""))
            except Exception:
                provider = ""
        result["livingmemory"] = {
            "ok": engine is not None and processor is not None,
            "engine_ready": engine is not None,
            "processor_ready": processor is not None,
            "embedding_provider_id": provider,
            "documents": _document_count(lm),
        }

    umo = target_umo(bot)
    result["umo"] = {"configured": umo, "full": bool(umo.count(":") == 2)}
    return result


def _document_count(lm) -> int | None:
    """LivingMemory 已入库记忆条数（只读）。"""
    try:
        import sqlite3

        from astrbot.api.star import StarTools

        data_dir = str(StarTools.get_data_dir(LIVINGMEMORY_PLUGIN))
        path = os.path.join(data_dir, "livingmemory.db")
        if not os.path.exists(path):
            return None
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return int(conn.execute("select count(*) from documents").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return None
