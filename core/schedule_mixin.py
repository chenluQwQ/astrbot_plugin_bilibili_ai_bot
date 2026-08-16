"""定时任务调度：主动视频和动态发布的时间管理。"""
import hashlib
import json
import random
import re
from datetime import datetime
from astrbot.api import logger
from .config import (
    AUTONOMOUS_PLAN_FILE, BANGUMI_SCHEDULE_FILE, DYNAMIC_SCHEDULE_FILE,
    DYNAMIC_WATCH_SCHEDULE_FILE, SCHEDULE_FILE, SPECIAL_FOLLOW_SCHEDULE_FILE,
)


class ScheduleMixin:
    """日程管理。"""

    _AUTONOMOUS_LIMITS = {
        "reply": ("AUTONOMOUS_REPLY_DAILY_MIN", "AUTONOMOUS_REPLY_DAILY_MAX", "AUTONOMOUS_REPLY_DAILY_LIMIT", 80),
        "private": ("AUTONOMOUS_PRIVATE_DAILY_MIN", "AUTONOMOUS_PRIVATE_DAILY_MAX", "AUTONOMOUS_PRIVATE_DAILY_LIMIT", 30),
        "dynamic": ("AUTONOMOUS_DYNAMIC_DAILY_MIN", "AUTONOMOUS_DYNAMIC_DAILY_MAX", "AUTONOMOUS_DYNAMIC_DAILY_LIMIT", 2),
        "proactive": ("AUTONOMOUS_PROACTIVE_DAILY_MIN", "AUTONOMOUS_PROACTIVE_DAILY_MAX", "AUTONOMOUS_PROACTIVE_DAILY_LIMIT", 4),
    }

    def _autonomous_limit_range(self, kind):
        """Return the configured (minimum, maximum) for an autonomous quota.

        The old ``*_DAILY_LIMIT`` keys remain readable for existing installs.
        If a user has customized an old key and the new max key is still at its
        schema default, the old value is treated as the migrated maximum.
        """
        min_key, max_key, legacy_key, default_max = self._AUTONOMOUS_LIMITS[kind]
        minimum = self.config.get(min_key, 0)
        maximum = self.config.get(max_key, None)
        legacy = self.config.get(legacy_key, default_max)
        try:
            minimum = max(0, int(minimum or 0))
        except (TypeError, ValueError):
            minimum = 0
        try:
            maximum = int(maximum) if maximum is not None else int(legacy or default_max)
        except (TypeError, ValueError):
            maximum = int(legacy or default_max)
        try:
            legacy = int(legacy or default_max)
        except (TypeError, ValueError):
            legacy = int(default_max)
        if maximum == int(default_max) and legacy != int(default_max):
            maximum = legacy
        maximum = max(minimum, maximum, 0)
        return minimum, maximum

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

    def _autonomous_config_fingerprint(self):
        keys = (
            "ENABLE_AUTONOMOUS_DAILY_PLAN", "AUTONOMOUS_ACTIVITY_LEVEL",
            "AUTONOMOUS_PLAN_PROMPT",
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
            "SPECIAL_FOLLOW_FIXED_TIMES", "FIXED_PROACTIVE_TIMES", "FIXED_DYNAMIC_TIMES",
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
        """Generate one validated LLM plan per day and clamp it to admin limits."""
        if not self.config.get("ENABLE_AUTONOMOUS_DAILY_PLAN", False):
            return {}
        today = datetime.now().strftime("%Y-%m-%d")
        fingerprint = self._autonomous_config_fingerprint()
        cached = self._load_json(AUTONOMOUS_PLAN_FILE, {})
        if not force and isinstance(cached, dict) and cached.get("date") == today and cached.get("config_fingerprint") == fingerprint:
            return cached

        activity = max(0, min(100, int(self.config.get("AUTONOMOUS_ACTIVITY_LEVEL", 55))))
        proactive_min, proactive_cap = self._autonomous_limit_range("proactive")
        dynamic_min, dynamic_cap = self._autonomous_limit_range("dynamic")
        proactive_max = max(0, min(
            int(self.config.get("PROACTIVE_DAILY_LIMIT", 5)),
            proactive_cap,
        )) if self._schedule_feature_enabled("proactive") else 0
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
管理员范围：主动浏览 {proactive_min}-{proactive_max} 次，发布动态 {dynamic_min}-{dynamic_max} 次；关注动态巡视最多 {dynamic_watch_max} 次，追番最多 {bangumi_max} 次，特别关注最多 {follow_max} 次。下限只在对应能力已启用且当天条件允许时尽量满足。
相邻主动事件至少间隔 {max(15, int(self.config.get('AUTONOMOUS_MIN_ACTION_GAP_MINUTES', 45)))} 分钟。
人设摘要：{str(persona)[:1200]}
管理员补充：{str(self.config.get('AUTONOMOUS_PLAN_PROMPT', ''))[:800]}
JSON 格式：{{"proactive_times":["HH:MM"],"dynamic_times":["HH:MM"],"dynamic_watch_times":["HH:MM"],"bangumi_times":["HH:MM"],"special_follow_times":["HH:MM"],"rationale":"一句话说明今日节奏"}}"""
        model_plan = {}
        generation_status = "success"
        model_error = ""
        try:
            raw_plan = await self._llm_call(prompt, max_tokens=600)
            model_plan = self._extract_plan_json(raw_plan)
            if not raw_plan or not model_plan:
                generation_status = "error"
                model_error = "模型调用失败、未配置模型提供商，或模型未返回有效计划"
        except Exception as exc:
            generation_status = "error"
            model_error = f"模型调用失败：{exc}"[:240]
            logger.warning(f"[BiliBot] 自主日程生成失败，使用安全回退：{exc}")

        # Model may choose fewer actions. If it returns no usable count, use an
        # activity-scaled deterministic fallback rather than always filling max.
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
            values = model_plan.get(key, [])
            soft_maximum = min(maximum, caps.get(key, maximum))
            if isinstance(values, list):
                # The model may request fewer events, but it cannot bypass an
                # administrator-configured lower bound when the feature is on.
                requested = max(minimum, len(values))
                return min(soft_maximum, requested)
            return soft_maximum
        if not model_plan:
            targets = dict(caps)
        else:
            targets = {key: target_for(key, minimum, maximum) for key, minimum, maximum in (
                ("proactive_times", proactive_min, proactive_max), ("dynamic_times", dynamic_min, dynamic_max),
                ("bangumi_times", 0, bangumi_max), ("special_follow_times", 0, follow_max),
                ("dynamic_watch_times", 0, dynamic_watch_max),
            )}
        rng = random.Random(f"{today}|{fingerprint}")
        occupied = []
        normalized = {}
        for key in ("dynamic_times", "proactive_times", "dynamic_watch_times", "bangumi_times", "special_follow_times"):
            normalized[key] = self._sanitize_autonomous_times(model_plan.get(key, []), targets[key], occupied, rng)
        reply_min, reply_max = self._autonomous_limit_range("reply")
        private_min, private_max = self._autonomous_limit_range("private")
        plan = {
            "date": today,
            "config_fingerprint": fingerprint,
            "activity_level": activity,
            "activity_label": "低迷" if activity < 25 else "平稳" if activity < 50 else "活跃" if activity < 75 else "高能",
            **normalized,
            "reply_target": max(reply_min, min(reply_max, round(reply_min + (reply_max - reply_min) * factor))) if self.config.get("ENABLE_REPLY", True) else 0,
            "private_target": max(private_min, min(private_max, round(private_min + (private_max - private_min) * factor))) if self.config.get("ENABLE_PRIVATE_MESSAGES", False) else 0,
            "rationale": str(model_plan.get("rationale") or "根据今日活跃度生成，并受管理员范围与最小间隔保护。")[:240],
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "generation_status": generation_status,
            "model_error": model_error,
            "source": "model" if generation_status == "success" else "fallback",
        }
        self._save_json(AUTONOMOUS_PLAN_FILE, plan)
        # Replace runtime schedule state immediately so WebUI regeneration and the
        # current main-loop iteration both see the new plan.
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
            self._save_schedule_state([], set())
            return [], set()
        planned = self._plan_time_pairs("proactive_times")
        if planned or self._autonomous_plan_for_today():
            self._save_schedule_state(planned, set())
            return planned, set()
        fixed = self._fixed_time_pairs("FIXED_PROACTIVE_TIMES")
        if fixed:
            times = fixed
        else:
            n_times = self.config.get("PROACTIVE_TIMES_COUNT", 2)
            times = sorted(random.sample(range(10, 23), min(n_times, 12)))
            times = [(h, random.randint(0, 59)) for h in times]
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "proactive_times": [f"{h}:{m:02d}" for h, m in times], "proactive_triggered": []}
        self._save_json(SCHEDULE_FILE, schedule)
        return times, set()

    def _load_or_generate_schedule(self):
        if not self._schedule_feature_enabled("proactive"):
            self._save_schedule_state([], set())
            return [], set()
        try:
            schedule = self._load_json(SCHEDULE_FILE, {})
            if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
                times = []
                for t in schedule.get("proactive_times", []):
                    h, m = t.split(":")
                    times.append((int(h), int(m)))
                triggered = set(schedule.get("proactive_triggered", []))
                return times, triggered
        except Exception:
            pass
        return self._generate_daily_schedule()

    def _save_schedule_state(self, times, triggered):
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "proactive_times": [f"{h}:{m:02d}" for h, m in times], "proactive_triggered": list(triggered)}
        self._save_json(SCHEDULE_FILE, schedule)

    # ── 动态调度 ──
    def _generate_dynamic_schedule(self):
        if not self._schedule_feature_enabled("dynamic"):
            self._save_dynamic_schedule_state([], set())
            return [], set()
        planned = self._plan_time_pairs("dynamic_times")
        if planned or self._autonomous_plan_for_today():
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
        if planned or self._autonomous_plan_for_today():
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
        mode = self.config.get("SPECIAL_FOLLOW_MODE", "random")
        count = self.config.get("SPECIAL_FOLLOW_TIMES_COUNT", 1)
        fixed = self.config.get("SPECIAL_FOLLOW_FIXED_TIMES", [])
        return f"{mode}|{count}|{','.join(str(t) for t in fixed)}"

    def _generate_special_follow_schedule(self):
        if not self._schedule_feature_enabled("special_follow"):
            self._save_special_follow_schedule_state([], set())
            return [], set()
        planned = self._plan_time_pairs("special_follow_times")
        if planned or self._autonomous_plan_for_today():
            self._save_special_follow_schedule_state(planned, set())
            return planned, set()
        mode = self.config.get("SPECIAL_FOLLOW_MODE", "random").lower().strip()
        if mode == "fixed":
            # 固定时间模式：从配置读 HH:MM 列表
            fixed = self.config.get("SPECIAL_FOLLOW_FIXED_TIMES", [])
            times = []
            for t in fixed:
                try:
                    parts = str(t).split(":")
                    times.append((int(parts[0]), int(parts[1])))
                except (ValueError, IndexError):
                    pass
            times.sort()
        else:
            # 随机时间模式
            n_times = self.config.get("SPECIAL_FOLLOW_TIMES_COUNT", 1)
            hours = sorted(random.sample(range(10, 23), min(n_times, 12)))
            times = [(h, random.randint(0, 59)) for h in hours]
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
        if planned or self._autonomous_plan_for_today():
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
