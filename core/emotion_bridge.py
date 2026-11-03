"""从绫「情绪与关系」模块的 BiliBot 双读桥接（阶段 3，首周 observe_only）。

只做两件事：
1. 每次互动后在后台把脱敏摘要上报给从绫 EmotionService.ingest（不发送原文）；
2. 日终清算后做一次好感/亲和度对账，供首周观察两边偏差。

BiliBot 本地好感写入保持原样（双读），所以这里所有失败都只记日志，不影响主链。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from astrbot.api import logger

from .config import DATA_DIR

CONGLING_PLUGIN = "astrbot_plugin_congling"
RECONCILE_FILE = os.path.join(DATA_DIR, "emotion_reconcile.json")
RECONCILE_LIMIT = 50          # 每天最多对账多少位用户，避免清算拖太久
DEFAULT_WARN_THRESHOLD = 15.0  # 本地好感与从绫亲和度归一化后的偏差告警阈值（0~100）


def _emotion_service(plugin: Any) -> Any:
    """取从绫插件上的 EmotionService；没装从绫时返回 None。"""
    try:
        context = getattr(plugin, "context", None)
        meta = context.get_registered_star(CONGLING_PLUGIN) if context else None
        return getattr(getattr(meta, "star_cls", None), "emotion", None)
    except Exception as exc:
        logger.debug(f"[BiliBot] 查找从绫情绪模块失败: {exc!r}")
        return None


def _bridge_config(service: Any) -> tuple[str, bool, float]:
    """读从绫配置：(read_mode, observe_enabled, warn_threshold)。"""
    read_mode = "dual_read"
    enabled = True
    threshold = DEFAULT_WARN_THRESHOLD
    cfg = getattr(service, "cfg", None)
    try:
        if cfg is not None:
            read_mode = str(cfg.str("emotion.bili.read_mode", "dual_read") or "dual_read")
            enabled = bool(cfg.bool("emotion.observe.enabled", True))
            threshold = float(
                cfg.float("emotion.bili.reconcile_warn_threshold", DEFAULT_WARN_THRESHOLD)
                or DEFAULT_WARN_THRESHOLD
            )
    except Exception as exc:
        logger.debug(f"[BiliBot] 读取从绫情绪配置失败，按双读兜底: {exc!r}")
    if read_mode not in {"dual_read", "from_congling"}:
        read_mode = "off"
    return read_mode, enabled, max(1.0, min(100.0, threshold))


def _as_score(value: Any) -> float:
    """本地 affection.json 里通常是数字，容错字典/字符串写法。"""
    if isinstance(value, dict):
        value = value.get("score", 0)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def report_relationship_event(plugin: Any, mid: Any, username: str, score_delta: Any, source: str) -> None:
    """同步入口：只投递后台任务，调用方（评论/私信主链）不被 IO 阻塞。"""
    try:
        delta = int(score_delta)
    except (TypeError, ValueError):
        delta = 0
    # 分数没变化就不落事件，避免把每天大量「无变化互动」灌进情绪时间线。
    if delta == 0:
        return
    try:
        asyncio.get_running_loop().create_task(
            _report(plugin, mid, username, delta, source)
        )
    except RuntimeError:
        logger.debug("[BiliBot] 无事件循环，跳过情绪上报")
    except Exception as exc:
        logger.debug(f"[BiliBot] 情绪上报投递失败: {exc!r}")


async def _report(plugin: Any, mid: Any, username: str, delta: int, source: str) -> None:
    try:
        service = _emotion_service(plugin)
        if service is None:
            return
        read_mode, enabled, _ = _bridge_config(service)
        if not enabled or read_mode == "off":
            return
        private = str(source) in {"private", "dm"}
        if delta > 0:
            label = "开心"
        elif private:
            label = "难过"
        else:
            label = "生气"
        summary = (
            f"{'私信' if private else '评论区'}互动，关系分数变化 {delta:+d}"
        )
        result = await service.ingest({
            "source_account": f"bili:{mid}",
            "platform": "bili",
            "account_id": str(mid),
            "label": label,
            "intensity": min(0.9, 0.3 + abs(delta) / 10.0),
            "summary": summary,
            "scene": "private" if private else "video_comment",
            "audience": "self_private" if private else "public",
            "source_msg_id": f"bilibot:{time.time():.0f}",
            "confidence": 0.6,
        })
        if not result.get("ok"):
            logger.debug(f"[BiliBot] 情绪上报被跳过: {result.get('error')}")
        else:
            logger.debug(
                f"[BiliBot] 情绪上报成功: {mid} {label} -> {result.get('person')}"
            )
    except Exception as exc:
        logger.debug(f"[BiliBot] 情绪上报异常: {exc!r}")


async def run_daily_reconcile(plugin: Any) -> None:
    """日终对账：本地好感 0~100 vs 从绫 affinity（0~1000 归一化到 0~100）。"""
    service = _emotion_service(plugin)
    if service is None:
        logger.info("[BiliBot] 未找到从绫情绪模块，跳过每日对账")
        return
    read_mode, enabled, threshold = _bridge_config(service)
    if not enabled or read_mode == "off":
        logger.info(f"[BiliBot] 情绪双读已关闭（read_mode={read_mode}），跳过每日对账")
        return
    affection = getattr(plugin, "_affection", {}) or {}
    pairs = sorted(
        ((str(mid), _as_score(score)) for mid, score in affection.items()),
        key=lambda item: item[1],
        reverse=True,
    )[:RECONCILE_LIMIT]
    checked = 0
    warned = 0
    items: list[dict[str, Any]] = []
    for mid, local in pairs:
        try:
            payload = await service.state_payload({"source_account": f"bili:{mid}"})
        except Exception as exc:
            logger.debug(f"[BiliBot] 对账读取失败 {mid}: {exc!r}")
            continue
        affinity = payload.get("affinity") or {}
        remote = affinity.get("score")
        if remote is None:
            # 从绫 affinity 可能按来源账号记账（群聊里认识的人），按原键补查一次。
            try:
                row = await service.store.fetch_one(
                    "SELECT score FROM affinity WHERE persona_id=? AND subject_key=?",
                    (service.persona_id, f"bili:{mid}"),
                )
                remote = (row or {}).get("score")
            except Exception:
                remote = None
        checked += 1
        if remote is None:
            # 从绫还没有这个人的 affinity 行：不算偏差，只记录待补。
            items.append({
                "uid": mid, "local": round(local, 1), "remote": None,
                "deviation": None, "flagged": False, "reason": "从绫暂无 affinity",
            })
            continue
        remote_norm = max(0.0, min(100.0, float(remote) / 10.0))
        deviation = round(local - remote_norm, 1)
        flagged = abs(deviation) >= threshold
        if flagged:
            warned += 1
        items.append({
            "uid": mid,
            "local": round(local, 1),
            "remote": round(remote_norm, 1),
            "deviation": deviation,
            "flagged": flagged,
            "reason": "偏差超阈值" if flagged else "",
        })
    report = {
        "checked_at": int(time.time()),
        "read_mode": read_mode,
        "threshold": threshold,
        "checked": checked,
        "warned": warned,
        "truncated": len(affection) > RECONCILE_LIMIT,
        "items": items,
    }
    try:
        plugin._save_json(RECONCILE_FILE, report)
    except Exception as exc:
        logger.error(f"[BiliBot] 写入对账文件失败: {exc!r}")
    logger.info(
        f"[BiliBot] 🧮 情绪对账完成：检查 {checked} 人，偏差超阈值 {warned} 人"
        f"（阈值 {threshold:g}）"
    )
    if warned:
        logger.warning(f"[BiliBot] ⚠️ {warned} 人的本地好感与从绫亲和度偏差超阈值，详见 {RECONCILE_FILE}")
