"""宿主 AstrBot 知识库只读查询（kb_manager）。

按需检索管理员显式授权的资料库，给评论生成等 LLM 场景当补充资料。
边界与 LivingMemory 统一检索（unified_context）保持同一口径：

- 只读：不写 kb_manager 的任何状态，不缓存检索正文（删除后不会被旧结果复活）；
- 白名单：只有 `KB_LOOKUP_IDS` 里逐字写出的资料库可用，空配置一律拒绝；
- 防碰撞：`get_kb` 的实例必须与 `get_kb_by_name` 的解析结果同一对象，
  防止别的资料库拿授权 UUID 当名称（同名重建后的旧 UUID 不返回内容）；
- 限额：每查询 ≤3 库、每来源 ≤4 条、单条 ≤1600 字、10 秒预算。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger

from .host_view import HostView

_KB_BUDGET_SECONDS = 10.0
_KB_PER_QUERY_MAX = 3
_TOP_M_FINAL = 4
_MAX_CHUNK_CHARS = 1600

_warned_unavailable = False


def _warn_once(message: str) -> None:
    global _warned_unavailable
    if not _warned_unavailable:
        _warned_unavailable = True
        logger.warning(message)


async def authorized_kbs(kb_manager, allowed_ids: list[str]) -> dict[str, str]:
    """白名单 → {kb_id: kb_name}；不可用、缺失或碰撞的库直接跳过，不猜。"""
    helpers: dict[str, str] = {}
    ids = [str(x).strip() for x in allowed_ids if str(x).strip()][:_KB_PER_QUERY_MAX]
    for kb_id in ids:
        try:
            helper = await kb_manager.get_kb(kb_id)
            if helper is None or getattr(helper, "init_error", None):
                continue
            # 宿主 get_kb_by_name 先按名称后按 UUID 兜底；交叉复核防止
            # 另一资料库使用授权 UUID 作名称造成碰撞。
            resolved = await kb_manager.get_kb_by_name(kb_id)
        except Exception:
            continue
        if resolved is not helper or str(helper.kb.kb_id) != kb_id:
            continue
        helpers[kb_id] = str(helper.kb.kb_name)
    return helpers


class KnowledgeLookup(HostView):
    """宿主知识库的只读查询客户端。"""

    HOST_API = frozenset({"context", "settings"})

    async def lookup(self, query: str) -> str:
        """检索授权知识库；返回拼好的资料块（未命中或不可用返回空串）。"""
        text = str(query or "").strip()
        if not text:
            return ""
        allowed = self.settings.list("KB_LOOKUP_IDS")
        if not allowed:
            _warn_once("[BiliBot] KB_LOOKUP_IDS 为空，知识库查询工具停用")
            return ""
        kb_manager = getattr(self.context, "kb_manager", None)
        if kb_manager is None:
            _warn_once("[BiliBot] 宿主 kb_manager 不可用，知识库查询停用")
            return ""

        try:
            helpers = await authorized_kbs(kb_manager, allowed[:_KB_PER_QUERY_MAX])
            if not helpers:
                _warn_once("[BiliBot] 知识库白名单内没有可用知识库，查询停用")
                return ""
            async with asyncio.timeout(_KB_BUDGET_SECONDS):
                result = await kb_manager.retrieve(
                    text[:1200],
                    kb_names=list(helpers.values()),
                    top_k_fusion=12,
                    top_m_final=_TOP_M_FINAL,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[BiliBot] 知识库查询不可用，本次跳过")
            return ""

        lines = []
        for item in (result or {}).get("results", []):
            if not isinstance(item, dict):
                continue
            # 逐条复核归属库；白名单外的候选一律丢弃。
            if str(item.get("kb_id")) not in helpers:
                continue
            if not all(item.get(k) for k in ("kb_id", "doc_id", "chunk_id", "content")):
                continue
            content = str(item["content"])[:_MAX_CHUNK_CHARS]
            name = str(item.get("kb_name", ""))[:100]
            doc = str(item.get("doc_name", ""))[:255]
            lines.append(f"【{name} / {doc}】\n{content}")
            if len(lines) >= _TOP_M_FINAL:
                break
        if not lines:
            return ""
        logger.info("[BiliBot] 知识库查询命中 %d 条（库：%s）", len(lines), sorted(helpers))
        return "\n\n".join(lines)
