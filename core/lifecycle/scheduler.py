"""唯一的主循环：看哪些活动到点了，交给对应的分支，记下结果。

主干不知道具体有哪些分支，只认识 ``Branch`` 这个接口；分支由 ``app.py`` 注册进来。

和旧主循环保持一致的地方：
- 只在时刻之后的 ``grace_minutes`` 分钟内触发，不补跑很久以前的
- 同一种活动同时只跑一个；一轮里同一种活动最多开始一个时刻
- 醒着的时候才跑「醒着的活动」，睡着的时候才跑「睡着时的活动」（日终清算之类）

比旧代码多做的：错过了时间（比如上一个还没跑完）的时刻明确记成 skipped，而不是悄悄不跑。
一个例行检查或一种活动的准备出错，只记错误日志，同一轮里别的照跑（旧主循环是整轮作废，
附属的心情、每日计划坏了，评论、私信也跟着一直不跑）。

分支可以多实现两个方法（插件里的五种定时活动都用到了）：
- ``prepare(day, now)``：每轮都调，由分支自己保证今天的时刻排好了（代替「每天只问一次 plan」）
- ``busy()``：除了主干起的任务，别处也可能占着这种活动
- ``run_now(**kwargs)``：手动叫一次时做什么（命令、工具走 ``Scheduler.run_now``）
"""

from __future__ import annotations

import asyncio
import inspect
from astrbot.api import logger
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Protocol, Union

from .activities import ActivityRuns, ActivityState
from .day import SleepWindow, today


AWAKE = "awake"
ASLEEP = "asleep"
MANUAL_REASON = "手动触发"


class Branch(Protocol):
    kind: str          # 活动类型，比如 "proactive"
    phase: str         # AWAKE 或 ASLEEP

    def enabled(self) -> bool: ...

    def plan(self, day: str, now: datetime) -> list[str]:
        """今天要在哪些时刻做（"HH:MM"）。每天只问一次。"""

    async def run(self, day: str, slot: str) -> None:
        """做这一次。抛异常算失败。"""


@dataclass(frozen=True)
class Routine:
    """每轮都看一眼的事（轮询、按条件自己决定做不做的清算和总结），不按时刻排。

    在主循环里直接 await，按注册顺序跑；一个抛异常只记日志，后面的照跑。
    """
    name: str
    fn: Callable[[], object]            # 同步或异步都行
    phase: str | None = None            # AWAKE / ASLEEP / None = 都跑
    enabled: Callable[[], bool] = lambda: True
    after_slots: bool = False           # True = 在定时活动之后跑


class Scheduler:
    def __init__(
        self,
        runs: Union[ActivityRuns, Callable[[], ActivityRuns]],
        sleep_window: Callable[[], SleepWindow],
        clock: Callable[[], datetime] = datetime.now,
        grace_minutes: int = 5,
        spawn: Callable[[Awaitable], asyncio.Task] = asyncio.ensure_future,
    ):
        # 可以直接给表，也可以给一个每次取表的函数（插件初始化时才换成 sqlite 里那份）。
        self._get_runs = runs if callable(runs) else (lambda: runs)
        self._sleep_window = sleep_window
        self._clock = clock
        self._grace = timedelta(minutes=grace_minutes)
        self._spawn = spawn
        self._branches: dict[str, Branch] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._routines: list[Routine] = []

    def register(self, branch: Branch) -> None:
        if branch.kind in self._branches:
            raise ValueError(f"活动 {branch.kind} 已经注册过")
        self._branches[branch.kind] = branch

    def add_routine(self, routine: Routine) -> None:
        if any(r.name == routine.name for r in self._routines):
            raise ValueError(f"例行检查 {routine.name} 已经注册过")
        self._routines.append(routine)

    def phase(self, now: datetime | None = None) -> str:
        return ASLEEP if self._sleep_window().is_asleep(now or self._clock()) else AWAKE

    async def _run_routines(self, phase: str, after_slots: bool) -> None:
        for routine in self._routines:
            if routine.after_slots != after_slots or routine.phase not in (None, phase):
                continue
            try:
                if not routine.enabled():
                    continue
                result = routine.fn()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[BiliBot] 例行检查 {routine.name} 出错（这一轮跳过它，别的照跑）: {exc}\n{traceback.format_exc()}")

    @property
    def _runs(self) -> ActivityRuns:
        return self._get_runs()

    def startup(self) -> int:
        """插件启动时调用：上次没跑完的记成被中断（不重跑）。"""
        return self._runs.interrupt_running()

    def busy(self, kind: str) -> bool:
        task = self._tasks.get(kind)
        if task is not None and not task.done():
            return True
        external = getattr(self._branches.get(kind), "busy", None)
        return bool(external and external())

    def run_now(self, kind: str, **kwargs) -> asyncio.Task | None:
        """手动叫一次（不管作息和日程）。忙不忙只在这里判断：正忙返回 None，否则返回起好的任务。

        任务的结果就是分支 ``run_now`` 的返回值，出错时是 None。
        """
        if self.busy(kind):
            return None
        branch = self._branches[kind]
        now = self._clock()
        day = today(now)
        slot = self._runs.start_manual(day, kind, now.strftime("%H:%M:%S"), MANUAL_REASON)
        task = self._spawn(self._run(kind, day, slot, lambda: branch.run_now(**kwargs), MANUAL_REASON))
        self._tasks[kind] = task
        return task

    async def tick(self, now: datetime | None = None) -> list[str]:
        """走一轮，返回这一轮开始了哪些活动（"kind@slot"）。"""
        now = now or self._clock()
        day = today(now)
        phase = self.phase(now)
        await self._run_routines(phase, after_slots=False)
        started = self._tick_slots(now, day, phase)
        await self._run_routines(phase, after_slots=True)
        return started

    def _tick_slots(self, now: datetime, day: str, phase: str) -> list[str]:
        started = []
        for kind, branch in self._branches.items():
            try:
                slot = self._tick_branch(now, day, phase, kind, branch)
            except Exception as exc:
                logger.error(f"[BiliBot] 定时活动 {kind} 排日程出错（这一轮跳过它，别的照跑）: {exc}\n{traceback.format_exc()}")
                continue
            if slot is not None:
                started.append(f"{kind}@{slot}")
        return started

    def _tick_branch(self, now: datetime, day: str, phase: str, kind: str, branch: Branch) -> str | None:
        """一种活动走一轮：排好今天的时刻，到点的开一个任务。返回开始的时刻，没开始返回 None。"""
        if branch.phase != phase or not branch.enabled():
            return None
        prepare = getattr(branch, "prepare", None)
        if prepare is not None:
            prepare(day, now)
        elif not self._runs.is_planned(day, kind):
            self._runs.plan(day, kind, branch.plan(day, now))
        for run in self._runs.runs(day, kind):
            if run.state is not ActivityState.PLANNED:
                continue
            slot_time = self._slot_time(now, run.slot)
            if slot_time is None or now < slot_time:
                continue
            if now - slot_time >= self._grace:
                reason = "上一次还没做完" if self.busy(kind) else "错过了时间"
                self._runs.skip(day, kind, run.slot, reason)
                continue
            if self.busy(kind):
                continue
            self._runs.start(day, kind, run.slot)
            self._tasks[kind] = self._spawn(self._run(kind, day, run.slot, lambda b=branch, d=day, s=run.slot: b.run(d, s)))
            return run.slot
        return None

    async def _run(self, kind: str, day: str, slot: str, work: Callable[[], Awaitable], reason: str = ""):
        try:
            result = await work()
        except asyncio.CancelledError:
            try:
                self._runs.interrupt(day, kind, slot, "任务被取消")
            except Exception as exc:
                # 插件停用时表可能已经关了；下次打开时 interrupt_running 会补记。
                logger.debug(f"[BiliBot] 记录 {kind}（{slot}）被取消失败: {exc}")
            raise
        except Exception as exc:
            logger.error(f"[BiliBot] {kind}（{slot}）失败: {exc}")
            self._runs.finish(day, kind, slot, ok=False, reason=str(exc)[:200])
            return None
        self._runs.finish(day, kind, slot, ok=True, reason=reason)
        return result

    @staticmethod
    def _slot_time(now: datetime, slot: str) -> datetime | None:
        try:
            hour, minute = (int(part) for part in slot.split(":", 1))
            return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (TypeError, ValueError):
            return None
