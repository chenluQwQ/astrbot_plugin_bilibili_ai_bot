"""近期动态区块（每周演化的结果）：``personality_evolution.json`` 只由这里读写。

核心人设不在这里，这里只有可替换的近期状态、手动维护的习惯和看法、低频梗，以及回滚用的快照。
每周怎么演化在 ``features/personality_evolution.py``，它算好结果再交给 ``save``。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from ..infra.text import repair_llm_json
from ..storage.json_file import JsonFile

BLOCK_KEYS = {
    "recent_state", "recent_preferences", "recent_thoughts",
    "recent_reflections",
}
EVOLUTION_KEYS = {
    "decision", "dynamic_block", "reflection", "meme_candidates",
}
MEME_CANDIDATE_KEYS = {"phrase", "aliases", "evidence", "contexts"}
MEME_RESULT_KEYS = {"phrase", "meaning", "contexts", "avoid", "expires_days"}

# 手动编辑命令的类别
MANUAL_KEYS = {"习惯": "speech_habits", "看法": "opinions", "变化": "evolved_traits"}


def short(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def bounded_list(value: Any, *, count: int, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = short(item, limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= count:
            break
    return result


def normalize_dynamic_block(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    return {
        "recent_state": short(value.get("recent_state"), 100),
        "recent_preferences": bounded_list(
            value.get("recent_preferences"), count=3, limit=70
        ),
        "recent_thoughts": bounded_list(
            value.get("recent_thoughts"), count=3, limit=90
        ),
        "recent_reflections": bounded_list(
            value.get("recent_reflections"), count=2, limit=90
        ),
    }


def normalize_state(value: Any) -> dict[str, Any]:
    """Read old JSON safely and migrate it in memory without deleting fields."""
    state = dict(value) if isinstance(value, dict) else {}
    raw_traits = state.get("evolved_traits", [])
    state["evolved_traits"] = []
    for item in raw_traits if isinstance(raw_traits, list) else []:
        if isinstance(item, dict):
            change = short(item.get("change"), 100)
            if change:
                state["evolved_traits"].append({
                    "time": short(item.get("time"), 20),
                    "change": change,
                    "trigger": short(item.get("trigger"), 100),
                })
        else:
            change = short(item, 100)
            if change:
                state["evolved_traits"].append({
                    "time": "", "change": change, "trigger": "旧版记录",
                })
    state["evolved_traits"] = state["evolved_traits"][-10:]
    state["speech_habits"] = bounded_list(
        state.get("speech_habits"), count=5, limit=70
    )
    state["opinions"] = bounded_list(
        state.get("opinions"), count=5, limit=80
    )
    if not isinstance(state.get("dynamic_block"), dict):
        state["dynamic_block"] = {
            "recent_state": short(state.get("last_reflection"), 100),
            "recent_preferences": bounded_list(
                state["opinions"], count=3, limit=70
            ),
            "recent_thoughts": bounded_list(
                [
                    item.get("change", "")
                    for item in state["evolved_traits"][-3:]
                ],
                count=3,
                limit=90,
            ),
            "recent_reflections": [],
        }
    state["dynamic_block"] = normalize_dynamic_block(
        state.get("dynamic_block")
    )
    try:
        state["version"] = max(0, int(state.get("version", 0) or 0))
    except (TypeError, ValueError):
        state["version"] = 0
    state["history"] = (
        [
            item for item in state.get("history", []) if isinstance(item, dict)
        ][-12:]
        if isinstance(state.get("history"), list)
        else []
    )
    state["memes"] = (
        [
            item for item in state.get("memes", []) if isinstance(item, dict)
        ][:2]
        if isinstance(state.get("memes"), list)
        else []
    )
    state.setdefault("last_reflection", "")
    state.setdefault("last_evolve", "")
    state.setdefault("last_evolve_week", "")
    return state


def meme_is_active(item: Any, today: str) -> bool:
    return (
        isinstance(item, dict)
        and bool(str(item.get("phrase") or "").strip())
        and str(item.get("expires_at") or "")[:10] >= today
    )


def meme_exposed_for_context(item: dict[str, Any], context: str) -> bool:
    """Deterministically expose a meme in about 15% of suitable reply calls."""
    if not context.strip():
        return False
    key = (
        f"{datetime.now().strftime('%G-W%V')}|{item.get('phrase', '')}|{context}"
    )
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:2], "big") % 100 < 15


def snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": int(state.get("version", 0) or 0),
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "dynamic_block": state.get("dynamic_block", {}),
        "memes": state.get("memes", []),
        "last_reflection": str(state.get("last_reflection") or ""),
    }


def parse_weekly_evolution(raw_text: Any) -> dict[str, Any]:
    """模型给的每周演化结果：字段严格对上才收，否则抛 ValueError。"""
    try:
        value = json.loads(repair_llm_json(str(raw_text or "")))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_evolution_json") from exc
    if not isinstance(value, dict) or set(value) != EVOLUTION_KEYS:
        raise ValueError("evolution_schema_mismatch")
    decision = str(value.get("decision") or "").strip().lower()
    if decision not in {"keep", "update"}:
        raise ValueError("invalid_evolution_decision")
    block_raw = value.get("dynamic_block")
    if not isinstance(block_raw, dict) or set(block_raw) != BLOCK_KEYS:
        raise ValueError("dynamic_block_schema_mismatch")
    block = normalize_dynamic_block(block_raw)
    reflection = short(value.get("reflection"), 120)
    raw_candidates = value.get("meme_candidates")
    if not isinstance(raw_candidates, list) or len(raw_candidates) not in {
        0, 3, 4, 5,
    }:
        raise ValueError("meme_candidates_count_mismatch")
    candidates = []
    for item in raw_candidates:
        if not isinstance(item, dict) or set(item) != MEME_CANDIDATE_KEYS:
            raise ValueError("meme_candidate_schema_mismatch")
        phrase = short(item.get("phrase"), 30)
        evidence = bounded_list(
            item.get("evidence"), count=5, limit=80
        )
        if not phrase or not evidence:
            raise ValueError("meme_candidate_missing_evidence")
        candidates.append({
            "phrase": phrase,
            "aliases": bounded_list(
                item.get("aliases"), count=4, limit=30
            ),
            "evidence": evidence,
            "contexts": bounded_list(
                item.get("contexts"), count=3, limit=50
            ),
        })
    return {
        "decision": decision,
        "dynamic_block": block,
        "reflection": reflection,
        "meme_candidates": candidates,
    }


class Personality:
    def __init__(self, file: JsonFile):
        self._file = file

    # ── 读 ──
    def state(self) -> dict[str, Any]:
        """整理过的状态（旧格式在内存里补齐，不删字段）。"""
        return normalize_state(self._file.read({}))

    def prompt(self, context="") -> str:
        state = self.state()
        block = state["dynamic_block"]
        parts = []
        if block["recent_state"]:
            parts.append(f"最近状态：{block['recent_state']}")
        if block["recent_preferences"]:
            parts.append("近期喜好：" + "；".join(block["recent_preferences"]))
        if block["recent_thoughts"]:
            parts.append("最近感想：" + "；".join(block["recent_thoughts"]))
        if block["recent_reflections"]:
            parts.append("近期反思：" + "；".join(block["recent_reflections"]))

        # Legacy/manual entries remain compatible, but automatic evolution never
        # appends to these core-adjacent fields.
        habits = bounded_list(state.get("speech_habits"), count=5, limit=70)
        opinions = bounded_list(state.get("opinions"), count=5, limit=80)
        if habits:
            parts.append("手动维护的说话习惯：" + "；".join(habits))
        if opinions:
            parts.append("手动维护的看法：" + "；".join(opinions))

        today = datetime.now().strftime("%Y-%m-%d")
        for meme in state.get("memes", [])[:2]:
            if meme_is_active(meme, today) and meme_exposed_for_context(
                meme, str(context or "")
            ):
                contexts = bounded_list(
                    meme.get("contexts"), count=3, limit=50
                )
                avoid = short(meme.get("avoid"), 90)
                hint = (
                    f"近期低频表达参考：{short(meme.get('phrase'), 30)}。"
                    "只在眼前语境确实合适时偶尔使用一次，不要硬塞或解释梗。"
                )
                if contexts:
                    hint += "适合：" + "、".join(contexts) + "。"
                if avoid:
                    hint += "避免：" + avoid + "。"
                parts.append(hint)
                break
        if not parts:
            return ""
        return "【每周更新的近期动态区块（不改变核心人设）】\n" + "\n".join(parts)

    # ── 写 ──
    def save(self, state: dict[str, Any]) -> None:
        self._file.write(state)

    def rollback(self) -> tuple[bool, str]:
        state = self.state()
        history = state.get("history", [])
        if not history:
            return False, "没有可回滚的自动演化版本"
        snap = history.pop()
        previous_version = state["version"]
        state["dynamic_block"] = normalize_dynamic_block(
            snap.get("dynamic_block")
        )
        state["memes"] = (
            list(snap.get("memes", []))[:2]
            if isinstance(snap.get("memes"), list)
            else []
        )
        state["last_reflection"] = short(
            snap.get("last_reflection"), 120
        )
        state["history"] = history[-12:]
        state["version"] = previous_version + 1
        state["rollback"] = {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "from_version": previous_version,
            "restored_snapshot_version": int(snap.get("version", 0) or 0),
        }
        self.save(state)
        return True, (
            f"已回滚自动演化内容（v{previous_version} → 快照v"
            f"{int(snap.get('version', 0) or 0)}，当前记录v{state['version']}）"
        )

    # 手动编辑直接改原始 JSON，不经过整理，和以前的命令一样
    def add_manual(self, category: str, content: str) -> str:
        """返回给主人看的结果；类别不对时不写。"""
        evo = self._file.read({})
        if not evo:
            evo = {"version": 0, "last_evolve": "", "evolved_traits": [], "speech_habits": [], "opinions": [], "last_reflection": ""}
        if category == "习惯":
            evo.setdefault("speech_habits", []).append(content)
            evo["speech_habits"] = evo["speech_habits"][-5:]
            message = f"✅ 已添加说话习惯：{content}"
        elif category == "看法":
            evo.setdefault("opinions", []).append(content)
            evo["opinions"] = evo["opinions"][-5:]
            message = f"✅ 已添加看法：{content}"
        elif category == "变化":
            evo.setdefault("evolved_traits", []).append({"time": datetime.now().strftime("%Y-%m-%d"), "change": content, "trigger": "手动添加"})
            evo["evolved_traits"] = evo["evolved_traits"][-10:]
            message = f"✅ 已添加成长变化：{content}"
        else:
            return "❌ 类别不对，可选：习惯、看法、变化"
        evo["version"] = evo.get("version", 0) + 1
        self.save(evo)
        return message

    def remove_manual(self, category: str, idx: int) -> str:
        """``idx`` 从 0 开始；返回给主人看的结果。"""
        evo = self._file.read({})
        if not evo:
            return "🌱 没有演化记录"
        key = MANUAL_KEYS.get(category)
        if not key:
            return "❌ 类别不对，可选：习惯、看法、变化"
        items = evo.get(key, [])
        if idx < 0 or idx >= len(items):
            return f"❌ 序号超范围（1-{len(items)}）"
        removed = items.pop(idx)
        evo["version"] = evo.get("version", 0) + 1
        self.save(evo)
        desc = removed.get("change", removed) if isinstance(removed, dict) else removed
        return f"✅ 已删除：{desc}"
