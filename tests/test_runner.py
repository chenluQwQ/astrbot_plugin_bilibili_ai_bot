"""启停的先后顺序（``core/runner.py``）：AstrBot 激活后开什么、启动先做什么、停用时取消和关掉哪些。

假插件上的每一步都记进 ``host.log``，断言整串顺序。搬出 ``main.py`` 时（8-3b）用同一个假插件跑过
旧代码，两边一样。
"""
from base_parts import attach_base
import asyncio
import types
import unittest
from unittest.mock import patch

import test_autonomous_ranges  # noqa: F401  (installs the AstrBot stub)
from core import runner as runner_module
from core.settings import Settings


class _Closable:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def close(self):
        self.log.append(f"{self.name}.close")


class _AsyncService:
    def __init__(self, log, name):
        self.log, self.name = log, name

    async def open(self):
        self.log.append(f"{self.name}.open")

    async def close(self):
        self.log.append(f"{self.name}.close")


def fake_host(*, cookie=(True, "ok"), has_cookie=True):
    log = []
    host = attach_base(types.SimpleNamespace(log=log, _running=False))
    host.settings = Settings({"ENABLE_PRIVATE_MESSAGES": True, "PRIVATE_MESSAGE_IDLE_POLL_INTERVAL": 300})
    host.private_inbox = types.SimpleNamespace(initialized=lambda: True)
    host.affection = types.SimpleNamespace(reload=lambda: None, pin_owner=lambda uid: None,
                                           mark_reached_from_scores=lambda: 0)
    poll = types.SimpleNamespace(_private_message_next_poll_at=None,
                                 _reset_poll_state=lambda: log.append("private_poll.reset"))
    host.private_poll = poll
    session = types.SimpleNamespace(_reset_on_stop=lambda: log.append("share_session.reset"))
    host.share_session = session
    reaction = types.SimpleNamespace(_share_reaction_tasks=set())
    host.share_reaction = reaction

    async def ensure_buvid():
        log.append("ensure_buvid")

    async def check_cookie():
        log.append("check_cookie")
        return cookie

    async def init_memory():
        log.append("init_memory")

    async def init_seen():
        log.append("init_seen_videos")

    host.bili_auth = types.SimpleNamespace(_ensure_buvid=ensure_buvid, check_cookie=check_cookie)
    host.bili_http._has_cookie = lambda: has_cookie
    host.memory_startup = types.SimpleNamespace(
        _initialize_unified_memory=init_memory, _initialize_seen_videos=init_seen)
    host.memories = types.SimpleNamespace(mark_pending=lambda exc: log.append("memory_sync_pending"))
    host.day_schedules = types.SimpleNamespace()
    host.day_schedules._mark_overdue_schedule_as_triggered_on_startup = lambda: log.append("mark_overdue")
    host.day_schedules._open_activity_runs = lambda path: log.append("open_activity_runs")
    host.day_schedules._close_activity_runs = lambda: log.append("close_activity_runs")
    host.comment_context = types.SimpleNamespace(
        _cancel_comment_inspections=lambda: log.append("cancel_comment_inspections"))
    host.cache_files._cleanup_temp_files = lambda: log.append("cleanup_temp_files")
    host.layered_runtime = _AsyncService(log, "layered_runtime")
    host.event_runtime = _AsyncService(log, "event_runtime")
    host.yt_dlp_updater = types.SimpleNamespace(cancel=lambda: log.append("yt_dlp.cancel"))
    host.app = types.SimpleNamespace(tool_watch_jobs={}, tool_watch_followup_tasks=set(),
                                     extension_registry=_AsyncService(log, "extensions"))
    for name in ("_proactive_task", "_dynamic_task", "_bangumi_task", "_special_follow_task",
                 "_dynamic_watch_task", "_consolidation"):
        setattr(host, name, None)
    return host


def patched(host):
    """把 ``core/runner.py`` 里碰文件和数据库的几样换成只记一笔的。"""
    log = host.log
    return patch.multiple(
        runner_module,
        backup_data_dir=lambda *a: log.append("backup_data_dir"),
        DynamicIdeas=types.SimpleNamespace(open=lambda path: (log.append("dynamic_ideas.open"),
                                                              _Closable(log, "dynamic_ideas"))[1]),
    )


async def idle_main_loop(log):
    log.append("main_loop")
    await asyncio.Event().wait()


async def lifecycle_trace(host, runner):
    """激活 → 停止 → 再启动 → 停用，返回记下的顺序。"""
    runner.main_loop = lambda: idle_main_loop(host.log)
    await runner.initialize()
    await asyncio.sleep(0)
    host.log.append(f"running={host._running}")
    activity = asyncio.ensure_future(asyncio.Event().wait())
    host._proactive_task = activity
    await runner.stop()
    await asyncio.sleep(0)
    host.log.append(f"activity_cancelled={activity.cancelled()} running={host._running}")
    await runner.start()
    await asyncio.sleep(0)
    await runner.terminate()
    host.log.append(f"running={host._running}")
    return host.log


EXPECTED = [
    "backup_data_dir", "open_activity_runs", "dynamic_ideas.open",
    "layered_runtime.open", "init_memory", "init_seen_videos", "check_cookie",
    "ensure_buvid", "mark_overdue", "main_loop", "running=True",
    "cancel_comment_inspections", "share_session.reset", "private_poll.reset",
    "activity_cancelled=True running=False",
    "ensure_buvid", "mark_overdue", "main_loop",
    "cancel_comment_inspections", "share_session.reset", "private_poll.reset",
    "yt_dlp.cancel", "extensions.close", "event_runtime.close", "layered_runtime.close",
    "close_activity_runs", "dynamic_ideas.close", "cleanup_temp_files",
    "running=False",
]


class RunnerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_activate_stop_restart_terminate_order(self):
        host = fake_host()
        runner = runner_module.Runner(host)
        with patched(host):
            log = await lifecycle_trace(host, runner)
        self.assertEqual(log, EXPECTED)
        self.assertIsNone(runner.task)
        self.assertIsNone(host.dynamic_ideas)

    async def test_private_poll_waits_after_start(self):
        host = fake_host()
        runner = runner_module.Runner(host)
        runner.main_loop = lambda: idle_main_loop(host.log)
        with patched(host):
            await runner.start()
            self.assertIsNotNone(host.private_poll._private_message_next_poll_at)
            await runner.stop()

    async def test_broken_schedule_does_not_keep_the_main_loop_down(self):
        # 日程是附属的：启动时整理它出错，主循环（评论、私信）照样起来
        host = fake_host()

        def broken():
            raise ValueError("日程文件坏了")

        host.day_schedules._mark_overdue_schedule_as_triggered_on_startup = broken
        runner = runner_module.Runner(host)
        runner.main_loop = lambda: idle_main_loop(host.log)
        with patched(host), patch.object(runner_module, "logger") as logger:
            await runner.start()
            await asyncio.sleep(0)
            self.assertTrue(host._running)
            self.assertIn("main_loop", host.log)
            self.assertIn("日程", logger.error.call_args[0][0])
            await runner.stop()

    async def test_no_cookie_means_no_background_work(self):
        host = fake_host(has_cookie=False)
        runner = runner_module.Runner(host)
        with patched(host):
            await runner.initialize()
        self.assertNotIn("check_cookie", host.log)
        self.assertNotIn("ensure_buvid", host.log)
        self.assertFalse(host._running)

    async def test_cookie_check_failure_still_starts(self):
        # 网络暂时不通不等于 Cookie 失效
        host = fake_host(cookie=(False, "检查失败: timeout"))
        runner = runner_module.Runner(host)
        runner.main_loop = lambda: idle_main_loop(host.log)
        with patched(host):
            await runner.initialize()
            self.assertTrue(host._running)
            await runner.stop()

    async def test_invalid_cookie_does_not_start(self):
        host = fake_host(cookie=(False, "Cookie 已失效"))
        runner = runner_module.Runner(host)
        with patched(host):
            await runner.initialize()
        self.assertFalse(host._running)

    async def test_start_clears_main_loop_left_by_a_hot_reload(self):
        host = fake_host()
        stale = asyncio.ensure_future(asyncio.Event().wait())
        stale.set_name(runner_module.MAIN_TASK_NAME)
        runner = runner_module.Runner(host)
        runner.main_loop = lambda: idle_main_loop(host.log)
        with patched(host):
            await runner.start()
            self.assertTrue(stale.cancelled())
            self.assertEqual(runner.task.get_name(), runner_module.MAIN_TASK_NAME)
            await runner.stop()


if __name__ == "__main__":
    unittest.main()
