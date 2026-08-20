"""定时任务调度：主动视频和动态发布的时间管理。"""
import asyncio
import hashlib
import json
import random
import re
from datetime import datetime, timedelta
from astrbot.api import logger
from .config import (
    AUTONOMOUS_PLAN_FILE, BANGUMI_SCHEDULE_FILE, DYNAMIC_SCHEDULE_FILE,
    DYNAMIC_WATCH_SCHEDULE_FILE, SCHEDULE_FILE, SPECIAL_FOLLOW_SCHEDULE_FILE,
)


class ScheduleMixin:
    """日程管理。"""

    _SCHEDULE_TRIGGER_GRACE_MINUTES = 5
    _PLAN_GENERATION_WINDOW_MINUTES = 15
    _PLAN_RETRY_GRACE_MINUTES = 5

    _AUTONOMOUS_LIMITS = {
        "reply": ("AUTONOMOUS_REPLY_DAILY_MIN", "AUTONOMOUS_REPLY_DAILY_MAX", "AUTONOMOUS_REPLY_DAILY_LIMIT", 80),
        "private": ("AUTONOMOUS_PRIVATE_DAILY_MIN", "AUTONOMOUS_PRIVATE_DAILY_MAX", "AUTONOMOUS_PRIVATE_DAILY_LIMIT", 30),
        "dynamic": ("AUTONOMOUS_DYNAMIC_DAILY_MIN", "AUTONOMOUS_DYNAMIC_DAILY_MAX", "AUTONOMOUS_DYNAMIC_DAILY_LIMIT", 2),
        "proactive": ("AUTONOMOUS_PROACTIVE_DAILY_MIN", "AUTONOMOUS_PROACTIVE_DAILY_MAX", "AUTONOMOUS_PROACTIVE_DAILY_LIMIT", 4),
    }

    def _autonomous_limit_range(self, kind):
        """Return ``(0, hard maximum)`` for an autonomous safety budget.

        The old ``*_DAILY_LIMIT`` keys remain readable for existing installs.
        If a user has customized an old key and the new max key is still at its
        schema default, the old value is treated as the migrated maximum.  Old
        lower-bound keys are deliberately ignored: no behaviour is performed to
        fill a quota.
        """
        min_key, max_key, legacy_key, default_max = self._AUTONOMOUS_LIMITS[kind]
        maximum = self.config.get(max_key, None)
        legacy = self.config.get(legacy_key, default_max)
        try:
            legacy = int(legacy) if legacy is not None else int(default_max)
        except (TypeError, ValueError):
            legacy = int(default_max)
        try:
            maximum = int(maximum) if maximum is not None else legacy
        except (TypeError, ValueError):
            maximum = legacy
        if maximum == int(default_max) and legacy != int(default_max):
            maximum = legacy
        maximum = max(maximum, 0)
        return 0, maximum

    def _autonomous_limit_max(self, kind):
        return self._autonomous_limit_range(kind)[1]

    @staticmethod
    def _parse_time_value(value):
        try:
            hour, minute = str(value).strip().split(":", 1)
            hour, minute = int(hour), int(minute)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except (TypeError, ValueError):
            pass
        return None

    @classmethod
    def _parse_window_value(cls, value):
        """Parse ``HH:MM-HH:MM`` or a schedule-window mapping."""
        if isinstance(value, dict):
            start_raw = value.get("start_time") or value.get("start")
            end_raw = value.get("end_time") or value.get("end")
            scheduled_raw = value.get("scheduled_time") or value.get("time")
        else:
            parts = re.split(r"\s*(?:-|—|–|~|至)\s*", str(value or "").strip(), maxsplit=1)
            if len(parts) != 2:
                return None
            start_raw, end_raw = parts
            scheduled_raw = None
        start = cls._parse_time_value(start_raw)
        end = cls._parse_time_value(end_raw)
        if start is None or end is None:
            return None
        start_minute = start[0] * 60 + start[1]
        end_minute = end[0] * 60 + end[1]
        duration = (end_minute - start_minute) % 1440
        if duration < 15:
            return None
        scheduled = cls._parse_time_value(scheduled_raw) if scheduled_raw else None
        return {
            "start_time": f"{start[0]:02d}:{start[1]:02d}",
            "end_time": f"{end[0]:02d}:{end[1]:02d}",
            "start_minute": start_minute,
            "end_minute": end_minute,
            "duration_minutes": duration,
            "scheduled_time": f"{scheduled[0]:02d}:{scheduled[1]:02d}" if scheduled else "",
        }

    @staticmethod
    def _minute_text(minute):
        minute %= 1440
        return f"{minute // 60:02d}:{minute % 60:02d}"

    def _window_trigger_minute(self, start_minute, duration_minutes, seed):
        """Pick a stable 15-minute trigger inside the middle half of a window."""
        safe_duration = max(15, int(duration_minutes))
        low = max(0, safe_duration // 4)
        high = max(low, safe_duration - low - 1)
        digest = int(hashlib.sha256(str(seed).encode()).hexdigest()[:8], 16)
        offset = low + digest % max(1, high - low + 1)
        return ((start_minute + offset) // 15 * 15) % 1440

    def _fixed_window_entries(self):
        entries = []
        for index, raw in enumerate(self.config.get("FIXED_PROACTIVE_WINDOWS", []) or []):
            parsed = self._parse_window_value(raw)
            if parsed and self._is_awake_minute(parsed["start_minute"]):
                if not parsed["scheduled_time"]:
                    trigger = self._window_trigger_minute(parsed["start_minute"], parsed["duration_minutes"], f"fixed|{index}|{raw}")
                    parsed["scheduled_time"] = self._minute_text(trigger)
                entries.append(parsed)
        if entries:
            return entries
        # Backward compatibility: turn old exact times into centered windows.
        duration = max(30, int(self.config.get("AUTONOMOUS_PROACTIVE_WINDOW_MINUTES", 90)))
        for index, pair in enumerate(self._fixed_time_pairs("FIXED_PROACTIVE_TIMES")):
            center = pair[0] * 60 + pair[1]
            start = max(0, center - duration // 2)
            end = min(1439, start + duration)
            entries.append({
                "start_time": self._minute_text(start), "end_time": self._minute_text(end),
                "start_minute": start, "end_minute": end, "duration_minutes": end - start,
                "scheduled_time": self._minute_text(center),
            })
        return entries

    def _autonomous_generation_due(self, now=None):
        now = now or datetime.now()
        mode = str(self.config.get("AUTONOMOUS_PLAN_GENERATION_MODE", "after_sleep") or "after_sleep")
        if mode == "fixed_time":
            parsed = self._parse_time_value(self.config.get("AUTONOMOUS_PLAN_GENERATION_TIME", "08:05")) or (8, 5)
            due_minute = parsed[0] * 60 + parsed[1]
        else:
            due_minute = (int(self.config.get("SLEEP_END", 8)) * 60 + max(0, int(self.config.get("AUTONOMOUS_PLAN_AFTER_SLEEP_MINUTES", 5)))) % 1440
        elapsed = now.hour * 60 + now.minute - due_minute
        return 0 <= elapsed <= self._PLAN_GENERATION_WINDOW_MINUTES

    @classmethod
    def _schedule_slot_due(cls, now, hour, minute):
        """Only trigger near the configured slot; never replay stale events."""
        scheduled = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        elapsed = (now - scheduled).total_seconds()
        return 0 <= elapsed < cls._SCHEDULE_TRIGGER_GRACE_MINUTES * 60

    def _autonomous_config_fingerprint(self):
        keys = (
            "ENABLE_AUTONOMOUS_DAILY_PLAN", "AUTONOMOUS_ACTIVITY_LEVEL",
            "AUTONOMOUS_PLAN_PROMPT", "AUTONOMOUS_PLAN_GENERATION_MODE",
            "AUTONOMOUS_PLAN_AFTER_SLEEP_MINUTES", "AUTONOMOUS_PLAN_GENERATION_TIME",
            "AUTONOMOUS_PLAN_RETRY_MINUTES", "AUTONOMOUS_PROACTIVE_WINDOW_MINUTES",
            "AUTONOMOUS_REPLY_DAILY_MIN", "AUTONOMOUS_REPLY_DAILY_MAX",
            "AUTONOMOUS_PRIVATE_DAILY_MIN", "AUTONOMOUS_PRIVATE_DAILY_MAX",
            "AUTONOMOUS_DYNAMIC_DAILY_MIN", "AUTONOMOUS_DYNAMIC_DAILY_MAX",
            "AUTONOMOUS_PROACTIVE_DAILY_MIN", "AUTONOMOUS_PROACTIVE_DAILY_MAX",
            "AUTONOMOUS_REPLY_DAILY_LIMIT", "AUTONOMOUS_PRIVATE_DAILY_LIMIT",
            "AUTONOMOUS_DYNAMIC_DAILY_LIMIT", "AUTONOMOUS_PROACTIVE_DAILY_LIMIT",
            "AUTONOMOUS_MIN_ACTION_GAP_MINUTES",
            "ENABLE_REPLY", "ENABLE_PRIVATE_MESSAGES", "ENABLE_PROACTIVE",
            "PROACTIVE_TIMES_COUNT", "ENABLE_DYNAMIC", "DYNAMIC_TIMES_COUNT",
            "DYNAMIC_DAILY_COUNT", "ENABLE_BANGUMI", "BANGUMI_PROACTIVE",
            "BANGUMI_DAILY_LIMIT", "SPECIAL_FOLLOW_TIMES_COUNT",
            "SPECIAL_FOLLOW_ENABLED", "SPECIAL_FOLLOW_MODE",
            "SPECIAL_FOLLOW_FIXED_TIMES", "FIXED_PROACTIVE_WINDOWS", "FIXED_PROACTIVE_TIMES", "FIXED_DYNAMIC_TIMES",
            "FIXED_BANGUMI_TIMES", "FIXED_SPECIAL_FOLLOW_TIMES",
            "ENABLE_DYNAMIC_WATCH", "DYNAMIC_WATCH_TIMES_COUNT", "DYNAMIC_WATCH_DAILY_LIMIT",
            "FIXED_DYNAMIC_WATCH_TIMES", "SLEEP_START", "SLEEP_END",
        )
        payload = {key: self.config.get(key) for key in keys}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def _autonomous_plan_for_today(self):
        if not self.config.get("ENABLE_AUTONOMOUS_DAILY_PLAN", False):
            return {}
        plan = self._load_json(AUTONOMOUS_PLAN_FILE, {})
        if (
            isinstance(plan, dict)
            and plan.get("date") == datetime.now().strftime("%Y-%m-%d")
            and plan.get("config_fingerprint") == self._autonomous_config_fingerprint()
        ):
            return plan
        return {}

    def _plan_time_pairs(self, key):
        plan = self._autonomous_plan_for_today()
        pairs = []
        for value in plan.get(key, []) if isinstance(plan, dict) else []:
            parsed = self._parse_time_value(value)
            if parsed is not None:
                pairs.append(parsed)
        return sorted(set(pairs))

    def _fixed_time_pairs(self, key):
        pairs = []
        for value in self.config.get(key, []) or []:
            parsed = self._parse_time_value(value)
            if parsed is not None and self._is_awake_minute(parsed[0] * 60 + parsed[1]):
                pairs.append(parsed)
        return sorted(set(pairs))

    def _is_awake_minute(self, minute):
        start = int(self.config.get("SLEEP_START", 2)) * 60
        end = int(self.config.get("SLEEP_END", 8)) * 60
        if start == end:
            return True
        sleeping = start <= minute < end if start < end else minute >= start or minute < end
        return not sleeping

    def _schedule_feature_enabled(self, kind):
        """Return whether a schedule type can really execute in the main loop."""
        if kind == "proactive":
            return bool(self.config.get("ENABLE_PROACTIVE", False))
        if kind == "dynamic":
            return bool(self.config.get("ENABLE_DYNAMIC", False))
        if kind == "bangumi":
            return bool(self.config.get("ENABLE_BANGUMI", False) and self.config.get("BANGUMI_PROACTIVE", False))
        if kind == "special_follow":
            return bool(self.config.get("SPECIAL_FOLLOW_ENABLED", False))
        if kind == "dynamic_watch":
            return bool(self.config.get("ENABLE_DYNAMIC_WATCH", False))
        return False

    def _activity_awake_window(self):
        """Use activity as a soft active-time window without bypassing sleep or hard gaps."""
        activity = max(0, min(100, int(self.config.get("AUTONOMOUS_ACTIVITY_LEVEL", 55))))
        if activity < 25:
            return 11 * 60, 20 * 60 + 15
        if activity < 50:
            return 10 * 60, 21 * 60 + 45
        if activity < 75:
            return 9 * 60, 22 * 60 + 30
        return 8 * 60, 23 * 60 + 15

    def _sanitize_autonomous_times(self, values, target, occupied, rng):
        minimum_gap = max(15, int(self.config.get("AUTONOMOUS_MIN_ACTION_GAP_MINUTES", 45)))
        selected = []
        candidates = []
        for raw in values if isinstance(values, list) else []:
            parsed = self._parse_time_value(raw)
            if parsed:
                candidates.append(parsed[0] * 60 + parsed[1])
        active_start, active_end = self._activity_awake_window()
        awake_slots = [minute for minute in range(active_start, active_end, 15) if self._is_awake_minute(minute)]
        rng.shuffle(awake_slots)
        candidates.extend(awake_slots)
        for minute in candidates:
            if len(selected) >= target:
                break
            if not self._is_awake_minute(minute):
                continue
            if any(abs(minute - other) < minimum_gap for other in occupied + selected):
                continue
            selected.append(minute)
        selected.sort()
        occupied.extend(selected)
        return [f"{minute // 60:02d}:{minute % 60:02d}" for minute in selected]

    @staticmethod
    def _extract_plan_json(text):
        raw = str(text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S)
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except Exception:
            match = re.search(r"\{.*\}", raw, re.S)
            if not match:
                return {}
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else {}
            except Exception:
                return {}

    async def _ensure_autonomous_daily_plan(self, force=False):
        """Serialize plan generation so WebUI and the main loop cannot duplicate it."""
        lock = getattr(self, "_autonomous_plan_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._autonomous_plan_lock = lock
        waited_for_inflight = lock.locked()
        async with lock:
            if waited_for_inflight:
                cached = self._load_json(AUTONOMOUS_PLAN_FILE, {})
                if (
                    isinstance(cached, dict)
                    and cached.get("date") == datetime.now().strftime("%Y-%m-%d")
                    and cached.get("config_fingerprint") == self._autonomous_config_fingerprint()
                ):
                    return cached
            return await self._ensure_autonomous_daily_plan_locked(force=force)

    async def _ensure_autonomous_daily_plan_locked(self, force=False):
        """Generate one validated LLM plan per day and clamp it to admin limits."""
        if not self.config.get("ENABLE_AUTONOMOUS_DAILY_PLAN", False):
            return {}
        today = datetime.now().strftime("%Y-%m-%d")
        fingerprint = self._autonomous_config_fingerprint()
        cached = self._load_json(AUTONOMOUS_PLAN_FILE, {})
        cached_matches = isinstance(cached, dict) and cached.get("date") == today and cached.get("config_fingerprint") == fingerprint
        if not force and cached_matches and cached.get("generation_status") != "error":
            return cached
        previous_attempts = 0
        retrying = False
        if not force and cached_matches and cached.get("generation_status") == "error":
            previous_attempts = max(1, int(cached.get("model_attempts", 1) or 1))
            if previous_attempts >= 2 or cached.get("retry_exhausted"):
                return cached
            retry_minutes = max(5, int(self.config.get("AUTONOMOUS_PLAN_RETRY_MINUTES", 15)))
            try:
                generated_at = datetime.strptime(str(cached.get("generated_at")), "%Y-%m-%d %H:%M:%S")
                retry_at = generated_at + timedelta(minutes=retry_minutes)
                retry_deadline = retry_at + timedelta(minutes=self._PLAN_RETRY_GRACE_MINUTES)
                now = datetime.now()
                if now < retry_at:
                    return cached
                if now > retry_deadline:
                    cached["retry_exhausted"] = True
                    cached["model_error"] = (
                        f"{str(cached.get('model_error') or '模型计划生成失败')}；"
                        "已错过唯一一次重试窗口，今天不再自动请求"
                    )[:240]
                    self._save_json(AUTONOMOUS_PLAN_FILE, cached)
                    return cached
                retrying = True
            except (TypeError, ValueError):
                cached["retry_exhausted"] = True
                self._save_json(AUTONOMOUS_PLAN_FILE, cached)
                return cached
        elif not force and not self._autonomous_generation_due():
            return cached if cached_matches else {}

        activity = max(0, min(100, int(self.config.get("AUTONOMOUS_ACTIVITY_LEVEL", 55))))
        proactive_min, proactive_cap = self._autonomous_limit_range("proactive")
        dynamic_min, dynamic_cap = self._autonomous_limit_range("dynamic")
        # Browsing windows and watched-video counts are deliberately separate:
        # PROACTIVE_TIMES_COUNT limits rounds, while PROACTIVE_DAILY_LIMIT is
        # enforced inside the watcher as the all-day video ceiling.
        configured_round_limit = max(0, int(self.config.get("PROACTIVE_TIMES_COUNT", 2) or 0))
        proactive_max = min(configured_round_limit, proactive_cap)
        proactive_max = max(0, proactive_max) if self._schedule_feature_enabled("proactive") else 0
        dynamic_max = max(0, min(
            int(self.config.get("DYNAMIC_TIMES_COUNT", self.config.get("DYNAMIC_DAILY_COUNT", 1))),
            int(self.config.get("DYNAMIC_DAILY_COUNT", 1)),
            dynamic_cap,
        )) if self._schedule_feature_enabled("dynamic") else 0
        proactive_min = min(proactive_min, proactive_max)
        dynamic_min = min(dynamic_min, dynamic_max)
        bangumi_max = max(0, int(self.config.get("BANGUMI_DAILY_LIMIT", 1))) if self._schedule_feature_enabled("bangumi") else 0
        follow_max = max(0, int(self.config.get("SPECIAL_FOLLOW_TIMES_COUNT", 1))) if self._schedule_feature_enabled("special_follow") else 0
        dynamic_watch_max = max(0, int(self.config.get("DYNAMIC_WATCH_TIMES_COUNT", 2))) if self._schedule_feature_enabled("dynamic_watch") else 0
        mood, _ = self._get_today_mood() if hasattr(self, "_get_today_mood") else ("平静", "")
        persona = await self._get_system_prompt() if hasattr(self, "_get_system_prompt") else "自然、克制的B站角色"
        prompt = f"""为一个B站角色安排今天的主动日程。只输出 JSON 对象，不要解释。
日期：{today}
当前心情：{mood}
活跃度：{activity}/100（低时应明显减少事件，高时也不能刷屏）
睡眠区间：{int(self.config.get('SLEEP_START', 2)):02d}:00 到 {int(self.config.get('SLEEP_END', 8)):02d}:00
管理员安全上限：主动浏览最多 {proactive_max} 个时间段，发布动态最多 {dynamic_max} 次；关注动态巡视最多 {dynamic_watch_max} 次，追番最多 {bangumi_max} 次，特别关注最多 {follow_max} 次。所有数量都是上限，不是KPI；没有真实动机时可以填空数组，不要为了凑数安排事件。
相邻主动事件至少间隔 {max(15, int(self.config.get('AUTONOMOUS_MIN_ACTION_GAP_MINUTES', 45)))} 分钟。
人设摘要：{str(persona)[:1200]}
管理员补充：{str(self.config.get('AUTONOMOUS_PLAN_PROMPT', ''))[:800]}
JSON 格式：{{"proactive_windows":["HH:MM-HH:MM"],"dynamic_times":["HH:MM"],"dynamic_watch_times":["HH:MM"],"bangumi_times":["HH:MM"],"special_follow_times":["HH:MM"],"rationale":"一句话说明今日节奏"}}"""
        model_plan = {}
        generation_status = "success"
        model_error = ""
        try:
            raw_plan = await self._llm_call(prompt, max_tokens=600)
            model_plan = self._extract_plan_json(raw_plan)
            if not raw_plan:
                generation_status = "error"
                model_error = str(
                    getattr(self, "_last_llm_error", "")
                    or "模型没有返回计划内容"
                )[:240]
            elif not model_plan:
                generation_status = "error"
                model_error = "模型已返回内容，但没有按要求生成有效的 JSON 日程"
        except Exception as exc:
            generation_status = "error"
            model_error = f"模型调用失败：{exc}"[:240]
            logger.warning(f"[BiliBot] 自主日程生成失败，今天不新增自动事件：{exc}")

        # Activity only narrows the safety ceiling. It never creates a minimum
        # quota, and a failed model call produces no new autonomous events.
        factor = 0.15 + 0.85 * activity / 100

        def activity_cap(minimum, maximum):
            """Translate activity into a target while respecting the admin range."""
            maximum = max(0, int(maximum))
            minimum = max(0, min(int(minimum), maximum))
            if maximum == 0:
                return 0
            return min(maximum, max(minimum, round(maximum * factor)))

        caps = {
            "proactive_times": activity_cap(proactive_min, proactive_max),
            "dynamic_times": activity_cap(dynamic_min, dynamic_max),
            "bangumi_times": activity_cap(0, bangumi_max),
            "special_follow_times": activity_cap(0, follow_max),
            "dynamic_watch_times": activity_cap(0, dynamic_watch_max),
        }

        def target_for(key, minimum, maximum):
            # The model protocol names proactive output ``proactive_windows``;
            # the normalized plan keeps the legacy ``proactive_times`` field as
            # well.  Count the field that the prompt actually asks the model to
            # return, otherwise a valid window plan is silently reduced to zero.
            source_key = "proactive_windows" if key == "proactive_times" else key
            values = model_plan.get(source_key, [])
            soft_maximum = min(maximum, caps.get(key, maximum))
            if isinstance(values, list):
                requested = len(values)
                return min(soft_maximum, requested)
            return soft_maximum
        if not model_plan:
            # 模型失败时不替它编造主动意图；当天安全地保持无新增计划。
            targets = {key: 0 for key in caps}
        else:
            targets = {key: target_for(key, minimum, maximum) for key, minimum, maximum in (
                ("proactive_times", proactive_min, proactive_max), ("dynamic_times", dynamic_min, dynamic_max),
                ("bangumi_times", 0, bangumi_max), ("special_follow_times", 0, follow_max),
                ("dynamic_watch_times", 0, dynamic_watch_max),
            )}
        rng = random.Random(f"{today}|{fingerprint}")
        occupied = []
        normalized = {}
        for key in ("dynamic_times", "dynamic_watch_times", "bangumi_times", "special_follow_times"):
            normalized[key] = self._sanitize_autonomous_times(model_plan.get(key, []), targets[key], occupied, rng)
        requested_windows = model_plan.get("proactive_windows", [])
        window_centers = []
        for raw in requested_windows if isinstance(requested_windows, list) else []:
            parsed = self._parse_window_value(raw)
            if parsed:
                center = (parsed["start_minute"] + parsed["duration_minutes"] // 2) % 1440
                window_centers.append(self._minute_text(center))
        proactive_times = self._sanitize_autonomous_times(window_centers, targets["proactive_times"], occupied, rng)
        window_duration = max(30, min(360, int(self.config.get("AUTONOMOUS_PROACTIVE_WINDOW_MINUTES", 90))))
        active_start, active_end = self._activity_awake_window()
        proactive_windows = []
        normalized_times = []
        for index, time_text in enumerate(proactive_times):
            center_pair = self._parse_time_value(time_text)
            center = center_pair[0] * 60 + center_pair[1]
            start = max(active_start, center - window_duration // 2)
            end = min(active_end, start + window_duration)
            start = max(active_start, end - window_duration)
            trigger = self._window_trigger_minute(start, max(15, end - start), f"{today}|{fingerprint}|{index}")
            proactive_windows.append({"start_time": self._minute_text(start), "end_time": self._minute_text(end), "scheduled_time": self._minute_text(trigger), "trigger_policy": "once_in_window"})
            normalized_times.append(self._minute_text(trigger))
        normalized["proactive_windows"] = proactive_windows
        normalized["proactive_times"] = normalized_times
        _reply_min, reply_max = self._autonomous_limit_range("reply")
        _private_min, private_max = self._autonomous_limit_range("private")
        plan = {
            "date": today,
            "config_fingerprint": fingerprint,
            "activity_level": activity,
            "activity_label": "低迷" if activity < 25 else "平稳" if activity < 50 else "活跃" if activity < 75 else "高能",
            **normalized,
            "reply_cap": reply_max if self.config.get("ENABLE_REPLY", True) else 0,
            "private_cap": private_max if self.config.get("ENABLE_PRIVATE_MESSAGES", False) else 0,
            "rationale": str(model_plan.get("rationale") or (
                "根据今日活跃度生成，并受管理员安全上限与最小间隔保护。"
                if generation_status == "success"
                else "模型计划未生成，今天不新增自动事件。"
            ))[:240],
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generation_status": generation_status,
            "model_error": model_error,
            "source": "model" if generation_status == "success" else "none",
            "model_attempts": previous_attempts + 1,
            "retry_exhausted": bool(generation_status == "error" and retrying),
        }
        if generation_status == "error" and retrying:
            plan["model_error"] = (
                f"{model_error or '模型计划生成失败'}；唯一一次重试仍失败，今天不再自动请求"
            )[:240]
        self._save_json(AUTONOMOUS_PLAN_FILE, plan)
        # Replace runtime schedule state immediately so WebUI regeneration and the
        # current main-loop iteration both see the new plan.
        self._proactive_windows = list(plan.get("proactive_windows", []))
        self._proactive_times = [parsed for value in plan["proactive_times"] if (parsed := self._parse_time_value(value)) is not None]
        self._dynamic_times = [parsed for value in plan["dynamic_times"] if (parsed := self._parse_time_value(value)) is not None]
        self._bangumi_times = [parsed for value in plan["bangumi_times"] if (parsed := self._parse_time_value(value)) is not None]
        self._special_follow_times = [parsed for value in plan["special_follow_times"] if (parsed := self._parse_time_value(value)) is not None]
        self._dynamic_watch_times = [parsed for value in plan["dynamic_watch_times"] if (parsed := self._parse_time_value(value)) is not None]
        self._proactive_triggered = set()
        self._dynamic_triggered = set()
        self._bangumi_triggered = set()
        self._special_follow_triggered = set()
        self._dynamic_watch_triggered = set()
        self._bangumi_update_checked = False
        self._save_schedule_state(self._proactive_times, self._proactive_triggered)
        self._save_dynamic_schedule_state(self._dynamic_times, self._dynamic_triggered)
        self._save_bangumi_schedule_state(self._bangumi_times, self._bangumi_triggered, False)
        self._save_special_follow_schedule_state(self._special_follow_times, self._special_follow_triggered)
        self._save_dynamic_watch_schedule_state(self._dynamic_watch_times, self._dynamic_watch_triggered)
        logger.info(f"[BiliBot] 自主日程已生成：{plan['activity_label']} | {plan['rationale']}")
        return plan

    # ── 主动视频调度 ──
    def _generate_daily_schedule(self):
        if not self._schedule_feature_enabled("proactive"):
            self._proactive_windows = []
            self._save_schedule_state([], set())
            return [], set()
        plan = self._autonomous_plan_for_today()
        if self.config.get("ENABLE_AUTONOMOUS_DAILY_PLAN", False):
            planned = self._plan_time_pairs("proactive_times")
            self._proactive_windows = list(plan.get("proactive_windows", [])) if plan else []
            self._save_schedule_state(planned, set())
            return planned, set()
        configured_round_limit = max(0, int(self.config.get("PROACTIVE_TIMES_COUNT", 2) or 0))
        round_cap = min(configured_round_limit, self._autonomous_limit_max("proactive"))
        windows = self._fixed_window_entries()[:round_cap]
        self._proactive_windows = [{key: item[key] for key in ("start_time", "end_time", "scheduled_time")} | {"trigger_policy": "once_in_window"} for item in windows]
        times = [self._parse_time_value(item["scheduled_time"]) for item in windows]
        times = [item for item in times if item is not None]
        self._save_schedule_state(times, set())
        return times, set()

    def _load_or_generate_schedule(self):
        if not self._schedule_feature_enabled("proactive"):
            self._proactive_windows = []
            self._save_schedule_state([], set())
            return [], set()
        schedule = self._load_json(SCHEDULE_FILE, {})
        if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
            pairs = [self._parse_time_value(value) for value in schedule.get("proactive_times", [])]
            self._proactive_windows = list(schedule.get("proactive_windows", []))
            return [value for value in pairs if value is not None], set(schedule.get("proactive_triggered", []))
        return self._generate_daily_schedule()

    def _save_schedule_state(self, times, triggered):
        schedule = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "proactive_times": self._format_time_pairs(times),
            "proactive_windows": list(getattr(self, "_proactive_windows", [])),
            "proactive_triggered": sorted(triggered),
        }
        self._save_json(SCHEDULE_FILE, schedule)

    # ── 动态调度 ──
    def _generate_dynamic_schedule(self):
        if not self._schedule_feature_enabled("dynamic"):
            self._save_dynamic_schedule_state([], set())
            return [], set()
        planned = self._plan_time_pairs("dynamic_times")
        if self.config.get("ENABLE_AUTONOMOUS_DAILY_PLAN", False):
            self._save_dynamic_schedule_state(planned, set())
            return planned, set()
        fixed = self._fixed_time_pairs("FIXED_DYNAMIC_TIMES")
        if fixed:
            times = fixed
        else:
            n_times = self.config.get("DYNAMIC_TIMES_COUNT", 1)
            times = sorted(random.sample(range(10, 23), min(n_times, 12)))
            times = [(h, random.randint(0, 59)) for h in times]
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "dynamic_times": [f"{h}:{m:02d}" for h, m in times], "dynamic_triggered": []}
        self._save_json(DYNAMIC_SCHEDULE_FILE, schedule)
        return times, set()

    def _load_or_generate_dynamic_schedule(self):
        if not self._schedule_feature_enabled("dynamic"):
            self._save_dynamic_schedule_state([], set())
            return [], set()
        try:
            schedule = self._load_json(DYNAMIC_SCHEDULE_FILE, {})
            if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
                times = []
                for t in schedule.get("dynamic_times", []):
                    h, m = t.split(":")
                    times.append((int(h), int(m)))
                triggered = set(schedule.get("dynamic_triggered", []))
                return times, triggered
        except Exception:
            pass
        return self._generate_dynamic_schedule()

    def _save_dynamic_schedule_state(self, times, triggered):
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "dynamic_times": [f"{h}:{m:02d}" for h, m in times], "dynamic_triggered": list(triggered)}
        self._save_json(DYNAMIC_SCHEDULE_FILE, schedule)

    # ── 番剧调度 ──
    def _generate_bangumi_schedule(self):
        if not self._schedule_feature_enabled("bangumi"):
            self._save_bangumi_schedule_state([], set(), False)
            return [], set(), False
        planned = self._plan_time_pairs("bangumi_times")
        if self.config.get("ENABLE_AUTONOMOUS_DAILY_PLAN", False):
            self._save_bangumi_schedule_state(planned, set(), False)
            return planned, set(), False
        fixed = self._fixed_time_pairs("FIXED_BANGUMI_TIMES")
        if fixed:
            times = fixed
        else:
            n_times = self.config.get("BANGUMI_DAILY_LIMIT", 1)
            available_hours = list(range(10, 23))
            n_times = min(n_times, len(available_hours))
            times = sorted(random.sample(available_hours, n_times))
            times = [(h, random.randint(0, 59)) for h in times]
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "bangumi_times": [f"{h}:{m:02d}" for h, m in times], "bangumi_triggered": [], "update_checked": False}
        self._save_json(BANGUMI_SCHEDULE_FILE, schedule)
        return times, set(), False

    def _load_or_generate_bangumi_schedule(self):
        if not self._schedule_feature_enabled("bangumi"):
            self._save_bangumi_schedule_state([], set(), False)
            return [], set(), False
        try:
            schedule = self._load_json(BANGUMI_SCHEDULE_FILE, {})
            if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
                times = []
                for t in schedule.get("bangumi_times", []):
                    h, m = t.split(":")
                    times.append((int(h), int(m)))
                triggered = set(schedule.get("bangumi_triggered", []))
                update_checked = schedule.get("update_checked", False)
                return times, triggered, update_checked
        except Exception:
            pass
        return self._generate_bangumi_schedule()

    def _save_bangumi_schedule_state(self, times, triggered, update_checked=False):
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "bangumi_times": [f"{h}:{m:02d}" for h, m in times], "bangumi_triggered": list(triggered), "update_checked": update_checked}
        self._save_json(BANGUMI_SCHEDULE_FILE, schedule)

    # ── 特别关注调度 ──
    def _get_special_follow_config_fingerprint(self):
        """当前特关配置的指纹，用于检测配置变更。"""
        plan = self._autonomous_plan_for_today()
        if plan:
            return f"autonomous|{plan.get('config_fingerprint', '')}"
        fixed = self.config.get("FIXED_SPECIAL_FOLLOW_TIMES", [])
        return f"fixed-plan|{','.join(str(t) for t in fixed)}"

    def _generate_special_follow_schedule(self):
        if not self._schedule_feature_enabled("special_follow"):
            self._save_special_follow_schedule_state([], set())
            return [], set()
        planned = self._plan_time_pairs("special_follow_times")
        if self.config.get("ENABLE_AUTONOMOUS_DAILY_PLAN", False):
            self._save_special_follow_schedule_state(planned, set())
            return planned, set()
        # 固定计划模式统一从“当天计划生成方式”板块读取，不再使用能力抽屉中的旧触发方式/次数。
        times = self._fixed_time_pairs("FIXED_SPECIAL_FOLLOW_TIMES")
        schedule = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "special_follow_times": [f"{h}:{m:02d}" for h, m in times],
            "special_follow_triggered": [],
            "config_fingerprint": self._get_special_follow_config_fingerprint(),
        }
        self._save_json(SPECIAL_FOLLOW_SCHEDULE_FILE, schedule)
        logger.info(f"[BiliBot] ⭐ 特关计划已生成：{[f'{h}:{m:02d}' for h, m in times]}")
        return times, set()

    def _load_or_generate_special_follow_schedule(self):
        if not self._schedule_feature_enabled("special_follow"):
            self._save_special_follow_schedule_state([], set())
            return [], set()
        try:
            schedule = self._load_json(SPECIAL_FOLLOW_SCHEDULE_FILE, {})
            if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
                # 配置变了就重新生成
                if schedule.get("config_fingerprint") != self._get_special_follow_config_fingerprint():
                    logger.info("[BiliBot] ⭐ 检测到特关配置变更，重新生成计划")
                    return self._generate_special_follow_schedule()
                times = []
                for t in schedule.get("special_follow_times", []):
                    h, m = t.split(":")
                    times.append((int(h), int(m)))
                triggered = set(schedule.get("special_follow_triggered", []))
                return times, triggered
        except Exception:
            pass
        return self._generate_special_follow_schedule()

    def _save_special_follow_schedule_state(self, times, triggered):
        schedule = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "special_follow_times": [f"{h}:{m:02d}" for h, m in times],
            "special_follow_triggered": list(triggered),
            "config_fingerprint": self._get_special_follow_config_fingerprint(),
        }
        self._save_json(SPECIAL_FOLLOW_SCHEDULE_FILE, schedule)

    # ── 关注动态巡视调度 ──
    def _generate_dynamic_watch_schedule(self):
        if not self._schedule_feature_enabled("dynamic_watch"):
            self._save_dynamic_watch_schedule_state([], set())
            return [], set()
        planned = self._plan_time_pairs("dynamic_watch_times")
        if self.config.get("ENABLE_AUTONOMOUS_DAILY_PLAN", False):
            self._save_dynamic_watch_schedule_state(planned, set())
            return planned, set()
        fixed = self._fixed_time_pairs("FIXED_DYNAMIC_WATCH_TIMES")
        if fixed:
            times = fixed
        else:
            count = max(0, min(12, int(self.config.get("DYNAMIC_WATCH_TIMES_COUNT", 2))))
            hours = sorted(random.sample(range(9, 23), min(count, 14)))
            times = [(hour, random.randint(0, 59)) for hour in hours]
        self._save_dynamic_watch_schedule_state(times, set())
        return times, set()

    def _load_or_generate_dynamic_watch_schedule(self):
        if not self._schedule_feature_enabled("dynamic_watch"):
            self._save_dynamic_watch_schedule_state([], set())
            return [], set()
        schedule = self._load_json(DYNAMIC_WATCH_SCHEDULE_FILE, {})
        if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
            pairs = [self._parse_time_value(value) for value in schedule.get("dynamic_watch_times", [])]
            return [value for value in pairs if value is not None], set(schedule.get("dynamic_watch_triggered", []))
        return self._generate_dynamic_watch_schedule()

    def _save_dynamic_watch_schedule_state(self, times, triggered):
        self._save_json(DYNAMIC_WATCH_SCHEDULE_FILE, {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "dynamic_watch_times": self._format_time_pairs(times),
            "dynamic_watch_triggered": sorted(triggered),
        })

    # ── 通用工具 ──
    @staticmethod
    def _format_time_pairs(times):
        return [f"{h}:{m:02d}" for h, m in times]

    def _ensure_today_schedules(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._schedule_feature_enabled("proactive"):
            sched = self._load_json(SCHEDULE_FILE, {})
            if sched.get("date") != today or not self._proactive_times:
                self._proactive_times, self._proactive_triggered = self._load_or_generate_schedule()
        else:
            self._proactive_times, self._proactive_triggered = [], set()

        if self._schedule_feature_enabled("dynamic"):
            dsched = self._load_json(DYNAMIC_SCHEDULE_FILE, {})
            if dsched.get("date") != today or not self._dynamic_times:
                self._dynamic_times, self._dynamic_triggered = self._load_or_generate_dynamic_schedule()
        else:
            self._dynamic_times, self._dynamic_triggered = [], set()

        if self._schedule_feature_enabled("bangumi"):
            bsched = self._load_json(BANGUMI_SCHEDULE_FILE, {})
            if bsched.get("date") != today or not getattr(self, "_bangumi_times", None):
                self._bangumi_times, self._bangumi_triggered, self._bangumi_update_checked = self._load_or_generate_bangumi_schedule()
        else:
            self._bangumi_times, self._bangumi_triggered, self._bangumi_update_checked = [], set(), False

        if self._schedule_feature_enabled("special_follow"):
            sfsched = self._load_json(SPECIAL_FOLLOW_SCHEDULE_FILE, {})
            if sfsched.get("date") != today or not getattr(self, "_special_follow_times", None):
                self._special_follow_times, self._special_follow_triggered = self._load_or_generate_special_follow_schedule()
        else:
            self._special_follow_times, self._special_follow_triggered = [], set()

        if self._schedule_feature_enabled("dynamic_watch"):
            dwsched = self._load_json(DYNAMIC_WATCH_SCHEDULE_FILE, {})
            if dwsched.get("date") != today or not getattr(self, "_dynamic_watch_times", None):
                self._dynamic_watch_times, self._dynamic_watch_triggered = self._load_or_generate_dynamic_watch_schedule()
        else:
            self._dynamic_watch_times, self._dynamic_watch_triggered = [], set()

    def _get_schedule_snapshot(self):
        self._ensure_today_schedules()
        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "proactive_times": self._format_time_pairs(self._proactive_times) if self._schedule_feature_enabled("proactive") else [],
            "proactive_windows": list(getattr(self, "_proactive_windows", [])) if self._schedule_feature_enabled("proactive") else [],
            "proactive_triggered": sorted(self._proactive_triggered) if self._schedule_feature_enabled("proactive") else [],
            "dynamic_times": self._format_time_pairs(self._dynamic_times) if self._schedule_feature_enabled("dynamic") else [],
            "dynamic_triggered": sorted(self._dynamic_triggered) if self._schedule_feature_enabled("dynamic") else [],
            "bangumi_times": self._format_time_pairs(getattr(self, "_bangumi_times", [])) if self._schedule_feature_enabled("bangumi") else [],
            "bangumi_triggered": sorted(getattr(self, "_bangumi_triggered", set())) if self._schedule_feature_enabled("bangumi") else [],
            "special_follow_times": self._format_time_pairs(getattr(self, "_special_follow_times", [])) if self._schedule_feature_enabled("special_follow") else [],
            "special_follow_triggered": sorted(getattr(self, "_special_follow_triggered", set())) if self._schedule_feature_enabled("special_follow") else [],
            "dynamic_watch_times": self._format_time_pairs(getattr(self, "_dynamic_watch_times", [])) if self._schedule_feature_enabled("dynamic_watch") else [],
            "dynamic_watch_triggered": sorted(getattr(self, "_dynamic_watch_triggered", set())) if self._schedule_feature_enabled("dynamic_watch") else [],
        }

    def _mark_overdue_schedule_as_triggered_on_startup(self):
        now_dt = datetime.now()
        changed = False
        self._ensure_today_schedules()
        proactive_overdue = {f"{h}:{m:02d}" for h, m in self._proactive_times if (now_dt.hour > h or (now_dt.hour == h and now_dt.minute > m))}
        overdue_to_add = proactive_overdue - self._proactive_triggered
        if overdue_to_add:
            self._proactive_triggered.update(overdue_to_add)
            self._save_schedule_state(self._proactive_times, self._proactive_triggered)
            changed = True
            logger.info(f"[BiliBot] 启动时跳过已过期的主动视频计划：{sorted(overdue_to_add)}")
        dynamic_overdue = {f"{h}:{m:02d}" for h, m in self._dynamic_times if (now_dt.hour > h or (now_dt.hour == h and now_dt.minute > m))}
        overdue_dynamic_to_add = dynamic_overdue - self._dynamic_triggered
        if overdue_dynamic_to_add:
            self._dynamic_triggered.update(overdue_dynamic_to_add)
            self._save_dynamic_schedule_state(self._dynamic_times, self._dynamic_triggered)
            changed = True
            logger.info(f"[BiliBot] 启动时跳过已过期的动态计划：{sorted(overdue_dynamic_to_add)}")
        bangumi_times = getattr(self, '_bangumi_times', [])
        bangumi_triggered = getattr(self, '_bangumi_triggered', set())
        bangumi_overdue = {f"{h}:{m:02d}" for h, m in bangumi_times if (now_dt.hour > h or (now_dt.hour == h and now_dt.minute > m))}
        overdue_bangumi_to_add = bangumi_overdue - bangumi_triggered
        if overdue_bangumi_to_add:
            bangumi_triggered.update(overdue_bangumi_to_add)
            self._bangumi_triggered = bangumi_triggered
            self._save_bangumi_schedule_state(bangumi_times, bangumi_triggered, getattr(self, '_bangumi_update_checked', False))
            changed = True
            logger.info(f"[BiliBot] 启动时跳过已过期的番剧计划：{sorted(overdue_bangumi_to_add)}")
        sf_times = getattr(self, '_special_follow_times', [])
        sf_triggered = getattr(self, '_special_follow_triggered', set())
        sf_overdue = {f"{h}:{m:02d}" for h, m in sf_times if (now_dt.hour > h or (now_dt.hour == h and now_dt.minute > m))}
        overdue_sf_to_add = sf_overdue - sf_triggered
        if overdue_sf_to_add:
            sf_triggered.update(overdue_sf_to_add)
            self._special_follow_triggered = sf_triggered
            self._save_special_follow_schedule_state(sf_times, sf_triggered)
            changed = True
            logger.info(f"[BiliBot] 启动时跳过已过期的特关计划：{sorted(overdue_sf_to_add)}")
        dynamic_watch_times = getattr(self, "_dynamic_watch_times", [])
        dynamic_watch_triggered = getattr(self, "_dynamic_watch_triggered", set())
        dynamic_watch_overdue = {
            f"{h}:{m:02d}"
            for h, m in dynamic_watch_times
            if now_dt.hour > h or (now_dt.hour == h and now_dt.minute > m)
        }
        overdue_dynamic_watch_to_add = dynamic_watch_overdue - dynamic_watch_triggered
        if overdue_dynamic_watch_to_add:
            dynamic_watch_triggered.update(overdue_dynamic_watch_to_add)
            self._dynamic_watch_triggered = dynamic_watch_triggered
            self._save_dynamic_watch_schedule_state(dynamic_watch_times, dynamic_watch_triggered)
            changed = True
            logger.info(f"[BiliBot] 启动时跳过已过期的关注动态巡视计划：{sorted(overdue_dynamic_watch_to_add)}")
        if not changed:
            logger.debug(f"[BiliBot] 启动时无需跳过过期计划（{now_dt.strftime('%Y-%m-%d')}）")
