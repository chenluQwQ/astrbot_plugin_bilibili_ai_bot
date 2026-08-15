"""个性层：生命状态、四维关系、自主作息调度。

对应 issue #6。Bot 以「真实用户」模式运行，有自己的能量、情绪、作息周期。
适配现有 schema（life_states 和 day_plans 表）。

核心设计：

1. **生命状态快照**（life_states 表）：记录 energy、mood、social 等状态。
2. **四维关系**（profiles 表）：familiarity、trust、warmth、conflict 动态更新。
3. **自主作息**（day_plans 表）：bot 根据能量预算排当天活动时段。
4. **主动调度**：在 active/social 时段可能主动互动；rest 时段拒绝非紧急互动。

配置项：
- PERSONA_ENERGY_DECAY_PER_HOUR：每小时能量自然消耗（0~100 范围）
- PERSONA_ENERGY_COST_REPLY：回复一次消耗多少能量
- PERSONA_SLEEP_START：默认休息时段开始（例如 "23:00"）
- PERSONA_SLEEP_END：默认休息时段结束（例如 "07:00"）
- PERSONA_TOKEN_BUDGET_DAILY：每日 token 预算
- PERSONA_PROACTIVE_ENABLED：是否允许主动行为
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, time

from ..storage import Database, now


@dataclass
class LifeState:
    """生命状态快照（适配现有 schema）。"""

    id: int
    at: float  # 时间戳
    mood: str  # calm/happy/tired/frustrated 等
    energy: float  # 0~1
    social: float  # 0~1，社交饱和度
    phase: str  # idle/active/rest/focus 等当前时段
    note: str  # 备注
    interests: dict  # JSON 对象
    budget: dict  # JSON 对象（token 等）


@dataclass
class DayPlanSegment:
    """当日计划的一个时段（适配现有 schema）。"""

    id: int
    plan_date: str  # YYYY-MM-DD
    start_min: int  # 当天第几分钟（0-1439）
    end_min: int  # 结束分钟
    activity: str  # rest/browse/reply/watch/create/social/chores
    intent: str  # 意图描述
    energy_cost: float  # 预计能量消耗
    planned_by: str  # bot/user
    state: str  # planned/active/done/skipped
    summary: str  # 完成后的总结
    stats: dict  # JSON 对象
    created_at: float  # 时间戳


class PersonaEngine:
    """个性层引擎（适配现有 schema）。"""

    def __init__(self, db: Database, config_getter) -> None:
        self._db = db
        self._get = config_getter

    # ------------------------------------------------------------ 配置
    def _energy_decay_per_hour(self) -> float:
        return float(self._get("PERSONA_ENERGY_DECAY_PER_HOUR", 5.0))

    def _energy_cost_reply(self) -> float:
        return float(self._get("PERSONA_ENERGY_COST_REPLY", 2.0))

    def _sleep_start(self) -> time:
        s = str(self._get("PERSONA_SLEEP_START", "23:00"))
        h, m = map(int, s.split(":"))
        return time(h, m)

    def _sleep_end(self) -> time:
        s = str(self._get("PERSONA_SLEEP_END", "07:00"))
        h, m = map(int, s.split(":"))
        return time(h, m)

    def _token_budget_daily(self) -> int:
        return int(self._get("PERSONA_TOKEN_BUDGET_DAILY", 20000))

    def _proactive_enabled(self) -> bool:
        return bool(self._get("PERSONA_PROACTIVE_ENABLED", True))

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    # ------------------------------------------------------------ 状态快照
    async def snapshot(self) -> LifeState:
        """取最新状态快照。没有则初始化。"""
        row = await self._db.fetch_one(
            "SELECT * FROM life_states ORDER BY at DESC LIMIT 1"
        )
        if row:
            return LifeState(
                id=row["id"],
                at=row["at"],
                mood=row["mood"],
                energy=row["energy"],
                social=row["social"],
                phase=row["phase"],
                note=row["note"],
                interests=json.loads(row["interests"]),
                budget=json.loads(row["budget"]),
            )
        # 初始化
        return await self._init_state()

    async def _init_state(self) -> LifeState:
        state_id = await self._db.execute(
            "INSERT INTO life_states(at,mood,energy,social,phase,note,interests,budget) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                now(),
                "calm",
                0.8,
                0.3,
                "idle",
                "初始化",
                json.dumps({}),
                json.dumps({"tokens_daily": self._token_budget_daily()}),
            ),
        )
        return LifeState(
            id=state_id,
            at=now(),
            mood="calm",
            energy=0.8,
            social=0.3,
            phase="idle",
            note="初始化",
            interests={},
            budget={"tokens_daily": self._token_budget_daily()},
        )

    async def update_state(
        self,
        energy_delta: float = 0.0,
        social_delta: float = 0.0,
        mood: str | None = None,
        phase: str | None = None,
        note: str = "",
    ) -> LifeState:
        """增量更新状态并记录快照。"""
        prev = await self.snapshot()
        new_energy = max(0.0, min(1.0, prev.energy + energy_delta))
        new_social = max(0.0, min(1.0, prev.social + social_delta))
        new_mood = mood if mood else prev.mood
        new_phase = phase if phase else prev.phase

        # 能量过低强制休息
        if new_energy < 0.2 and new_phase not in ("rest", "idle"):
            new_phase = "rest"
            note = note or "能量耗尽，强制休息"

        state_id = await self._db.execute(
            "INSERT INTO life_states(at,mood,energy,social,phase,note,interests,budget) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                now(),
                new_mood,
                new_energy,
                new_social,
                new_phase,
                note,
                json.dumps(prev.interests),
                json.dumps(prev.budget),
            ),
        )
        return LifeState(
            id=state_id,
            at=now(),
            mood=new_mood,
            energy=new_energy,
            social=new_social,
            phase=new_phase,
            note=note,
            interests=prev.interests,
            budget=prev.budget,
        )

    async def decay(self, hours: float) -> None:
        """自然能量衰减。"""
        decay_per_hour = self._energy_decay_per_hour() / 100.0  # 转换为 0~1 范围
        total_decay = -decay_per_hour * hours
        await self.update_state(energy_delta=total_decay, note=f"自然衰减 {hours}h")

    async def consume_reply(self) -> None:
        """回复一次的能量消耗。"""
        cost = self._energy_cost_reply() / 100.0  # 转换为 0~1 范围
        await self.update_state(energy_delta=-cost, social_delta=0.05, note="回复消息")

    # ------------------------------------------------------------ 作息计划
    async def today_segments(self, date_key: str | None = None) -> list[DayPlanSegment]:
        """取今日所有计划时段。没有则生成。"""
        if not date_key:
            date_key = self._today()

        rows = await self._db.fetch_all(
            "SELECT * FROM day_plans WHERE plan_date=? ORDER BY start_min",
            (date_key,),
        )

        if not rows:
            await self._generate_plan(date_key)
            rows = await self._db.fetch_all(
                "SELECT * FROM day_plans WHERE plan_date=? ORDER BY start_min",
                (date_key,),
            )

        return [
            DayPlanSegment(
                id=row["id"],
                plan_date=row["plan_date"],
                start_min=row["start_min"],
                end_min=row["end_min"],
                activity=row["activity"],
                intent=row["intent"],
                energy_cost=row["energy_cost"],
                planned_by=row["planned_by"],
                state=row["state"],
                summary=row["summary"],
                stats=json.loads(row["stats"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def _generate_plan(self, date_key: str) -> None:
        """自主生成当日计划。核心：必须有休息时段。"""
        sleep_start = self._sleep_start()
        sleep_end = self._sleep_end()

        def time_to_min(t: time) -> int:
            return t.hour * 60 + t.minute

        # 简化示例：固定模板
        segments = [
            (time_to_min(sleep_end), time_to_min(time(9, 0)), "browse", "晨间唤醒"),
            (
                time_to_min(time(9, 0)),
                time_to_min(time(12, 0)),
                "reply",
                "上午活跃时段",
            ),
            (time_to_min(time(12, 0)), time_to_min(time(14, 0)), "rest", "午休"),
            (
                time_to_min(time(14, 0)),
                time_to_min(time(18, 0)),
                "social",
                "下午社交时段",
            ),
            (time_to_min(time(18, 0)), time_to_min(time(20, 0)), "create", "晚间整理"),
            (
                time_to_min(time(20, 0)),
                time_to_min(time(22, 0)),
                "browse",
                "夜间轻松互动",
            ),
            (time_to_min(time(22, 0)), time_to_min(sleep_start), "rest", "准备休息"),
        ]

        # 夜间睡眠
        if sleep_start > sleep_end:
            segments.append((time_to_min(sleep_start), 1440, "rest", "夜间睡眠"))
            segments.append((0, time_to_min(sleep_end), "rest", "夜间睡眠"))
        else:
            segments.append(
                (time_to_min(sleep_start), time_to_min(sleep_end), "rest", "夜间睡眠")
            )

        for start_min, end_min, activity, intent in segments:
            await self._db.execute(
                "INSERT OR IGNORE INTO day_plans(plan_date,start_min,end_min,activity,intent,"
                "energy_cost,planned_by,state,summary,stats,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    date_key,
                    start_min,
                    end_min,
                    activity,
                    intent,
                    0.1 if activity == "rest" else 0.3,
                    "bot",
                    "planned",
                    "",
                    json.dumps({}),
                    now(),
                ),
            )

    async def current_segment(self) -> DayPlanSegment | None:
        """当前时刻 bot 应该处于什么状态。"""
        segments = await self.today_segments()
        now_min = datetime.now().hour * 60 + datetime.now().minute

        for seg in segments:
            if seg.start_min <= now_min < seg.end_min:
                return seg
        return None

    async def should_respond(self, priority: int) -> tuple[bool, str]:
        """判断当前是否应该回复。返回 (是否回复, 拒绝原因)。"""
        state = await self.snapshot()
        seg = await self.current_segment()

        if seg is None:
            return True, ""

        # Shared priorities are inverse: 0 is admin, 10 is a direct mention,
        # while larger values are less urgent/background work.
        if seg.activity == "rest" and int(priority) > 10:
            return False, "休息中，非紧急消息暂不回复"

        if state.energy < 0.1:
            return False, "能量耗尽，暂时无法回复"

        # 检查 token 预算
        budget = state.budget
        tokens_used = budget.get("tokens_used_today", 0)
        tokens_daily = budget.get("tokens_daily", self._token_budget_daily())
        if tokens_used >= tokens_daily:
            return False, "今日预算用尽，明天再聊"

        return True, ""

    async def consume_tokens(self, tokens: int) -> None:
        """记录 token 消耗。"""
        state = await self.snapshot()
        budget = state.budget.copy()
        budget["tokens_used_today"] = budget.get("tokens_used_today", 0) + tokens

        # 更新状态
        await self._db.execute(
            "UPDATE life_states SET budget=? WHERE id=?",
            (json.dumps(budget), state.id),
        )

    async def should_proactive(self) -> bool:
        """判断当前是否可以主动发起互动。"""
        if not self._proactive_enabled():
            return False

        state = await self.snapshot()
        seg = await self.current_segment()

        if seg is None or seg.activity not in ("social", "reply"):
            return False

        if state.energy < 0.4 or state.social > 0.8:
            return False

        # 随机概率，避免过于机械
        return random.random() < 0.2

    # ------------------------------------------------------------ 四维关系更新
    async def adjust_relationship(
        self,
        actor_id: str,
        familiarity_delta: float = 0.0,
        trust_delta: float = 0.0,
        warmth_delta: float = 0.0,
        conflict_delta: float = 0.0,
    ) -> None:
        """增量调整四维关系。由对话质量与行为触发。"""
        from ..storage import ProfileStore

        store = ProfileStore(self._db)
        profile = await store.get(actor_id)
        if profile is None:
            await store.upsert(actor_id, display_name="")
            profile = await store.get(actor_id)
        assert profile is not None

        updates = {}
        if familiarity_delta != 0.0:
            updates["familiarity"] = max(
                0.0, min(1.0, profile.familiarity + familiarity_delta)
            )
        if trust_delta != 0.0:
            updates["trust"] = max(0.0, min(1.0, profile.trust + trust_delta))
        if warmth_delta != 0.0:
            updates["warmth"] = max(0.0, min(1.0, profile.warmth + warmth_delta))
        if conflict_delta != 0.0:
            # conflict 字段在 Profile 中叫 conflict
            current_conflict = getattr(profile, "conflict", 0.0)
            updates["conflict"] = max(0.0, min(1.0, current_conflict + conflict_delta))

        if updates:
            await store.upsert(actor_id, delta=updates)
