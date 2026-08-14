"""存储层 API：记忆、画像、媒体摘要的读写接口。

对应 issue #8。旧实现把记忆、embedding、画像全塞进三个 JSON 文件整文件读写，
本模块提供行级增删改查、TTL 自动过期、按 scope 过滤召回。
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass, field
from typing import Any

from ..security import Scope, can_read, readable_scopes
from .db import Database, now


@dataclass
class Memory:
    """单条记忆。"""

    id: int
    scope: str
    memory_type: str
    level: str
    actor_id: str
    thread_id: str
    target_id: str
    text: str
    importance: int
    value_score: float
    privacy: int
    confidence: float
    source_event: int | None
    meta: dict[str, Any]
    created_at: float
    expires_at: float | None
    promoted_at: float | None
    bytes: int


@dataclass
class Profile:
    """用户群像主表。"""

    actor_id: str
    display_name: str
    familiarity: float
    trust: float
    warmth: float
    conflict: float
    stage: str
    impression: str
    topics: list[str]
    avoid: list[str]
    interact_count: int
    first_seen: float
    last_seen: float
    updated_at: float
    revision: int


@dataclass
class ProfileFact:
    """群像事实条目。"""

    id: int
    actor_id: str
    fact: str
    scope: str
    evidence: str
    confidence: float
    approved: int
    created_at: float
    expires_at: float | None


class MemoryStore:
    """记忆读写。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        scope: Scope | str,
        text: str,
        memory_type: str = "chat",
        level: str = "recent",
        actor_id: str = "",
        thread_id: str = "",
        target_id: str = "",
        importance: int = 5,
        value_score: float = 0.5,
        privacy: int = 0,
        confidence: float = 0.5,
        source_event: int | None = None,
        meta: dict[str, Any] | None = None,
        ttl: float | None = None,
    ) -> int:
        """写入一条记忆。返回 memory ID。"""
        content = str(text or "").strip()
        if not content:
            return 0
        expires_at = now() + ttl if ttl else None
        mem_bytes = len(content.encode("utf-8"))
        return await self._db.execute(
            "INSERT INTO memories("
            "scope,memory_type,level,actor_id,thread_id,target_id,text,importance,"
            "value_score,privacy,confidence,source_event,meta,created_at,expires_at,bytes"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(scope), memory_type, level, actor_id, thread_id, target_id,
                content, importance, value_score, privacy, confidence, source_event,
                json.dumps(meta or {}, ensure_ascii=False), now(), expires_at, mem_bytes,
            ),
        )

    async def promote(self, memory_id: int, to_level: str = "long_term") -> None:
        """晋升记忆层级。recent → long_term → aged。"""
        await self._db.execute(
            "UPDATE memories SET level=?, promoted_at=?, updated_at=? WHERE id=?",
            (to_level, now(), now(), memory_id),
        )

    async def recall(
        self,
        scope: Scope | str,
        limit: int = 20,
        actor_id: str = "",
        thread_id: str = "",
        level: str = "",
    ) -> list[Memory]:
        """召回记忆。按 scope 策略过滤，按时间倒序。"""
        allowed = readable_scopes(scope)
        if not allowed:
            return []
        placeholders = ",".join("?" * len(allowed))
        sql = (
            f"SELECT * FROM memories WHERE scope IN ({placeholders}) "
            f"AND (expires_at IS NULL OR expires_at > ?)"
        )
        params: list[Any] = [*[s.value for s in allowed], now()]
        if actor_id:
            sql += " AND actor_id=?"
            params.append(actor_id)
        if thread_id:
            sql += " AND thread_id=?"
            params.append(thread_id)
        if level:
            sql += " AND level=?"
            params.append(level)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = await self._db.fetch_all(sql, params)
        return [
            Memory(
                id=r["id"], scope=r["scope"], memory_type=r["memory_type"],
                level=r["level"], actor_id=r["actor_id"], thread_id=r["thread_id"],
                target_id=r["target_id"], text=r["text"], importance=r["importance"],
                value_score=r["value_score"], privacy=r["privacy"],
                confidence=r["confidence"], source_event=r["source_event"],
                meta=json.loads(r["meta"] or "{}"), created_at=r["created_at"],
                expires_at=r["expires_at"], promoted_at=r["promoted_at"],
                bytes=r["bytes"],
            )
            for r in rows
        ]

    async def delete(self, memory_id: int) -> None:
        await self._db.execute("DELETE FROM memories WHERE id=?", (memory_id,))

    async def purge_expired(self) -> int:
        """清理过期记忆。"""
        return await self._db.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now(),),
        )

    async def scope_size_bytes(self, scope: Scope | str) -> int:
        return int(
            await self._db.fetch_value(
                "SELECT COALESCE(SUM(bytes), 0) FROM memories WHERE scope=?",
                (str(scope),),
                default=0,
            ) or 0
        )

    async def total_count(self) -> int:
        return int(
            await self._db.fetch_value(
                "SELECT COUNT(*) FROM memories", default=0
            ) or 0
        )


class ProfileStore:
    """群像读写。增量更新，不重写全量。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, actor_id: str) -> Profile | None:
        row = await self._db.fetch_one(
            "SELECT * FROM profiles WHERE actor_id=?", (actor_id,)
        )
        if row is None:
            return None
        return Profile(
            actor_id=row["actor_id"], display_name=row["display_name"],
            familiarity=row["familiarity"], trust=row["trust"],
            warmth=row["warmth"], conflict=row["conflict"], stage=row["stage"],
            impression=row["impression"],
            topics=json.loads(row["topics"] or "[]"),
            avoid=json.loads(row["avoid"] or "[]"),
            interact_count=row["interact_count"], first_seen=row["first_seen"],
            last_seen=row["last_seen"], updated_at=row["updated_at"],
            revision=row["revision"],
        )

    async def upsert(
        self,
        actor_id: str,
        display_name: str = "",
        delta: dict[str, Any] | None = None,
    ) -> None:
        """插入或增量更新。delta 只包含变化字段，不重写全部。"""
        existing = await self.get(actor_id)
        if existing is None:
            await self._db.execute(
                "INSERT INTO profiles(actor_id,display_name,first_seen,last_seen,"
                "updated_at,revision) VALUES(?,?,?,?,?,?)",
                (actor_id, display_name, now(), now(), now(), 1),
            )
            return
        updates: dict[str, Any] = delta or {}
        updates["last_seen"] = now()
        updates["updated_at"] = now()
        updates["revision"] = existing.revision + 1
        if display_name and display_name != existing.display_name:
            updates["display_name"] = display_name
        set_clause = ", ".join(f"{k}=?" for k in updates)
        await self._db.execute(
            f"UPDATE profiles SET {set_clause} WHERE actor_id=?",
            (*updates.values(), actor_id),
        )

    async def add_fact(
        self,
        actor_id: str,
        fact: str,
        scope: Scope | str,
        evidence: str = "",
        confidence: float = 0.5,
        approved: int = 0,
        ttl: float | None = None,
    ) -> int:
        """写入群像事实。已存在则忽略（UNIQUE 约束）。"""
        expires_at = now() + ttl if ttl else None
        try:
            return await self._db.execute(
                "INSERT INTO profile_facts(actor_id,fact,scope,evidence,confidence,"
                "approved,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    actor_id, fact[:200], str(scope), evidence[:200],
                    confidence, approved, now(), expires_at,
                ),
            )
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                return 0
            raise

    async def facts(self, actor_id: str, approved_only: bool = False) -> list[ProfileFact]:
        sql = "SELECT * FROM profile_facts WHERE actor_id=? AND (expires_at IS NULL OR expires_at > ?)"
        params: list[Any] = [actor_id, now()]
        if approved_only:
            sql += " AND approved=1"
        sql += " ORDER BY created_at DESC"
        rows = await self._db.fetch_all(sql, params)
        return [
            ProfileFact(
                id=r["id"], actor_id=r["actor_id"], fact=r["fact"], scope=r["scope"],
                evidence=r["evidence"], confidence=r["confidence"],
                approved=r["approved"], created_at=r["created_at"],
                expires_at=r["expires_at"],
            )
            for r in rows
        ]

    async def delete_fact(self, fact_id: int) -> None:
        await self._db.execute("DELETE FROM profile_facts WHERE id=?", (fact_id,))

    async def purge_expired_facts(self) -> int:
        return await self._db.execute(
            "DELETE FROM profile_facts "
            "WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now(),),
        )


@dataclass
class MediaDigest:
    """媒体理解缓存。"""

    id: int
    kind: str
    ref: str
    title: str
    digest: str
    facts: dict[str, Any]
    tags: list[str]
    tokens_used: int
    cost_cents: float
    created_at: float
    expires_at: float | None
    hits: int
    last_hit_at: float | None


class MediaStore:
    """视频/图片/动态理解结果缓存。只存摘要与结构化事实，原始媒体不入库。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def put(
        self,
        kind: str,
        ref: str,
        title: str = "",
        digest: str = "",
        facts: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        tokens_used: int = 0,
        cost_cents: float = 0.0,
        ttl: float | None = None,
    ) -> int:
        """写入摘要。已存在则更新（ON CONFLICT）。"""
        expires_at = now() + ttl if ttl else None
        return await self._db.execute(
            "INSERT INTO media_digests(kind,ref,title,digest,facts,tags,tokens_used,"
            "cost_cents,created_at,expires_at,hits) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(kind,ref) DO UPDATE SET title=excluded.title,"
            "digest=excluded.digest,facts=excluded.facts,tags=excluded.tags,"
            "tokens_used=excluded.tokens_used,cost_cents=excluded.cost_cents,"
            "expires_at=excluded.expires_at",
            (
                kind, ref, title, digest, json.dumps(facts or {}, ensure_ascii=False),
                json.dumps(tags or [], ensure_ascii=False), tokens_used, cost_cents,
                now(), expires_at, 0,
            ),
        )

    async def get(self, kind: str, ref: str) -> MediaDigest | None:
        """取缓存。命中时更新 hits 与 last_hit_at。"""
        row = await self._db.fetch_one(
            "SELECT * FROM media_digests WHERE kind=? AND ref=? "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (kind, ref, now()),
        )
        if row is None:
            return None
        await self._db.execute(
            "UPDATE media_digests SET hits=hits+1, last_hit_at=? WHERE id=?",
            (now(), row["id"]),
        )
        return MediaDigest(
            id=row["id"], kind=row["kind"], ref=row["ref"], title=row["title"],
            digest=row["digest"], facts=json.loads(row["facts"] or "{}"),
            tags=json.loads(row["tags"] or "[]"), tokens_used=row["tokens_used"],
            cost_cents=row["cost_cents"], created_at=row["created_at"],
            expires_at=row["expires_at"], hits=row["hits"] + 1,
            last_hit_at=now(),
        )

    async def purge_expired(self) -> int:
        return await self._db.execute(
            "DELETE FROM media_digests "
            "WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now(),),
        )

    async def evict_lru(self, keep: int = 100) -> int:
        """LRU 淘汰。保留最近命中的 keep 条，删除其余。"""
        cutoff = await self._db.fetch_value(
            "SELECT last_hit_at FROM media_digests "
            "ORDER BY last_hit_at DESC LIMIT 1 OFFSET ?",
            (keep - 1,),
        )
        if cutoff is None:
            return 0
        return await self._db.execute(
            "DELETE FROM media_digests WHERE last_hit_at < ?", (cutoff,)
        )

    async def total_cost_cents(self) -> float:
        return float(
            await self._db.fetch_value(
                "SELECT COALESCE(SUM(cost_cents), 0) FROM media_digests", default=0.0
            ) or 0.0
        )
