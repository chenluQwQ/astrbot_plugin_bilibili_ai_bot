"""存储层 API：记忆、画像、媒体摘要的读写接口。

对应 issue #8。旧实现把记忆、embedding、画像全塞进三个 JSON 文件整文件读写，
本模块提供行级增删改查、TTL 自动过期、按 scope 过滤召回。
"""

from __future__ import annotations

from array import array
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from ..security import Scope, readable_scopes
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


@dataclass
class SeenVideo:
    """永久轻量“看过”记录。"""

    bvid: str
    first_seen_at: float
    last_related_at: float
    watch_count: int
    title: str
    owner_mid: str
    owner_name: str
    tname: str
    last_source: str


class SeenVideoStore:
    """永久 BV 去重账本；不按数量或时间裁剪。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def normalize_bvid(value: Any) -> str:
        raw = str(value or "").strip()
        if len(raw) < 4 or raw[:2].lower() != "bv":
            return ""
        return "BV" + raw[2:]

    @staticmethod
    def _number(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return float(default)

    @staticmethod
    def _mark_sync(conn, record: dict[str, Any]) -> bool:
        bvid = SeenVideoStore.normalize_bvid(record.get("bvid"))
        if not bvid:
            return False
        seen_at = SeenVideoStore._number(record.get("seen_at"), now())
        first_seen_at = SeenVideoStore._number(
            record.get("first_seen_at"), seen_at
        )
        last_related_at = SeenVideoStore._number(
            record.get("last_related_at"), seen_at
        )
        first_seen_at, last_related_at = (
            min(first_seen_at, last_related_at),
            max(first_seen_at, last_related_at),
        )
        increment = 1 if bool(record.get("increment", True)) else 0
        row = conn.execute(
            "SELECT * FROM seen_videos WHERE bvid=?", (bvid,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO seen_videos(bvid,first_seen_at,last_related_at,"
                "watch_count,title,owner_mid,owner_name,tname,last_source) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    bvid,
                    first_seen_at,
                    last_related_at,
                    max(1, increment),
                    str(record.get("title") or ""),
                    str(record.get("owner_mid") or ""),
                    str(record.get("owner_name") or ""),
                    str(record.get("tname") or ""),
                    str(record.get("source") or ""),
                ),
            )
            return True
        conn.execute(
            "UPDATE seen_videos SET first_seen_at=?,last_related_at=?,watch_count=?,"
            "title=?,owner_mid=?,owner_name=?,tname=?,last_source=? WHERE bvid=?",
            (
                min(float(row["first_seen_at"]), first_seen_at),
                max(float(row["last_related_at"]), last_related_at),
                int(row["watch_count"]) + increment,
                str(record.get("title") or row["title"] or ""),
                str(record.get("owner_mid") or row["owner_mid"] or ""),
                str(record.get("owner_name") or row["owner_name"] or ""),
                str(record.get("tname") or row["tname"] or ""),
                str(record.get("source") or row["last_source"] or ""),
                bvid,
            ),
        )
        return False

    async def mark_seen(
        self,
        bvid: str,
        *,
        seen_at: float | None = None,
        title: str = "",
        owner_mid: str = "",
        owner_name: str = "",
        tname: str = "",
        source: str = "",
        increment: bool = True,
    ) -> bool:
        record = {
            "bvid": bvid,
            "seen_at": seen_at if seen_at is not None else now(),
            "title": title,
            "owner_mid": owner_mid,
            "owner_name": owner_name,
            "tname": tname,
            "source": source,
            "increment": increment,
        }
        return bool(await self._db.run(self._mark_sync, record))

    async def import_many(self, records: list[dict[str, Any]]) -> int:
        """幂等迁移旧来源，不把多份旧副本误算成重复观看。"""
        prepared = [dict(record, increment=False) for record in records]

        def _import(conn):
            created = 0
            for record in prepared:
                created += int(self._mark_sync(conn, record))
            return created

        return int(await self._db.run(_import))

    async def contains(self, bvid: str) -> bool:
        key = self.normalize_bvid(bvid)
        if not key:
            return False
        return bool(
            await self._db.fetch_value(
                "SELECT 1 FROM seen_videos WHERE bvid=?", (key,), default=0
            )
        )

    async def get(self, bvid: str) -> dict[str, Any] | None:
        key = self.normalize_bvid(bvid)
        if not key:
            return None
        row = await self._db.fetch_one(
            "SELECT * FROM seen_videos WHERE bvid=?", (key,)
        )
        return dict(row) if row is not None else None

    async def all_bvids(self) -> set[str]:
        rows = await self._db.fetch_all("SELECT bvid FROM seen_videos")
        return {str(row["bvid"]) for row in rows}

    async def count(self) -> int:
        return int(
            await self._db.fetch_value(
                "SELECT COUNT(*) FROM seen_videos", default=0
            ) or 0
        )


class FeedbackStore:
    """候选反馈仓库；不在这里直接改变人格或偏好。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_candidate(
        self,
        *,
        event_key: str,
        actor_id: str,
        actor_name: str,
        scope: str,
        feedback_type: str,
        topic: str = "",
        event_summary: str = "",
        possible_mistake: str = "",
        next_time: str = "",
        confidence: float = 0.0,
        relation_weight: float = 1.0,
        is_owner: bool = False,
        created_at: float | None = None,
    ) -> bool:
        timestamp = float(created_at if created_at is not None else now())
        params = (
                str(event_key)[:180], str(actor_id)[:80], str(actor_name)[:80],
                str(scope)[:40], str(feedback_type)[:30], str(topic)[:80],
                str(event_summary)[:160], str(possible_mistake)[:160],
                str(next_time)[:160], max(0.0, min(1.0, float(confidence))),
                max(0.1, min(5.0, float(relation_weight))),
                1 if is_owner else 0, "candidate", timestamp,
            )

        def _insert(conn):
            cursor = conn.execute(
                "INSERT OR IGNORE INTO feedback_candidates("
                "event_key,actor_id,actor_name,scope,feedback_type,topic,event_summary,"
                "possible_mistake,next_time,confidence,relation_weight,is_owner,status,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                params,
            )
            return cursor.rowcount == 1

        return bool(await self._db.run(_insert))

    async def recent(self, *, days: int = 7) -> list[dict[str, Any]]:
        since = now() - max(1, int(days)) * 86400
        rows = await self._db.fetch_all(
            "SELECT * FROM feedback_candidates WHERE created_at>=? "
            "ORDER BY created_at DESC", (since,)
        )
        return [dict(row) for row in rows]

    async def aggregate(self, *, days: int = 7) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in await self.recent(days=days):
            key = (str(row["feedback_type"]), str(row["topic"] or "未分类反馈"))
            item = grouped.setdefault(
                key,
                {
                    "feedback_type": key[0], "topic": key[1], "count": 0,
                    "weighted_score": 0.0, "actors": set(), "owner_count": 0,
                    "examples": [],
                },
            )
            item["count"] += 1
            item["weighted_score"] += float(row["relation_weight"])
            item["actors"].add(str(row["actor_id"]))
            item["owner_count"] += int(row["is_owner"])
            example = str(row["next_time"] or row["possible_mistake"] or "")
            if example and example not in item["examples"] and len(item["examples"]) < 3:
                item["examples"].append(example)
        result = []
        for item in grouped.values():
            item["distinct_actors"] = len(item.pop("actors"))
            item["weighted_score"] = round(item["weighted_score"], 2)
            result.append(item)
        return sorted(
            result,
            key=lambda item: (item["weighted_score"], item["distinct_actors"]),
            reverse=True,
        )

    @staticmethod
    def _relevance_terms(value: Any) -> set[str]:
        """Extract conservative topic terms without exposing raw feedback."""
        text = re.sub(r"\s+", " ", str(value or "").casefold()).strip()
        terms = {
            token for token in re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", text)
        }
        stop_terms = {
            "这个", "那个", "一下", "有点", "感觉", "问题", "时候", "可以",
            "还是", "回复", "用户", "内容", "应该", "不要", "需要",
        }
        for chunk in re.findall(r"[\u3400-\u9fff]+", text):
            if len(chunk) >= 2:
                terms.update(
                    chunk[index:index + 2]
                    for index in range(len(chunk) - 1)
                    if chunk[index:index + 2] not in stop_terms
                )
        return terms

    async def relevant(
        self, query: str, *, days: int = 30, limit: int = 3
    ) -> list[dict[str, Any]]:
        """Return supported, scene-relevant reflection candidates only.

        A single ordinary user's suggestion remains only a candidate: it is not
        injected until the owner raised it, multiple actors agree, or accumulated
        relationship weight is high enough. This is guidance, not personality
        mutation.
        """
        query_terms = self._relevance_terms(query)
        if not query_terms:
            return []
        ranked = []
        for item in await self.aggregate(days=days):
            if str(item.get("feedback_type") or "") not in {
                "suggestion", "correction", "criticism",
            }:
                continue
            if not (
                int(item.get("owner_count", 0) or 0) >= 1
                or int(item.get("distinct_actors", 0) or 0) >= 2
                or float(item.get("weighted_score", 0) or 0) >= 2.5
            ):
                continue
            candidate_text = " ".join([
                str(item.get("topic") or ""),
                *[str(value or "") for value in item.get("examples", [])],
            ])
            overlap = query_terms & self._relevance_terms(candidate_text)
            if not overlap:
                continue
            ranked_item = dict(item)
            ranked_item["relevance_score"] = round(
                len(overlap) * 2.0
                + min(5.0, float(item.get("weighted_score", 0) or 0))
                + min(2.0, int(item.get("distinct_actors", 0) or 0) * 0.5),
                2,
            )
            ranked.append(ranked_item)
        ranked.sort(
            key=lambda item: (
                item["relevance_score"], item["owner_count"],
                item["distinct_actors"], item["weighted_score"],
            ),
            reverse=True,
        )
        return ranked[:max(0, min(5, int(limit)))]


class PreferenceStore:
    """把视频评价信号沉淀为候选、近期和稳定偏好。"""

    SIGNAL_TYPES = {
        "up", "partition", "work", "character", "food", "theme", "music",
        "game", "technology", "activity", "location", "other",
    }
    POLARITIES = {"like", "dislike", "fatigue", "curious"}
    _POLARITY_WEIGHT = {
        "like": 1.0,
        "curious": 0.6,
        "dislike": 1.0,
        "fatigue": 0.85,
    }
    _STAGE_RANK = {"candidate": 1, "recent": 2, "stable": 3}

    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _clean(value: Any, limit: int = 80) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

    @classmethod
    def _key(cls, signal_type: str, value: str) -> str:
        return f"{signal_type}:{value.casefold()}"

    async def record_video_signals(
        self,
        *,
        source_ref: str,
        signals: list[dict[str, Any]],
        occurred_at: float | None = None,
    ) -> int:
        """幂等写入一次观看产生的具体偏好证据。"""
        timestamp = float(occurred_at if occurred_at is not None else now())
        source = self._clean(source_ref, 160)
        prepared = []
        for signal in signals[:5] if isinstance(signals, list) else []:
            if not isinstance(signal, dict):
                continue
            signal_type = self._clean(signal.get("type") or "other", 24)
            value = self._clean(signal.get("value"), 80)
            polarity = self._clean(signal.get("polarity"), 16)
            if signal_type not in self.SIGNAL_TYPES or not value or polarity not in self.POLARITIES:
                continue
            try:
                strength = max(0.0, min(1.0, float(signal.get("strength", 0))))
            except (TypeError, ValueError, OverflowError):
                continue
            if strength <= 0:
                continue
            preference_key = self._key(signal_type, value)
            digest_input = f"{source}|{timestamp:.3f}|{preference_key}|{polarity}"
            evidence_key = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
            prepared.append((
                evidence_key, preference_key, signal_type, value, polarity,
                strength, source, timestamp,
            ))
        if not prepared:
            return 0

        def _insert(conn):
            created = 0
            for params in prepared:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO preference_evidence("
                    "evidence_key,preference_key,signal_type,value,polarity,strength,"
                    "source_ref,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                    params,
                )
                created += int(cursor.rowcount == 1)
            return created

        return int(await self._db.run(_insert))

    @staticmethod
    def _week_starts(timestamps: list[float]) -> list:
        starts = set()
        for timestamp in timestamps:
            day = datetime.fromtimestamp(timestamp, timezone.utc).date()
            starts.add(day - timedelta(days=day.weekday()))
        return sorted(starts)

    @staticmethod
    def _max_consecutive_weeks(starts: list) -> int:
        best = run = 0
        previous = None
        for current in starts:
            run = run + 1 if previous is not None and current - previous == timedelta(days=7) else 1
            best = max(best, run)
            previous = current
        return best

    async def refresh(self, *, at: float | None = None) -> dict[str, list[dict[str, Any]]]:
        """重算生命周期；返回当前偏好和本轮变化，供周报使用。"""
        timestamp = float(at if at is not None else now())
        cutoff = timestamp - 180 * 86400

        def _refresh(conn):
            rows = conn.execute(
                "SELECT * FROM preference_evidence WHERE occurred_at>=? "
                "ORDER BY occurred_at", (cutoff,)
            ).fetchall()
            previous_rows = {
                str(row["preference_key"]): dict(row)
                for row in conn.execute("SELECT * FROM preferences").fetchall()
            }
            grouped: dict[str, list[Any]] = {}
            for row in rows:
                grouped.setdefault(str(row["preference_key"]), []).append(row)

            current: list[dict[str, Any]] = []
            changes: list[dict[str, Any]] = []
            for preference_key in sorted(set(grouped) | set(previous_rows)):
                evidence = grouped.get(preference_key, [])
                old = previous_rows.get(preference_key)
                if not evidence:
                    if old:
                        deleted = dict(old)
                        deleted["lifecycle_action"] = "deleted"
                        changes.append(deleted)
                        conn.execute(
                            "DELETE FROM preferences WHERE preference_key=?",
                            (preference_key,),
                        )
                    continue

                times = [float(row["occurred_at"]) for row in evidence]
                first_seen, last_seen = min(times), max(times)
                week_starts = self._week_starts(times)
                consecutive = self._max_consecutive_weeks(week_starts)
                recent_count = sum(value >= timestamp - 7 * 86400 for value in times)
                age_days = max(0.0, (timestamp - last_seen) / 86400)
                old_stage = str(old["stage"]) if old else ""

                if age_days > 90:
                    if old:
                        deleted = dict(old)
                        deleted["lifecycle_action"] = "deleted"
                        changes.append(deleted)
                    conn.execute(
                        "DELETE FROM preferences WHERE preference_key=?",
                        (preference_key,),
                    )
                    continue
                if consecutive >= 3:
                    stage = "stable"
                elif recent_count >= 2:
                    stage = "recent"
                elif age_days <= 7:
                    stage = "candidate"
                elif old_stage == "stable" and age_days <= 90:
                    stage = "stable"
                elif old_stage == "recent" and age_days <= 21:
                    stage = "recent"
                else:
                    if old:
                        deleted = dict(old)
                        deleted["lifecycle_action"] = "deleted"
                        changes.append(deleted)
                    conn.execute(
                        "DELETE FROM preferences WHERE preference_key=?",
                        (preference_key,),
                    )
                    continue

                totals = {name: 0.0 for name in self.POLARITIES}
                for row in evidence:
                    totals[str(row["polarity"])] += float(row["strength"])
                polarity = max(
                    totals,
                    key=lambda name: totals[name] * self._POLARITY_WEIGHT[name],
                )
                dominant = totals[polarity] * self._POLARITY_WEIGHT[polarity]
                if stage == "stable":
                    decay = max(0.1, 1.0 - age_days / 90.0)
                    expires_at = last_seen + 90 * 86400
                elif stage == "recent":
                    decay = 1.0 if age_days <= 7 else max(0.1, 1.0 - (age_days - 7) / 14.0)
                    expires_at = last_seen + 21 * 86400
                else:
                    decay = max(0.1, 1.0 - age_days / 7.0)
                    expires_at = last_seen + 7 * 86400
                score = round(min(1.0, dominant / max(1, len(evidence))) * decay, 3)

                old_rank = self._STAGE_RANK.get(old_stage, 0)
                new_rank = self._STAGE_RANK[stage]
                old_count = int(old["evidence_count"]) if old else 0
                if not old or new_rank > old_rank or len(evidence) > old_count:
                    action = "enhanced"
                elif age_days <= 7:
                    action = "retained"
                else:
                    action = "weakened"

                item = {
                    "preference_key": preference_key,
                    "signal_type": str(evidence[-1]["signal_type"]),
                    "value": str(evidence[-1]["value"]),
                    "polarity": polarity,
                    "stage": stage,
                    "score": score,
                    "evidence_count": len(evidence),
                    "active_weeks": len(week_starts),
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "lifecycle_action": action,
                    "updated_at": timestamp,
                    "expires_at": expires_at,
                }
                conn.execute(
                    "INSERT INTO preferences(preference_key,signal_type,value,polarity,"
                    "stage,score,evidence_count,active_weeks,first_seen,last_seen,"
                    "lifecycle_action,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(preference_key) DO UPDATE SET signal_type=excluded.signal_type,"
                    "value=excluded.value,polarity=excluded.polarity,stage=excluded.stage,"
                    "score=excluded.score,evidence_count=excluded.evidence_count,"
                    "active_weeks=excluded.active_weeks,first_seen=excluded.first_seen,"
                    "last_seen=excluded.last_seen,lifecycle_action=excluded.lifecycle_action,"
                    "updated_at=excluded.updated_at,expires_at=excluded.expires_at",
                    tuple(item.values()),
                )
                current.append(item)
                if action != "retained":
                    changes.append(dict(item))
            current.sort(
                key=lambda item: (self._STAGE_RANK[item["stage"]], item["score"], item["last_seen"]),
                reverse=True,
            )
            return {"current": current, "changes": changes}

        return await self._db.run(_refresh)

    async def current(self, *, limit: int = 30) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT * FROM preferences ORDER BY "
            "CASE stage WHEN 'stable' THEN 3 WHEN 'recent' THEN 2 ELSE 1 END DESC,"
            "score DESC,last_seen DESC LIMIT ?", (max(1, int(limit)),)
        )
        return [dict(row) for row in rows]


class MemoryStore:
    """记忆读写。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _vector_blob(embedding: Any) -> tuple[int, bytes] | None:
        if not isinstance(embedding, (list, tuple)) or not embedding:
            return None
        try:
            values = array("f", (float(value) for value in embedding))
        except (TypeError, ValueError, OverflowError):
            return None
        return len(values), values.tobytes()

    @staticmethod
    def _vector_list(blob: Any, dim: int) -> list[float]:
        if not blob or int(dim or 0) <= 0:
            return []
        values = array("f")
        try:
            values.frombytes(bytes(blob))
        except (TypeError, ValueError):
            return []
        return list(values[: int(dim)])

    @staticmethod
    def _legacy_payload(record: dict[str, Any]) -> dict[str, Any]:
        # 正文和向量已有专用列，避免在 meta 中再复制一份大对象。
        return {
            str(key): value
            for key, value in dict(record or {}).items()
            if key not in {"text", "embedding", "_sqlite_id"}
        }

    @staticmethod
    def _number(value: Any, default: float, cast=float):
        try:
            return cast(value)
        except (TypeError, ValueError, OverflowError):
            return cast(default)

    @staticmethod
    def _legacy_row(record: dict[str, Any]) -> dict[str, Any]:
        content = str(record.get("text") or "").strip()
        key = str(record.get("rpid") or "").strip()
        if not key or not content:
            raise ValueError("legacy memory requires rpid and text")
        return {
            "legacy_key": key,
            "scope": str(record.get("scope") or "bili_comment"),
            "memory_type": str(record.get("memory_type") or "chat"),
            "level": str(record.get("level") or "recent"),
            "actor_id": str(record.get("actor_id") or ""),
            "thread_id": str(record.get("thread_id") or ""),
            "target_id": str(record.get("target_id") or record.get("bvid") or record.get("oid") or ""),
            "text": content,
            "importance": max(
                1, min(10, MemoryStore._number(record.get("importance"), 5, int))
            ),
            "value_score": MemoryStore._number(record.get("value_score"), 0.5),
            "privacy": MemoryStore._number(record.get("privacy"), 0, int),
            "confidence": MemoryStore._number(record.get("confidence"), 0.5),
            # 旧 JSON 没有可靠的 events 外键，强行沿用数字会让迁移因悬空
            # 引用整体失败；原始值仍保留在 legacy meta 中供审计。
            "source_event": None,
            "meta": json.dumps(
                {"legacy": MemoryStore._legacy_payload(record)},
                ensure_ascii=False,
                default=str,
            ),
            "created_at": MemoryStore._number(record.get("created_at"), now()),
            "expires_at": record.get("expires_at"),
            "promoted_at": record.get("promoted_at_ts"),
            "bytes": len(content.encode("utf-8")),
            "vector": MemoryStore._vector_blob(record.get("embedding")),
            "vector_model": str(record.get("embedding_model") or "legacy"),
        }

    @staticmethod
    def _upsert_legacy_sync(conn, row: dict[str, Any]) -> int:
        mapped = conn.execute(
            "SELECT memory_id FROM legacy_memory_map WHERE legacy_key=?",
            (row["legacy_key"],),
        ).fetchone()
        params = (
            row["scope"], row["memory_type"], row["level"], row["actor_id"],
            row["thread_id"], row["target_id"], row["text"], row["importance"],
            row["value_score"], row["privacy"], row["confidence"],
            row["source_event"], row["meta"], row["created_at"],
            row["expires_at"], row["promoted_at"], row["bytes"],
        )
        if mapped is None:
            cursor = conn.execute(
                "INSERT INTO memories(scope,memory_type,level,actor_id,thread_id,"
                "target_id,text,importance,value_score,privacy,confidence,source_event,"
                "meta,created_at,expires_at,promoted_at,bytes) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                params,
            )
            memory_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO legacy_memory_map(memory_id,legacy_key) VALUES(?,?)",
                (memory_id, row["legacy_key"]),
            )
        else:
            memory_id = int(mapped["memory_id"])
            conn.execute(
                "UPDATE memories SET scope=?,memory_type=?,level=?,actor_id=?,"
                "thread_id=?,target_id=?,text=?,importance=?,value_score=?,privacy=?,"
                "confidence=?,source_event=?,meta=?,created_at=?,expires_at=?,"
                "promoted_at=?,bytes=? WHERE id=?",
                (*params, memory_id),
            )
        vector = row.get("vector")
        if vector:
            dim, blob = vector
            conn.execute(
                "INSERT INTO memory_vectors(memory_id,model,dim,vec) VALUES(?,?,?,?) "
                "ON CONFLICT(memory_id) DO UPDATE SET model=excluded.model,"
                "dim=excluded.dim,vec=excluded.vec",
                (memory_id, row["vector_model"], dim, blob),
            )
        else:
            conn.execute("DELETE FROM memory_vectors WHERE memory_id=?", (memory_id,))
        return memory_id

    async def upsert_legacy(self, record: dict[str, Any]) -> int:
        """将一条兼容记录幂等写入 SQLite，向量单独保存。"""
        row = self._legacy_row(record)
        return int(await self._db.run(self._upsert_legacy_sync, row))

    async def replace_legacy(self, records: list[dict[str, Any]]) -> int:
        """以 records 原子替换旧版兼容记忆，不影响原生 SQLite 记录。"""
        rows = [self._legacy_row(record) for record in records]

        def _replace(conn):
            keep = {row["legacy_key"] for row in rows}
            for row in rows:
                self._upsert_legacy_sync(conn, row)
            existing = conn.execute(
                "SELECT memory_id,legacy_key FROM legacy_memory_map"
            ).fetchall()
            stale_ids = [
                int(item["memory_id"])
                for item in existing
                if str(item["legacy_key"]) not in keep
            ]
            if stale_ids:
                conn.executemany(
                    "DELETE FROM memories WHERE id=?",
                    ((memory_id,) for memory_id in stale_ids),
                )
            return len(rows)

        return int(await self._db.run(_replace))

    async def load_legacy(self) -> list[dict[str, Any]]:
        """按时间顺序加载兼容记录，重建旧业务仍使用的内存视图。"""
        rows = await self._db.fetch_all(
            "SELECT m.*,lm.legacy_key,mv.model AS vector_model,mv.dim AS vector_dim,"
            "mv.vec AS vector_blob FROM memories m "
            "JOIN legacy_memory_map lm ON lm.memory_id=m.id "
            "LEFT JOIN memory_vectors mv ON mv.memory_id=m.id "
            "ORDER BY m.created_at,m.id"
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                meta = json.loads(row["meta"] or "{}")
            except (TypeError, ValueError):
                meta = {}
            record = dict(meta.get("legacy") or {})
            record.update(
                {
                    "rpid": str(row["legacy_key"]),
                    "text": str(row["text"]),
                    "scope": str(row["scope"]),
                    "memory_type": str(row["memory_type"]),
                    "level": str(row["level"]),
                    "importance": int(row["importance"]),
                    "value_score": float(row["value_score"]),
                    "privacy": int(row["privacy"]),
                    "confidence": float(row["confidence"]),
                    "created_at": float(row["created_at"]),
                    "_sqlite_id": int(row["id"]),
                }
            )
            vector = self._vector_list(row["vector_blob"], row["vector_dim"] or 0)
            if vector:
                record["embedding"] = vector
                record["embedding_model"] = str(row["vector_model"] or "legacy")
            result.append(record)
        return result

    async def legacy_count(self) -> int:
        return int(
            await self._db.fetch_value(
                "SELECT COUNT(*) FROM legacy_memory_map", default=0
            )
            or 0
        )

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
                str(scope),
                memory_type,
                level,
                actor_id,
                thread_id,
                target_id,
                content,
                importance,
                value_score,
                privacy,
                confidence,
                source_event,
                json.dumps(meta or {}, ensure_ascii=False),
                now(),
                expires_at,
                mem_bytes,
            ),
        )

    async def promote(self, memory_id: int, to_level: str = "long_term") -> None:
        """晋升记忆层级。recent → long_term → aged。"""
        await self._db.execute(
            "UPDATE memories SET level=?, promoted_at=? WHERE id=?",
            (to_level, now(), memory_id),
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
                id=r["id"],
                scope=r["scope"],
                memory_type=r["memory_type"],
                level=r["level"],
                actor_id=r["actor_id"],
                thread_id=r["thread_id"],
                target_id=r["target_id"],
                text=r["text"],
                importance=r["importance"],
                value_score=r["value_score"],
                privacy=r["privacy"],
                confidence=r["confidence"],
                source_event=r["source_event"],
                meta=json.loads(r["meta"] or "{}"),
                created_at=r["created_at"],
                expires_at=r["expires_at"],
                promoted_at=r["promoted_at"],
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
            )
            or 0
        )

    async def total_count(self) -> int:
        return int(
            await self._db.fetch_value("SELECT COUNT(*) FROM memories", default=0) or 0
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
            actor_id=row["actor_id"],
            display_name=row["display_name"],
            familiarity=row["familiarity"],
            trust=row["trust"],
            warmth=row["warmth"],
            conflict=row["conflict"],
            stage=row["stage"],
            impression=row["impression"],
            topics=json.loads(row["topics"] or "[]"),
            avoid=json.loads(row["avoid"] or "[]"),
            interact_count=row["interact_count"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            updated_at=row["updated_at"],
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
                    actor_id,
                    fact[:200],
                    str(scope),
                    evidence[:200],
                    confidence,
                    approved,
                    now(),
                    expires_at,
                ),
            )
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                return 0
            raise

    async def facts(
        self, actor_id: str, approved_only: bool = False
    ) -> list[ProfileFact]:
        sql = "SELECT * FROM profile_facts WHERE actor_id=? AND (expires_at IS NULL OR expires_at > ?)"
        params: list[Any] = [actor_id, now()]
        if approved_only:
            sql += " AND approved=1"
        sql += " ORDER BY created_at DESC"
        rows = await self._db.fetch_all(sql, params)
        return [
            ProfileFact(
                id=r["id"],
                actor_id=r["actor_id"],
                fact=r["fact"],
                scope=r["scope"],
                evidence=r["evidence"],
                confidence=r["confidence"],
                approved=r["approved"],
                created_at=r["created_at"],
                expires_at=r["expires_at"],
            )
            for r in rows
        ]

    async def delete_fact(self, fact_id: int) -> None:
        await self._db.execute("DELETE FROM profile_facts WHERE id=?", (fact_id,))

    async def purge_expired_facts(self) -> int:
        return await self._db.execute(
            "DELETE FROM profile_facts WHERE expires_at IS NOT NULL AND expires_at < ?",
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
                kind,
                ref,
                title,
                digest,
                json.dumps(facts or {}, ensure_ascii=False),
                json.dumps(tags or [], ensure_ascii=False),
                tokens_used,
                cost_cents,
                now(),
                expires_at,
                0,
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
            id=row["id"],
            kind=row["kind"],
            ref=row["ref"],
            title=row["title"],
            digest=row["digest"],
            facts=json.loads(row["facts"] or "{}"),
            tags=json.loads(row["tags"] or "[]"),
            tokens_used=row["tokens_used"],
            cost_cents=row["cost_cents"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            hits=row["hits"] + 1,
            last_hit_at=now(),
        )

    async def purge_expired(self) -> int:
        return await self._db.execute(
            "DELETE FROM media_digests WHERE expires_at IS NOT NULL AND expires_at < ?",
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
            )
            or 0.0
        )
