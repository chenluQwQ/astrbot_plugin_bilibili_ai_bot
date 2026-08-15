"""周期行为调度：日报、兴趣分享、动态发布的定时触发。

对应 issue #12。旧实现各 mixin 各自用 threading.Timer 定时，重复且易冲突。
新方案统一由 AstrBot 定时插件钩子（如有）或独立线程调度，所有周期任务走
ProactiveDecider 生成候选，审批后执行。

定时任务类型：

1. **日报**（daily_report）：每天固定时间（如 09:00）发布状态摘要，
   内容包括昨日互动统计、今日计划、当前情绪状态。
2. **兴趣分享**（interest_share）：每天 1-2 次，从关注/收藏中挑选内容推荐给粉丝，
   附上个性化评论（由 LLM 生成）。
3. **动态发布**（dynamic_post）：不定期（根据 persona 状态），分享思考、观察、
   或对热点的看法，模拟真实用户的动态节奏。
4. **关系维护**（revisit）：每周回访 warmth 高但近期未互动的用户，主动私信或评论。

配置项：
- ``SCHEDULE_DAILY_REPORT_TIME``：日报发布时间（HH:MM）
- ``SCHEDULE_INTEREST_SHARE_TIMES``：兴趣分享时间列表（JSON 数组）
- ``SCHEDULE_DYNAMIC_POST_ENABLED``：是否启用不定期动态
- ``SCHEDULE_REVISIT_INTERVAL_DAYS``：关系维护间隔（天）
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, time, timezone
from typing import Any

from ..governance import ProactiveDecider
from ..persona import PersonaEngine
from ..storage import Database


class ScheduledTasks:
    """周期任务调度器。"""

    def __init__(
        self,
        db: Database,
        config_getter,
        persona: PersonaEngine,
        decider: ProactiveDecider,
    ) -> None:
        self._db = db
        self._get = config_getter
        self._persona = persona
        self._decider = decider
        self._running = False

    # ------------------------------------------------------------ 配置
    def _daily_report_time(self) -> time:
        s = str(self._get("SCHEDULE_DAILY_REPORT_TIME", "09:00"))
        h, m = map(int, s.split(":"))
        return time(h, m)

    def _interest_share_times(self) -> list[time]:
        raw = str(self._get("SCHEDULE_INTEREST_SHARE_TIMES", '["11:00","16:00"]'))
        times_str = json.loads(raw)
        result = []
        for t in times_str:
            h, m = map(int, t.split(":"))
            result.append(time(h, m))
        return result

    def _dynamic_post_enabled(self) -> bool:
        return bool(self._get("SCHEDULE_DYNAMIC_POST_ENABLED", False))

    def _revisit_interval_days(self) -> int:
        return int(self._get("SCHEDULE_REVISIT_INTERVAL_DAYS", 7))

    # ------------------------------------------------------------ 核心循环
    async def run(self) -> None:
        """启动调度循环。阻塞，应在独立线程/Task 运行。"""
        self._running = True
        while self._running:
            await self._tick()
            await asyncio.sleep(60)  # 每分钟检查一次

    def stop(self) -> None:
        self._running = False

    async def _tick(self) -> None:
        """单次调度 tick。检查所有定时任务是否到期。"""
        dt_now = datetime.now(timezone.utc)
        now_time = dt_now.time()
        date_key = dt_now.strftime("%Y-%m-%d")

        # 1. 日报
        report_time = self._daily_report_time()
        if self._near(now_time, report_time):
            kv_key = f"scheduled:daily_report:{date_key}"
            done = await self._db.kv_get(kv_key, 0)
            if not done:
                await self._trigger_daily_report()
                await self._db.kv_set(kv_key, 1, ttl=86400)

        # 2. 兴趣分享
        for share_time in self._interest_share_times():
            if self._near(now_time, share_time):
                kv_key = f"scheduled:interest_share:{date_key}:{share_time.strftime('%H:%M')}"
                done = await self._db.kv_get(kv_key, 0)
                if not done:
                    await self._trigger_interest_share()
                    await self._db.kv_set(kv_key, 1, ttl=86400)

        # 3. 不定期动态（每小时随机概率触发，由 persona 状态决定）
        if self._dynamic_post_enabled() and now_time.minute == 0:
            kv_key = f"scheduled:dynamic_check:{date_key}:{now_time.hour}"
            done = await self._db.kv_get(kv_key, 0)
            if not done:
                await self._maybe_dynamic_post()
                await self._db.kv_set(kv_key, 1, ttl=3600)

        # 4. 关系维护（每天检查一次，00:30）
        if now_time.hour == 0 and now_time.minute == 30:
            kv_key = f"scheduled:revisit_check:{date_key}"
            done = await self._db.kv_get(kv_key, 0)
            if not done:
                await self._trigger_revisit()
                await self._db.kv_set(kv_key, 1, ttl=86400)

    @staticmethod
    def _near(t1: time, t2: time, tolerance_min: int = 1) -> bool:
        """判断两个时间是否接近（容忍度内）。"""
        delta = abs((t1.hour * 60 + t1.minute) - (t2.hour * 60 + t2.minute))
        return delta <= tolerance_min

    # ------------------------------------------------------------ 具体任务
    async def _trigger_daily_report(self) -> None:
        """触发日报生成。"""
        candidates = await self._decider.generate_candidates()
        for c in candidates:
            if c.kind == "daily_report":
                await self._decider.submit(c)
                break

    async def _trigger_interest_share(self) -> None:
        """触发兴趣分享。"""
        candidates = await self._decider.generate_candidates()
        for c in candidates:
            if c.kind == "interest_share":
                await self._decider.submit(c)
                break

    async def _maybe_dynamic_post(self) -> None:
        """随机触发动态发布（由 persona 状态决定）。"""
        state = await self._persona.snapshot()
        seg = await self._persona.current_segment()
        if seg is None or seg["mode"] not in ("active", "social"):
            return
        if state.energy < 40.0 or state.mood < 0.0:
            return
        # 简化：随机概率 10%
        import random

        if random.random() < 0.1:
            from ..adapter import ActionRequest

            from ..governance import ProactiveCandidate

            candidate = ProactiveCandidate(
                kind="dynamic_post",
                priority=50,
                target_id="",
                summary="不定期动态发布",
                action=ActionRequest(
                    tool="post_dynamic",
                    summary="随机动态",
                    args={"content": "[由 LLM 生成的动态内容]"},
                ),
            )
            await self._decider.submit(candidate)

    async def _trigger_revisit(self) -> None:
        """检查并触发关系维护。"""
        # 简化示例：实际需查询 profiles 并过滤最近未互动 + warmth 高的
        # top = await store.top_by_warmth(limit=5)
        # for profile in top:
        #     if should_revisit(profile):
        #         candidate = ProactiveCandidate(...)
        #         await self._decider.submit(candidate)
        pass

    async def stats(self) -> dict[str, Any]:
        """调度统计。"""
        date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_report_done = bool(
            await self._db.kv_get(f"scheduled:daily_report:{date_key}", 0)
        )
        return {
            "running": self._running,
            "daily_report_done": daily_report_done,
            "next_report_time": self._daily_report_time().strftime("%H:%M"),
        }
