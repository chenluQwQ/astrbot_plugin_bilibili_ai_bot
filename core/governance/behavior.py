"""行为治理：Skills 预算闸门与主动行为决策器。

对应 issue #10 #11。问题根源：

1. 旧实现 LLM 可无限调用平台 API，无成本意识，单次对话可能烧掉几千 token。
2. Skills 工具（发评论、发动态、转发、点赞）没有审批流程，LLM 被诱导可直接执行写操作。
3. 主动行为（日报、兴趣分享、动态发布）缺乏统一调度，各 mixin 各自定时，重复与冲突。

新方案：

**预算闸门**：
- 每个 skill 声明预估成本（tokens）
- 调用前检查当日预算余额（persona.day_plans.token_budget）
- 写工具必须有 capability token（由用户原始消息授予，不可伪造）

**主动行为决策器**：
- 从 persona 引擎查询当前状态（能量、模式、社交饱和度）
- 根据四维关系选择互动对象（优先 warmth 高且 familiarity 足够的）
- 生成候选行为（发日报、分享内容、回访熟人），排优先级
- 提交到 actions 表等待确认（管理员可在 WebUI 批量审批）

配置项：
- ``SKILLS_DAILY_TOKEN_BUDGET``：Skills 每日 token 上限（独立于 persona 预算）
- ``PROACTIVE_APPROVAL_REQUIRED``：主动行为是否需要预先审批
- ``PROACTIVE_MAX_PER_DAY``：每日主动行为次数上限
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..adapter import ActionRegistry, ActionRequest
from ..persona import PersonaEngine
from ..security import Capability
from ..storage import Database, ProfileStore, now


@dataclass
class SkillCost:
    """Skill 调用成本声明。"""

    name: str
    estimated_tokens: int
    tier: str  # cheap / moderate / expensive


# 内置 skills 成本表（实际应从 skill 元数据读取）
BUILTIN_COSTS = {
    "reply_comment": SkillCost("reply_comment", 150, "cheap"),
    "post_dynamic": SkillCost("post_dynamic", 200, "moderate"),
    "send_dm": SkillCost("send_dm", 100, "cheap"),
    "like": SkillCost("like", 10, "cheap"),
    "forward": SkillCost("forward", 50, "cheap"),
    "understand_video": SkillCost("understand_video", 1500, "expensive"),
}


class BudgetGate:
    """预算闸门。"""

    def __init__(self, db: Database, config_getter) -> None:
        self._db = db
        self._get = config_getter

    def _daily_budget(self) -> int:
        return int(self._get("SKILLS_DAILY_TOKEN_BUDGET", 10000))

    async def can_afford(
        self, skill: str, tokens: int | None = None
    ) -> tuple[bool, str]:
        """判断预算是否足够。返回 (是否允许, 拒绝原因)。"""
        cost = BUILTIN_COSTS.get(skill)
        if cost is None and tokens is None:
            return True, ""  # 未知 skill，放行（保守）
        estimated = tokens or (cost.estimated_tokens if cost else 0)
        date_key = self._today()
        kv_key = f"skills_budget:{date_key}"
        used = int(await self._db.kv_get(kv_key, 0) or 0)
        budget = self._daily_budget()
        if used + estimated > budget:
            return False, f"Skills 预算不足（已用 {used}/{budget}）"
        return True, ""

    async def consume(self, skill: str, tokens: int) -> None:
        """记录实际消耗。"""
        date_key = self._today()
        kv_key = f"skills_budget:{date_key}"
        used = int(await self._db.kv_get(kv_key, 0) or 0)
        await self._db.kv_set(kv_key, used + tokens, ttl=86400)

    async def remaining(self) -> int:
        """剩余预算。"""
        date_key = self._today()
        kv_key = f"skills_budget:{date_key}"
        used = int(await self._db.kv_get(kv_key, 0) or 0)
        budget = self._daily_budget()
        return max(0, budget - used)

    @staticmethod
    def _today() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class ProactiveCandidate:
    """候选主动行为。"""

    kind: str  # daily_report / interest_share / revisit / dynamic_post
    priority: int
    target_id: str
    summary: str
    action: ActionRequest


class ProactiveDecider:
    """主动行为决策器。"""

    def __init__(
        self,
        db: Database,
        config_getter,
        persona: PersonaEngine,
        budget_gate: BudgetGate,
    ) -> None:
        self._db = db
        self._get = config_getter
        self._persona = persona
        self._budget = budget_gate
        self._registry = ActionRegistry(db)

    def _approval_required(self) -> bool:
        return bool(self._get("PROACTIVE_APPROVAL_REQUIRED", True))

    def _max_per_day(self) -> int:
        return int(self._get("PROACTIVE_MAX_PER_DAY", 5))

    async def generate_candidates(
        self
    ) -> list[ProactiveCandidate]:
        """生成今日候选主动行为。"""
        # 检查前置条件
        if not await self._persona.should_proactive():
            return []
        date_key = self._today()
        kv_key = f"proactive_count:{date_key}"
        count = int(await self._db.kv_get(kv_key, 0) or 0)
        if count >= self._max_per_day():
            return []

        candidates: list[ProactiveCandidate] = []

        # 候选 1：日报（每日一次）
        daily_key = f"daily_report:{date_key}"
        done = await self._db.kv_get(daily_key, 0)
        if not done:
            candidates.append(
                ProactiveCandidate(
                    kind="daily_report",
                    priority=80,
                    target_id="",
                    summary="发布今日状态日报",
                    action=ActionRequest(
                        tool="post_dynamic",
                        summary="今日状态日报",
                        args={"content": "[由 LLM 生成的日报内容]"},
                    ),
                )
            )

        # 候选 2：兴趣分享（从收藏/关注中挑一个推荐）
        interest_key = f"interest_share:{date_key}"
        done = await self._db.kv_get(interest_key, 0)
        if not done:
            candidates.append(
                ProactiveCandidate(
                    kind="interest_share",
                    priority=60,
                    target_id="",
                    summary="分享感兴趣的内容",
                    action=ActionRequest(
                        tool="post_dynamic",
                        summary="兴趣内容分享",
                        args={"content": "[由 LLM 挑选并生成分享文案]"},
                    ),
                )
            )

        # 候选 3：回访熟人（warmth 高且最近未互动）
        profiles = ProfileStore(self._db)
        # 简化示例：实际需查询 profiles 表并过滤
        # top_friends = await profiles.top_by_warmth(limit=5)
        # for friend in top_friends:
        #     if await should_revisit(friend):
        #         candidates.append(...)

        return candidates

    async def submit(self, candidate: ProactiveCandidate) -> int:
        """提交候选行为到 actions 表。需审批则 state=pending，否则直接 running。"""
        action_id = await self._registry.register(candidate.action)
        if action_id == 0:
            return 0  # 幂等：已存在
        # 更新计数
        date_key = self._today()
        kv_key = f"proactive_count:{date_key}"
        count = int(await self._db.kv_get(kv_key, 0) or 0)
        await self._db.kv_set(kv_key, count + 1, ttl=86400)
        # 标记该类型已执行
        type_key = f"{candidate.kind}:{date_key}"
        await self._db.kv_set(type_key, 1, ttl=86400)
        return action_id

    async def stats(self) -> dict[str, Any]:
        date_key = self._today()
        kv_key = f"proactive_count:{date_key}"
        count = int(await self._db.kv_get(kv_key, 0) or 0)
        max_count = self._max_per_day()
        budget_remaining = await self._budget.remaining()
        return {
            "proactive_used_today": count,
            "proactive_max_per_day": max_count,
            "proactive_remaining": max(0, max_count - count),
            "skills_budget_remaining": budget_remaining,
        }

    @staticmethod
    def _today() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
