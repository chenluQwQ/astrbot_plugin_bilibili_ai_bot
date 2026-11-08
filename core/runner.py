"""插件的启停：AstrBot 激活后开后台、启动/停止主循环、停用时收拾干净；主循环和它管的夜里例行、Cookie 检查。

插件上只留一个 ``runner``。主循环任务、启动锁、清算任务、上次查 Cookie 的时间、问题日志的
handler 住在这里；``_running`` 和各活动正在跑的任务别处还在读，留在插件上。
"""
from __future__ import annotations

import asyncio
import time
import traceback
from datetime import datetime

from astrbot.api import logger
from .config import DATA_DIR, LAYERED_DB_FILE, PLUGIN_VERSION, PROBLEM_LOG_FILE
from .domain.dynamic_ideas import DynamicIdeas
from .domain.memos import Memos
from .infra import problem_log
from .infra.host_view import HostView
from .storage.migration import backup_data_dir

MAIN_TASK_NAME = "astrbot_plugin_bilibili_ai_bot.main_loop"


class Runner(HostView):
    HOST_API = frozenset({
        "_bangumi_task", "_consolidation", "_dynamic_task", "_dynamic_watch_task", "_proactive_task",
        "_running", "_special_follow_task", "activities", "activity_logs", "app", "bili_auth", "bili_http", "cache_files", "comment_context", "day_schedules", "event_runtime", "processed_keys", "people", "affection", "memory_docs", "states",
        "layered_runtime", "memories", "memory_startup", "private_inbox", "private_poll",
        "settings", "share_reaction", "share_session", "summaries", "yt_dlp_updater",
    })

    def __init__(self, host):
        super().__init__(host)
        self.task = None
        self.start_lock = asyncio.Lock()
        self.consolidation_task = None
        self.last_cookie_check = 0
        self.problem_log_handler = None

    async def initialize(self):
        """Start background work only after AstrBot has formally activated the plugin."""
        try:
            self.problem_log_handler = problem_log.attach(logger, PROBLEM_LOG_FILE)
        except Exception as exc:
            self.problem_log_handler = None
            logger.warning(f"[BiliBot] 问题日志文件打不开（不影响运行）: {exc}")
        try:
            # 每个版本第一次启动先备份 data/（json + sqlite），在任何迁移之前。
            await asyncio.to_thread(backup_data_dir, DATA_DIR, PLUGIN_VERSION, LAYERED_DB_FILE)
        except Exception as exc:
            logger.error(f"[BiliBot] 升级前备份数据失败（不影响运行）: {exc}")
        try:
            # 小状态最先接：日程、心情这些下面马上就要读
            self.states.connect(LAYERED_DB_FILE)
        except Exception as exc:
            logger.error(f"[BiliBot] 小状态接不上数据库，先照旧写 json（下次接上时会合并进去）: {exc}")
        # 日程状态和四层服务同一个 sqlite 文件、各用各的连接；打不开就只记在内存里。
        self.day_schedules._open_activity_runs(LAYERED_DB_FILE)
        try:
            self._host.dynamic_ideas = DynamicIdeas.open(LAYERED_DB_FILE)
        except Exception as exc:
            logger.error(f"[BiliBot] 动态池子打不开，想发的只记在内存里（重启就没了）: {exc}")
        try:
            self._host.memos = Memos.open(LAYERED_DB_FILE)
        except Exception as exc:
            logger.error(f"[BiliBot] 备忘录打不开，记的只在内存里（重启就没了）: {exc}")
        try:
            self.activity_logs.connect(LAYERED_DB_FILE)
        except Exception as exc:
            logger.error(f"[BiliBot] 活动日志接不上数据库，先照旧写 json（下次接上时会合并进去）: {exc}")
        try:
            self.processed_keys.connect(LAYERED_DB_FILE)
        except Exception as exc:
            logger.error(f"[BiliBot] 去重账本接不上数据库，先照旧写 json（下次接上时会合并进去）: {exc}")
        try:
            self.people.connect(LAYERED_DB_FILE)
        except Exception as exc:
            logger.error(f"[BiliBot] 好感度和画像接不上数据库，先照旧写 json（下次接上时会合并进去）: {exc}")
        try:
            self.memory_docs.connect(LAYERED_DB_FILE)
        except Exception as exc:
            logger.error(f"[BiliBot] 番剧进度和视频记忆接不上数据库，先照旧写 json（下次接上时会合并进去）: {exc}")
        self.affection.reload()
        self.affection.pin_owner(str(self.settings.get("OWNER_MID") or "").strip())
        self.affection.mark_reached_from_scores()
        try:
            await self.layered_runtime.open()
            logger.info("[BiliBot] 四层运行服务已连接")
        except Exception as exc:
            # The established JSON/in-memory path remains usable when SQLite cannot
            # be opened; the runtime observer records no data until the next reload.
            logger.error(f"[BiliBot] 四层运行服务启动失败，已回退兼容主链: {exc}")
        else:
            try:
                await self.memory_startup._initialize_unified_memory()
            except Exception as exc:
                # 不拿用户已有的 JSON 记忆冒险；迁移失败只降级记忆存储，事件层照常运行。
                self.memories.mark_pending(exc)
                logger.error(
                    f"[BiliBot] 统一记忆迁移失败，暂时继续使用 memory.json: {exc}"
                )
            try:
                await self.memory_startup._initialize_seen_videos()
            except Exception as exc:
                # 去重账本失败不应把已经成功的语义记忆迁移标成失败。
                logger.error(
                    f"[BiliBot] 永久视频去重账本迁移失败，已保留旧记录兜底: {exc}"
                )
        if not self.bili_http._has_cookie():
            logger.warning("[BiliBot] Cookie未配置，后台任务未启动")
            return
        valid, msg = await self.bili_auth.check_cookie()
        if valid:
            await self.start()
            logger.info("[BiliBot] 自动启动")
        elif "检查失败" in str(msg):
            # 网络暂时不可用不等于 Cookie 失效；仍启动主循环等待网络恢复。
            logger.warning(
                f"[BiliBot] Cookie 检查暂时失败（{msg}），仍启动后台任务等待网络恢复"
            )
            await self.start()
        else:
            logger.warning("[BiliBot] Cookie无效")

    async def start(self):
        async with self.start_lock:
            if self._running and self.task and not self.task.done():
                return

            # Hot reloads can leave a task from an older plugin object in the same
            # event loop. A stable task name gives the new instance a process-wide
            # single-instance guard, independent of Python module reload details.
            current = asyncio.current_task()
            stale_tasks = [
                task
                for task in asyncio.all_tasks()
                if task is not current
                and not task.done()
                and task.get_name() == MAIN_TASK_NAME
            ]
            for task in stale_tasks:
                task.cancel()
            if stale_tasks:
                await asyncio.gather(*stale_tasks, return_exceptions=True)
                logger.warning(
                    f"[BiliBot] 已清理 {len(stale_tasks)} 个热重载残留主循环"
                )

            await self.bili_auth._ensure_buvid()
            try:
                self.day_schedules._mark_overdue_schedule_as_triggered_on_startup()
            except Exception as exc:
                # 日程坏了不能拖住主循环（评论、私信照常）；主循环每轮准备日程时会再试，到点判断也不会补跑过期的。
                logger.error(f"[BiliBot] 启动时整理今天的日程出错，跳过（不影响评论和私信）: {exc}\n{traceback.format_exc()}")
            if self.settings.get("ENABLE_PRIVATE_MESSAGES"):
                if self.private_inbox.initialized():
                    try:
                        startup_delay = max(
                            60,
                            min(
                                3600,
                                int(
                                    self.settings.get("PRIVATE_MESSAGE_IDLE_POLL_INTERVAL")
                                    or 180
                                ),
                            ),
                        )
                    except (TypeError, ValueError):
                        startup_delay = 180
                    self.private_poll._private_message_next_poll_at = (
                        time.monotonic() + startup_delay
                    )
                    logger.info(
                        f"[BiliBot] 私信监听将在 {startup_delay} 秒启动，"
                        "避免重载后立即重试接口"
                    )
            self._host._running = True
            self.task = asyncio.create_task(
                self.main_loop(), name=MAIN_TASK_NAME
            )
            logger.info("[BiliBot] 启动")

    async def stop(self):
        async with self.start_lock:
            self._host._running = False
            task = self.task
            self.task = None
            if task and not task.done():
                task.cancel()
                if task is not asyncio.current_task():
                    await asyncio.gather(task, return_exceptions=True)
        self.comment_context._cancel_comment_inspections()
        if self._proactive_task and not self._proactive_task.done():
            self._proactive_task.cancel()
            self._host._proactive_task = None
        if self._dynamic_task and not self._dynamic_task.done():
            self._dynamic_task.cancel()
            self._host._dynamic_task = None
        if self._bangumi_task and not self._bangumi_task.done():
            self._bangumi_task.cancel()
            self._host._bangumi_task = None
        if self._special_follow_task and not self._special_follow_task.done():
            self._special_follow_task.cancel()
            self._host._special_follow_task = None
        if self._dynamic_watch_task and not self._dynamic_watch_task.done():
            self._dynamic_watch_task.cancel()
        self._host._dynamic_watch_task = None
        self.share_session._reset_on_stop()
        self.private_poll._reset_poll_state()
        if self.consolidation_task and not self.consolidation_task.done():
            self.consolidation_task.cancel()
            self.consolidation_task = None
        logger.info("[BiliBot] 停止")

    async def terminate(self):
        await self.stop()
        self.yt_dlp_updater.cancel()
        # 分享视频的观看/评价任务会调模型和发消息，停用后不能继续跑。
        for task in list(self.share_reaction._share_reaction_tasks):
            task.cancel()
        for task in list(self.app.tool_watch_jobs.values()):
            task.cancel()
        for task in list(self.app.tool_watch_followup_tasks):
            task.cancel()
        extension_registry = self.app.extension_registry
        if extension_registry is not None:
            try:
                await extension_registry.close()
            except Exception as exc:
                logger.warning(f"[BiliBot] 扩展 Host 关闭异常: {exc}")
        from .network import close_plugin_clients
        await close_plugin_clients(self._host)
        try:
            await self.event_runtime.close()
        except Exception as exc:
            logger.warning(f"[BiliBot] 动作队列关闭异常: {exc}")
        try:
            await self.layered_runtime.close()
        except Exception as exc:
            logger.warning(f"[BiliBot] 四层运行服务关闭异常: {exc}")
        try:
            self.day_schedules._close_activity_runs()
        except Exception as exc:
            logger.warning(f"[BiliBot] 活动状态表关闭异常: {exc}")
        try:
            pool, self._host.dynamic_ideas = getattr(self._host, "dynamic_ideas", None), None
            if pool is not None:
                pool.close()
        except Exception as exc:
            logger.warning(f"[BiliBot] 动态池子关闭异常: {exc}")
        try:
            memos, self._host.memos = getattr(self._host, "memos", None), None
            if memos is not None:
                memos.close()
        except Exception as exc:
            logger.warning(f"[BiliBot] 备忘录关闭异常: {exc}")
        try:
            self.activity_logs.close()
        except Exception as exc:
            logger.warning(f"[BiliBot] 活动日志关闭异常: {exc}")
        try:
            self.processed_keys.close()
        except Exception as exc:
            logger.warning(f"[BiliBot] 去重账本关闭异常: {exc}")
        try:
            self.people.close()
        except Exception as exc:
            logger.warning(f"[BiliBot] 好感度和画像关闭异常: {exc}")
        try:
            self.memory_docs.close()
        except Exception as exc:
            logger.warning(f"[BiliBot] 番剧进度和视频记忆关闭异常: {exc}")
        try:
            self.states.close()
        except Exception as exc:
            logger.warning(f"[BiliBot] 小状态关闭异常: {exc}")
        # 最后摘：上面关闭时出的问题也要记进去。热重载不摘的话新旧实例会各写一遍。
        problem_log.detach(logger, self.problem_log_handler)
        self.problem_log_handler = None
        # LLM 工具不在此手动注销：按名称删除可能误删其他插件覆盖注册的同名工具；
        # AstrBot 会按 handler_module_path 清理当前插件的工具。
        self.cache_files._cleanup_temp_files()
        logger.info("[BiliBot] 已停用")

    async def run_consolidation_safe(self):
        """安全执行日终清算，捕获所有异常。"""
        try:
            logger.info("[BiliBot] 🌙 开始日终清算...")
            summary = await self._consolidation.run_daily()
            logger.info(f"[BiliBot] 🌙 日终清算完成:\n{summary}")
        except Exception as e:
            logger.error(f"[BiliBot] 日终清算异常: {e}", exc_info=True)

    async def main_loop(self):
        """走一轮主干（例行检查和定时活动都在 activities._scheduler 里），睡一会，再走。"""
        logger.info("[BiliBot] 主循环开始")
        while self._running:
            try:
                scheduler, now = self.activities._scheduler(), datetime.now()
                await scheduler.tick(now)
                await asyncio.sleep(60 if scheduler.phase(now) == "asleep" else self.settings.get("POLL_INTERVAL"))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[BiliBot] 主循环出错: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(30)
        self._host._running = False

    async def night_routine(self):
        """睡着时：日终清算没做就起它；做完了（且没在跑）再看日总结、周总结。"""
        if self._consolidation.should_run_today():
            if self.consolidation_task is None or self.consolidation_task.done():
                self.consolidation_task = asyncio.create_task(self.run_consolidation_safe())
        elif self.consolidation_task is None or self.consolidation_task.done():
            try:
                await self.summaries._maybe_daily_summary()
            except Exception as e:
                logger.error(f"[BiliBot] 日总结调度异常: {e}")
            try:
                await self.summaries._maybe_weekly_summary()
            except Exception as e:
                logger.error(f"[BiliBot] 周总结调度异常: {e}")

    def maybe_update_yt_dlp(self):
        self.yt_dlp_updater.maybe_start()

    async def maybe_check_cookie(self):
        if time.time() - self.last_cookie_check > max(1, self.settings.get("COOKIE_CHECK_INTERVAL")) * 3600:
            await self.check_and_refresh_cookie()
            self.last_cookie_check = time.time()

    async def check_and_refresh_cookie(self):
        valid, info = await self.bili_auth.check_cookie()
        if valid:
            logger.info(f"[BiliBot] Cookie OK: {info}")
            return
        logger.warning(f"[BiliBot] Cookie 失效: {info}")
        if self.settings.get("COOKIE_AUTO_REFRESH"):
            ok, msg = await self.bili_auth.refresh_cookie()
            logger.info(f"[BiliBot] 刷新{'成功' if ok else '失败'}: {msg}")

