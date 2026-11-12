"""服务器版旧宿主接口 → 2.0.3 分层宿主的兼容层。

server_ext/ 下的移植模块原本写在 1.7.x 单体插件上，依赖一批 ``bot._xxx``
便捷方法；2.0.3 已把它们拆进 ``models`` / ``vision`` / ``bili_http`` /
``user_profiles`` / ``activity_logs``。这里做一层薄映射，避免为了几个调用
去改 2.0.3 的原有模块。

本文件只服务 server_ext，不改变 2.0.3 既有行为。
"""
from __future__ import annotations

import os
import re

from astrbot.api import logger

from ..config import DATA_DIR
from ..storage.json_file import JsonFile

# 和服务器版 security/redact.py 中的 PII 规则保持一致：喂给模型前替换成抽象说辞。
_PII_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号"),
    (re.compile(r"(?<!\d)0\d{2,3}[-—]\d{7,8}(?!\d)"), "座机号"),
    (re.compile(r"(?<!\d)[1-9]\d{4,10}(?!\d)"), "QQ号"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "邮箱"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "身份证号"),
    (re.compile(r"(?<!\d)\d{13,19}(?!\d)"), "银行卡号"),
    (
        re.compile(
            r"(密码|口令|password|passwd|pwd|验证码|验证code|code)\s*[:：=]\s*[^\s,;，。；\"']+",
            re.IGNORECASE,
        ),
        r"\1=[已隐藏]",
    ),
    (re.compile(r"\b(?:eyJ[A-Za-z0-9._-]{8,}|[A-Za-z0-9]{32,})\b"), "凭据"),
)


def redact_pii(text) -> str:
    """替换个人信息后再交给模型；与服务器版规则一致。"""
    result = str(text or "")
    for pattern, replacement in _PII_RULES:
        result = pattern.sub(replacement, result)
    return result


def redact_outbound(text, internal: bool = False):
    """转发到 2.0.2 的出站脱敏；签名保持服务器版的返回 ``(text, hits)``。"""
    from ..security.redact import redact_outbound as _redact

    return _redact(str(text or ""), internal=internal)


_FUSION_FLAG_KEYS = {
    "LIVINGMEMORY_SYNC_ENABLED": "FUSION_LIVINGMEMORY_ENABLED",
    "GALLERY_COLLECT_ENABLED": "FUSION_GALLERY_ENABLED",
    "GALLERY_COMMENT_ENABLED": "FUSION_GALLERY_COMMENT_ENABLED",
}


def _legacy_value(config, key):
    """按「旧的融合插件分组 → 顶层键」顺序取历史配置。"""
    try:
        fusion = config.get("FUSION_PLUGINS")
    except Exception:
        fusion = None
    if isinstance(fusion, dict) and fusion.get(key) is not None:
        return fusion.get(key)
    return config.get(key)


def config_value(bot, key: str, default=None):
    """读插件配置：优先三个「融合插件」开关，兼容旧分组/顶层键。"""
    try:
        config = bot.config
        if not hasattr(config, "get"):
            return default
        flag_key = _FUSION_FLAG_KEYS.get(key)
        if flag_key:
            flag = config.get(flag_key)
            if isinstance(flag, bool):
                return flag
        if key == "PROACTIVE_HOT_REPLY_COUNT":
            flag = config.get("FUSION_HOT_REPLY_ENABLED")
            if isinstance(flag, bool):
                if not flag:
                    return 0
                try:
                    count = int(float(_legacy_value(config, key) or 0))
                except (TypeError, ValueError):
                    count = 0
                return count if count > 0 else 1
        value = _legacy_value(config, key)
    except Exception:
        return default
    return default if value is None else value


def _path(path) -> str:
    """旧代码传的是 DATA_DIR 下的绝对路径，统一落到 2.0.2 的 DATA_DIR。"""
    return os.path.join(DATA_DIR, os.path.basename(str(path)))


def load_json(bot, path, default):
    """等价旧 ``bot._load_json``：只读 DATA_DIR 下的 json。"""
    return JsonFile(_path(path)).read(default)


def save_json(bot, path, data) -> None:
    """等价旧 ``bot._save_json``：原子写入 DATA_DIR 下的 json。"""
    JsonFile(_path(path)).write(data)


def activity_entries(bot, kind: str, fallback_path, default=()) -> list[dict]:
    """优先读 2.0.2 的活动日志（sqlite），退回旧 json 文件。

    2.0.2 把 watch/reply/proactive 三类流水都收进了 ``activity_logs``；
    移植模块只关心「条目列表」，这里屏蔽两代存储差异。
    """
    log = getattr(getattr(bot, "activity_logs", None), kind, None)
    if log is not None:
        try:
            entries = log.recent()
            if isinstance(entries, list):
                return [item for item in entries if isinstance(item, dict)]
        except Exception as exc:
            logger.debug(f"[BiliBot] 读取活动日志 {kind} 失败，退回 json: {exc!r}")
    data = load_json(bot, fallback_path, list(default))
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def proactive_entries(bot) -> list[dict]:
    """主动评论流水（旧 proactive_log.json）。"""
    from ..config import PROACTIVE_LOG_FILE

    return activity_entries(bot, "proactive", PROACTIVE_LOG_FILE)
