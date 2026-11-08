"""插件配置的唯一入口。

默认值只认 `_conf_schema.json`：AstrBot 加载插件配置时会用 schema 默认值补齐缺的键，
线上生效的本来就是它。代码里不再写 `config.get(键名, 默认值)`，改成 `settings.get(键名)`
或带类型的 `settings.int(键名)` 等。

`Settings` 不缓存配置，每次都读 AstrBot 传进来的那个配置对象，所以 WebUI 改完配置立刻生效。
这个模块不依赖 AstrBot，测试可以直接用。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"

# 代码在读、但 schema 里没有的键。AstrBot 加载时会删掉 schema 外的键，
# 所以写进配置的 BUVID3/BUVID4 重启后会丢，下次再自动获取。
CODE_ONLY_DEFAULTS: dict[str, Any] = {
    "BUVID3": "",
    "BUVID4": "",
    "VIDEO_SEGMENT_SEC": 300,
}


# 新键名 → 改名前的旧键名。新键还是默认值、旧键被用户改过时，按旧键的值算，
# 这样老用户升级后不用重新填。
LEGACY_KEYS: dict[str, str] = {
    "AUTONOMOUS_REPLY_DAILY_MAX": "AUTONOMOUS_REPLY_DAILY_LIMIT",
    "AUTONOMOUS_PRIVATE_DAILY_MAX": "AUTONOMOUS_PRIVATE_DAILY_LIMIT",
    "AUTONOMOUS_DYNAMIC_DAILY_MAX": "AUTONOMOUS_DYNAMIC_DAILY_LIMIT",
    "AUTONOMOUS_PROACTIVE_DAILY_MAX": "AUTONOMOUS_PROACTIVE_DAILY_LIMIT",
}


@lru_cache(maxsize=1)
def schema_defaults() -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    defaults = {key: field.get("default") for key, field in schema.items()}
    overlap = set(defaults) & set(CODE_ONLY_DEFAULTS)
    if overlap:
        raise RuntimeError(f"keys are both in the schema and CODE_ONLY_DEFAULTS: {sorted(overlap)}")
    defaults.update(CODE_ONLY_DEFAULTS)
    return defaults


def with_schema_defaults(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """给测试用：先铺上全部默认值，再盖上要测的值，和线上拿到的配置一样完整。"""
    config = json.loads(json.dumps(schema_defaults()))
    config.update(overrides or {})
    return config


class Settings:
    def __init__(self, config: Mapping[str, Any], defaults: Mapping[str, Any] | None = None):
        self._config = config
        self._defaults = schema_defaults() if defaults is None else defaults

    def default(self, key: str) -> Any:
        if key not in self._defaults:
            raise KeyError(f"unknown config key: {key}")
        return self._defaults[key]

    def get(self, key: str) -> Any:
        default = self.default(key)
        value = self._config.get(key)
        if value is None:
            value = default
        legacy_key = LEGACY_KEYS.get(key)
        if legacy_key and value == default:
            legacy = self._config.get(legacy_key)
            if legacy is not None and legacy != default:
                return legacy
        return value

    def bool(self, key: str) -> bool:
        value = self.get(key)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def int(self, key: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
        try:
            value = int(self.get(key))
        except (TypeError, ValueError):
            value = int(self.default(key))
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def float(self, key: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
        try:
            value = float(self.get(key))
        except (TypeError, ValueError):
            value = float(self.default(key))
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def str(self, key: str) -> str:
        return str(self.get(key) or "").strip()

    def list(self, key: str) -> list[str]:
        value = self.get(key)
        if isinstance(value, str):
            value = value.splitlines()
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]


class SettingsAccessor:
    """让插件对象通过 `self.settings` 读配置。"""

    def __get__(self, obj: Any, owner: type | None = None) -> Any:
        if obj is None:
            return self
        return Settings(obj.config)
