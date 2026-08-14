"""多模态理解：两阶段转述 + 独立会话 + 媒体 TTL。

对应 issue #9。问题根源：

1. 旧实现把视频/图片的视觉上下文直接拼到主会话，上下文迅速爆炸。
2. OCR/ASR/视频理解的原始输出（几千 token）全部留在记忆里，挤占有效信息。
3. 没有缓存，同一视频被多人@时重复调用理解模型。

新方案：

**阶段一（digest）**：视觉模型输出结构化事实（who/what/where/when/why/mood/objects），
  限长 500 token，入 media_digests 表并设 TTL。后续命中直接从缓存取。
**阶段二（自然语言转述）**：拿 digest 生成用户友好的描述或回答，这一步才进主会话。
**独立会话**：每个媒体理解任务用 `media_session(kind, ref)`，用完即弃，
  不污染用户对话历史。

配置项（插件配置 schema）：
- ``MEDIA_UNDERSTAND_MODEL``：视觉理解用哪个模型（cheap/accurate）
- ``MEDIA_DIGEST_TTL_HOURS``：摘要缓存多久
- ``MEDIA_MAX_FRAMES``：视频采样帧数上限
- ``MEDIA_TOKEN_BUDGET_DAILY``：每日 token 上限
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from ..security import Caller, Scope, media_session
from ..storage import Database, MediaStore


@dataclass
class MediaDigestRequest:
    """媒体理解请求。"""

    kind: str  # video / image / dynamic
    ref: str   # bvid / 图片 URL / dynamic_id
    title: str = ""
    url: str = ""
    context: str = ""  # 用户问了什么（如"这个视频讲的啥"）


@dataclass
class MediaDigestResult:
    """阶段一产物：结构化事实摘要。"""

    kind: str
    ref: str
    title: str
    digest: str  # 结构化 JSON 或简短事实列表
    facts: dict[str, Any]
    tags: list[str]
    tokens_used: int
    cost_cents: float
    from_cache: bool


class MediaUnderstanding:
    """两阶段媒体理解与缓存管理。"""

    def __init__(
        self,
        db: Database,
        config_getter,
        llm_caller,  # 实际调 LLM 的函数，由 LLMMixin 提供
        vision_caller,  # 实际调视觉模型的函数，由 VisionMixin 提供
    ) -> None:
        self._db = db
        self._get = config_getter
        self._llm = llm_caller
        self._vision = vision_caller
        self._store = MediaStore(db)

    # ------------------------------------------------------------ 配置
    def _digest_ttl(self) -> float:
        hours = float(self._get("MEDIA_DIGEST_TTL_HOURS", 72))
        return hours * 3600

    def _token_budget_daily(self) -> int:
        return int(self._get("MEDIA_TOKEN_BUDGET_DAILY", 50000))

    def _max_frames(self) -> int:
        return int(self._get("MEDIA_MAX_FRAMES", 8))

    def _understand_model(self) -> str:
        return str(self._get("MEDIA_UNDERSTAND_MODEL", "cheap") or "cheap")

    # ------------------------------------------------------------ 阶段一
    async def digest(
        self,
        request: MediaDigestRequest,
        caller: Caller,
        force_refresh: bool = False,
    ) -> MediaDigestResult:
        """阶段一：生成或取缓存的结构化摘要。"""
        if not force_refresh:
            cached = await self._store.get(request.kind, request.ref)
            if cached:
                return MediaDigestResult(
                    kind=cached.kind, ref=cached.ref, title=cached.title,
                    digest=cached.digest, facts=cached.facts, tags=cached.tags,
                    tokens_used=0, cost_cents=0.0, from_cache=True,
                )

        # 预算闸门
        today_key = f"media_token_budget:{self._today()}"
        used = int(await self._db.kv_get(today_key, 0) or 0)
        budget = self._token_budget_daily()
        if used >= budget:
            return MediaDigestResult(
                kind=request.kind, ref=request.ref, title=request.title,
                digest="超出每日预算，已跳过理解",
                facts={"error": "budget_exceeded"}, tags=[], tokens_used=0,
                cost_cents=0.0, from_cache=False,
            )

        # 调用视觉模型（具体实现由 vision_caller 提供）
        session_id = media_session(request.kind, request.ref, caller.account_id)
        raw_output = await self._vision(
            kind=request.kind,
            url=request.url,
            context=request.context,
            model=self._understand_model(),
            max_frames=self._max_frames(),
            session_id=session_id,
        )

        # 提取结构化事实（简化示例，实际由 prompt 控制输出格式）
        digest_text = raw_output.get("digest", "")
        facts = raw_output.get("facts", {})
        tags = raw_output.get("tags", [])
        tokens = raw_output.get("tokens_used", 0)
        cost = raw_output.get("cost_cents", 0.0)

        # 入库并更新预算
        await self._store.put(
            kind=request.kind, ref=request.ref, title=request.title,
            digest=digest_text[:500], facts=facts, tags=tags,
            tokens_used=tokens, cost_cents=cost, ttl=self._digest_ttl(),
        )
        await self._db.kv_set(today_key, used + tokens, ttl=86400)

        return MediaDigestResult(
            kind=request.kind, ref=request.ref, title=request.title,
            digest=digest_text, facts=facts, tags=tags, tokens_used=tokens,
            cost_cents=cost, from_cache=False,
        )

    # ------------------------------------------------------------ 阶段二
    async def narrate(
        self,
        digest_result: MediaDigestResult,
        user_question: str,
        caller: Caller,
    ) -> str:
        """阶段二：把 digest 转述成自然语言回答。

        这一步走主 LLM（非视觉模型），成本低，可以根据用户问题定制回答角度。
        """
        prompt = (
            f"用户问：{user_question}\n\n"
            f"媒体类型：{digest_result.kind}\n"
            f"标题：{digest_result.title}\n"
            f"结构化摘要：{digest_result.digest}\n"
            f"标签：{', '.join(digest_result.tags)}\n\n"
            f"请用自然、友好的语气回答用户的问题。如果摘要里没有相关信息，诚实说不清楚。"
        )
        response = await self._llm(
            prompt=prompt,
            session_id=caller.session_id,
            purpose="media_narrate",
        )
        return response.get("text", "")

    # ------------------------------------------------------------ 维护
    async def purge_expired(self) -> int:
        """清理过期缓存。"""
        return await self._store.purge_expired()

    async def evict_lru(self, keep: int = 100) -> int:
        """LRU 淘汰，避免缓存无限增长。"""
        return await self._store.evict_lru(keep)

    async def stats(self) -> dict[str, Any]:
        total_cost = await self._store.total_cost_cents()
        today_key = f"media_token_budget:{self._today()}"
        used_today = int(await self._db.kv_get(today_key, 0) or 0)
        budget = self._token_budget_daily()
        return {
            "total_cost_cents": total_cost,
            "tokens_used_today": used_today,
            "token_budget_daily": budget,
            "budget_remaining": max(0, budget - used_today),
        }

    @staticmethod
    def _today() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def image_url_hash(url: str) -> str:
    """图片 URL 归一化 hash，用作 ref。"""
    normalized = url.split("?")[0].split("#")[0]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
