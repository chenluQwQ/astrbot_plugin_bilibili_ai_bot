"""
AstrBot Plugin - Bilibili Bot 1.5.0
自动回复评论、好感度、记忆、心情、用户画像、主动视频、动态发布。
拆分版本：核心逻辑分布在 core/ 下的 Mixin 模块中。
"""
import sys
import io, os, time, asyncio, random, traceback
from datetime import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import ProviderRequest, LLMResponse
from .core.config import *
from .core import (
    UtilsMixin, LLMMixin, VisionMixin, MemoryMixin,
    AffectionMixin, PersonalityMixin, BilibiliAPIMixin,
    BangumiMixin, WebSearchMixin, VideoMixin, ReplyMixin,
    ProactiveMixin, DynamicMixin, ScheduleMixin, WeeklySummaryMixin, ShareMixin,
    PrivateMessageMixin, LiveDanmakuMixin, ConsolidationEngine, BiliBotMemoryAPI,
    EventRuntime, LayeredRuntime,
)

_ACTIVE_BILIBOT = None
_BILIBOT_MAIN_TASK_NAME = "astrbot_plugin_bilibili_ai_bot.main_loop"


def get_bilibili_ai_bot_api():
    """Return the active v2 memory/profile API for companion plugins."""
    bot = _ACTIVE_BILIBOT
    return getattr(bot, "memory_api", None) if bot is not None else None

_astrbot_site_packages = os.path.join(os.path.expanduser("~"), ".astrbot", "data", "site-packages")
if os.path.isdir(_astrbot_site_packages) and _astrbot_site_packages not in sys.path:
    sys.path.insert(0, _astrbot_site_packages)

@register("astrbot_plugin_bilibili_ai_bot","chenluQwQ","B站 AI Bot — 自动回复评论、好感度、记忆、心情、用户画像、主动视频、动态发布、LLM工具调用","1.5.0","https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot")
class BiliBiliBot(Star, UtilsMixin, LLMMixin, VisionMixin, MemoryMixin, AffectionMixin, PersonalityMixin, BilibiliAPIMixin, BangumiMixin, WebSearchMixin, VideoMixin, ReplyMixin, ProactiveMixin, DynamicMixin, ScheduleMixin, WeeklySummaryMixin, ShareMixin, PrivateMessageMixin, LiveDanmakuMixin):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._ensure_data_dir()
        self._running = False
        self._task = None
        self._start_lock = asyncio.Lock()
        self._memory_write_lock = asyncio.Lock()
        self._proactive_task = None
        self._bangumi_task = None
        self._cross_platform_activity = {}
        self._last_cookie_check = 0
        self._login_qrcode_key = None
        self._first_poll = True
        self._replied_at = set(self._load_json(REPLIED_AT_FILE, []))
        self._affection = self._load_json(AFFECTION_FILE, {})
        owner_mid = str(self.config.get("OWNER_MID", "") or "").strip()
        if owner_mid:
            self._affection[owner_mid] = 100
            self._save_json(AFFECTION_FILE, self._affection)
        self._memory = [self._normalize_memory_entry(m) for m in self._load_json(MEMORY_FILE, []) if isinstance(m, dict)]
        self._embed_client = None
        self._video_vision_client = None
        self._image_vision_client = None
        self._web_search_client = None
        self._consecutive_llm_failures = 0
        self._llm_circuit_open_until = 0.0
        self._llm_circuit_half_open = False
        self._llm_circuit_last_skip_log = 0.0
        self._reply_cooldown_until = 0.0
        self._proactive_windows, self._proactive_times, self._proactive_triggered = [], [], set()
        self._dynamic_task = None
        self._dynamic_times, self._dynamic_triggered = [], set()
        self._bangumi_times, self._bangumi_triggered, self._bangumi_update_checked = [], set(), False
        self._special_follow_task = None
        self._dynamic_watch_task = None
        self._dynamic_watch_times, self._dynamic_watch_triggered = [], set()
        self._bili_share_recent = {}
        self._pending_bili_shares = {}
        self._private_message_next_poll_at = 0.0
        self._private_message_backoff_seconds = 0
        self._private_message_success_streak = 0
        self._private_message_last_activity_at = 0.0
        self._private_message_last_warned_backoff = 0
        self.layered_runtime = LayeredRuntime(self.config, LAYERED_DB_FILE)
        try:
            action_timeout = float(
                self.config.get("BEHAVIOR_ACTION_TIMEOUT_SECONDS", 45) or 45
            )
        except (TypeError, ValueError):
            action_timeout = 45
        self.event_runtime = EventRuntime(
            observer=self.layered_runtime,
            action_timeout=action_timeout,
        )
        self._init_live_danmaku_state()
        self._special_follow_times, self._special_follow_triggered = [], set()
        self._log_environment_warnings()
        # ── 记忆清算引擎 & 外部接口 ──
        self._consolidation = ConsolidationEngine(self)
        self.memory_api = BiliBotMemoryAPI(self)
        global _ACTIVE_BILIBOT
        _ACTIVE_BILIBOT = self
        self._consolidation_task = None

        # 注册 FunctionTool 工具（结果回到 LLM 重新生成）
        from .core.tools import create_tools
        llm_tools = create_tools(self)
        self.layered_runtime.register_tools(llm_tools)
        self.context.add_llm_tools(*llm_tools)
        tool_names = ", ".join(tool.name for tool in llm_tools)
        logger.info(f"[BiliBot] LLM工具已精简注册: {len(llm_tools)} 个 ({tool_names})")

        # 注册 WebUI 控制面板
        from .core.webui_bridge import register_webui
        register_webui(self, self.context)

    async def initialize(self):
        """Start background work only after AstrBot has formally activated the plugin."""
        try:
            await self.layered_runtime.open()
            logger.info("[BiliBot] 四层运行服务已连接")
        except Exception as exc:
            # The established JSON/in-memory path remains usable when SQLite cannot
            # be opened; the runtime observer records no data until the next reload.
            logger.error(f"[BiliBot] 四层运行服务启动失败，已回退兼容主链: {exc}")
        else:
            try:
                await self._initialize_unified_memory()
            except Exception as exc:
                # 不拿用户已有的 JSON 记忆冒险；迁移失败只降级记忆存储，事件层照常运行。
                self._mark_memory_sync_pending(exc)
                logger.error(
                    f"[BiliBot] 统一记忆迁移失败，暂时继续使用 memory.json: {exc}"
                )
            try:
                await self._initialize_seen_videos()
            except Exception as exc:
                # 去重账本失败不应把已经成功的语义记忆迁移标成失败。
                logger.error(
                    f"[BiliBot] 永久视频去重账本迁移失败，已保留旧记录兜底: {exc}"
                )
        if not self._has_cookie():
            logger.warning("[BiliBot] Cookie未配置，后台任务未启动")
            return
        valid, msg = await self.check_cookie()
        if valid:
            await self._start_bot()
            logger.info("[BiliBot] 自动启动")
        elif "检查失败" in str(msg):
            # 网络暂时不可用不等于 Cookie 失效；仍启动主循环等待网络恢复。
            logger.warning(
                f"[BiliBot] Cookie 检查暂时失败（{msg}），仍启动后台任务等待网络恢复"
            )
            await self._start_bot()
        else:
            logger.warning("[BiliBot] Cookie无效")

    async def _start_bot(self):
        async with self._start_lock:
            if self._running and self._task and not self._task.done():
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
                and task.get_name() == _BILIBOT_MAIN_TASK_NAME
            ]
            for task in stale_tasks:
                task.cancel()
            if stale_tasks:
                await asyncio.gather(*stale_tasks, return_exceptions=True)
                logger.warning(
                    f"[BiliBot] 已清理 {len(stale_tasks)} 个热重载残留主循环"
                )

            await self._ensure_buvid()
            self._mark_overdue_schedule_as_triggered_on_startup()
            if self.config.get("ENABLE_PRIVATE_MESSAGES", False):
                private_state = self._load_json(PRIVATE_MESSAGE_STATE_FILE, {})
                if isinstance(private_state, dict) and private_state.get("initialized"):
                    try:
                        startup_delay = max(
                            60,
                            min(
                                3600,
                                int(
                                    self.config.get(
                                        "PRIVATE_MESSAGE_IDLE_POLL_INTERVAL", 180
                                    )
                                    or 180
                                ),
                            ),
                        )
                    except (TypeError, ValueError):
                        startup_delay = 180
                    self._private_message_next_poll_at = (
                        time.monotonic() + startup_delay
                    )
                    logger.info(
                        f"[BiliBot] 私信监听将在 {startup_delay} 秒启动，"
                        "避免重载后立即重试接口"
                    )
            self._running = True
            self._task = asyncio.create_task(
                self._main_loop(), name=_BILIBOT_MAIN_TASK_NAME
            )
            if self.config.get("ENABLE_LIVE_DANMAKU_REPLY", False):
                ok, message = await self._start_live_danmaku_listener()
                if not ok:
                    logger.warning(f"[BiliBot] 直播弹幕监听未启动: {message}")
            logger.info("[BiliBot] 启动")

    async def _stop_bot(self):
        await self._stop_live_danmaku_listener()
        async with self._start_lock:
            self._running = False
            task = self._task
            self._task = None
            if task and not task.done():
                task.cancel()
                if task is not asyncio.current_task():
                    await asyncio.gather(task, return_exceptions=True)
        if self._proactive_task and not self._proactive_task.done():
            self._proactive_task.cancel()
            self._proactive_task = None
        if self._dynamic_task and not self._dynamic_task.done():
            self._dynamic_task.cancel()
            self._dynamic_task = None
        if self._bangumi_task and not self._bangumi_task.done():
            self._bangumi_task.cancel()
            self._bangumi_task = None
        if self._special_follow_task and not self._special_follow_task.done():
            self._special_follow_task.cancel()
            self._special_follow_task = None
        if self._dynamic_watch_task and not self._dynamic_watch_task.done():
            self._dynamic_watch_task.cancel()
        self._dynamic_watch_task = None
        self._dynamic_watch_times, self._dynamic_watch_triggered = [], set()
        self._bili_share_recent = {}
        self._pending_bili_shares = {}
        self._private_message_next_poll_at = 0.0
        self._private_message_backoff_seconds = 0
        self._private_message_success_streak = 0
        self._private_message_last_activity_at = 0.0
        self._private_message_last_warned_backoff = 0
        if self._consolidation_task and not self._consolidation_task.done():
            self._consolidation_task.cancel()
            self._consolidation_task = None
        logger.info("[BiliBot] 停止")

    async def _run_consolidation_safe(self):
        """安全执行日终清算，捕获所有异常。"""
        try:
            logger.info("[BiliBot] 🌙 开始日终清算...")
            summary = await self._consolidation.run_daily()
            logger.info(f"[BiliBot] 🌙 日终清算完成:\n{summary}")
        except Exception as e:
            logger.error(f"[BiliBot] 日终清算异常: {e}", exc_info=True)

    async def _main_loop(self):
        logger.info("[BiliBot] 主循环开始")
        while self._running:
            try:
                await self._maybe_evolve_personality()
                await self._ensure_autonomous_daily_plan()
                h = datetime.now().hour
                ss = self.config.get("SLEEP_START", 2)
                se = self.config.get("SLEEP_END", 8)
                # 支持跨午夜的休眠区间（例如 23 → 7）。
                in_sleep = (ss <= h < se) if ss <= se else (h >= ss or h < se)
                if in_sleep:
                    # ── 日终清算：在睡眠时段触发 ──
                    if self._consolidation.should_run_today():
                        if self._consolidation_task is None or self._consolidation_task.done():
                            self._consolidation_task = asyncio.create_task(self._run_consolidation_safe())
                    # ── 周总结：日终清算已完成且不在运行时触发 ──
                    elif self._consolidation_task is None or self._consolidation_task.done():
                        try:
                            await self._maybe_daily_summary()
                        except Exception as e:
                            logger.error(f"[BiliBot] 日总结调度异常: {e}")
                        try:
                            await self._maybe_weekly_summary()
                        except Exception as e:
                            logger.error(f"[BiliBot] 周总结调度异常: {e}")
                    await asyncio.sleep(60)
                    continue
                ci = self.config.get("COOKIE_CHECK_INTERVAL", 6) * 3600
                if time.time() - self._last_cookie_check > ci:
                    await self._check_and_refresh_cookie()
                self._last_cookie_check = time.time()
                if self.config.get("ENABLE_PROACTIVE", False):
                    now_dt = datetime.now()
                    today_str = now_dt.strftime("%Y-%m-%d")
                    sched = self._load_json(SCHEDULE_FILE, {})
                    if sched.get("date") != today_str:
                        self._proactive_times, self._proactive_triggered = self._generate_daily_schedule()
                        logger.info(f"[BiliBot] 📅 新的一天！主动视频时间：{[f'{ph}:{pm:02d}' for ph,pm in self._proactive_times]}")
                    elif not self._proactive_times:
                        self._proactive_times, self._proactive_triggered = self._load_or_generate_schedule()
                    for ph, pm in self._proactive_times:
                        key = f"{ph}:{pm:02d}"
                        if key not in self._proactive_triggered and self._schedule_slot_due(now_dt, ph, pm):
                            if self._proactive_task is None or self._proactive_task.done():
                                self._proactive_task = asyncio.create_task(self._run_proactive())
                                self._proactive_triggered.add(key)
                                self._save_schedule_state(self._proactive_times, self._proactive_triggered)
                                trigger_log = self._load_json(PROACTIVE_TRIGGER_LOG_FILE, [])
                                trigger_log.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "proactive_video", "scheduled": key, "status": "triggered"})
                                self._save_json(PROACTIVE_TRIGGER_LOG_FILE, trigger_log[-200:])
                                logger.info(f"[BiliBot] 🎯 触发主动视频（{key}）")
                                break
                if self.config.get("ENABLE_DYNAMIC", False):
                    now_dt = datetime.now()
                    today_str = now_dt.strftime("%Y-%m-%d")
                    sched = self._load_json(DYNAMIC_SCHEDULE_FILE, {})
                    if sched.get("date") != today_str:
                        self._dynamic_times, self._dynamic_triggered = self._generate_dynamic_schedule()
                        logger.info(f"[BiliBot] 📅 动态时间：{[f'{dh}:{dm:02d}' for dh,dm in self._dynamic_times]}")
                    elif not self._dynamic_times:
                        self._dynamic_times, self._dynamic_triggered = self._load_or_generate_dynamic_schedule()
                    for dh, dm in self._dynamic_times:
                        key = f"{dh}:{dm:02d}"
                        if key not in self._dynamic_triggered and self._schedule_slot_due(now_dt, dh, dm):
                            if self._dynamic_task is None or self._dynamic_task.done():
                                self._dynamic_task = asyncio.create_task(self._run_dynamic())
                                self._dynamic_triggered.add(key)
                                self._save_dynamic_schedule_state(self._dynamic_times, self._dynamic_triggered)
                                logger.info(f"[BiliBot] 📢 触发动态发布（{key}）")
                                break
                if self.config.get("ENABLE_PRIVATE_MESSAGES", False):
                    await self._poll_private_messages()
                if self.config.get("ENABLE_REPLY", True):
                    await self._poll_unified()
                # 番剧主动看番（随机时间调度，与视频/动态一致）
                if self.config.get("ENABLE_BANGUMI", False) and self.config.get("BANGUMI_PROACTIVE", False):
                    now_dt = datetime.now()
                    today_str = now_dt.strftime("%Y-%m-%d")
                    bsched = self._load_json(BANGUMI_SCHEDULE_FILE, {})
                    if bsched.get("date") != today_str:
                        self._bangumi_times, self._bangumi_triggered, self._bangumi_update_checked = self._generate_bangumi_schedule()
                        logger.info(f"[BiliBot] 📅 番剧时间：{[f'{bh}:{bm:02d}' for bh,bm in self._bangumi_times]}")
                    elif not self._bangumi_times:
                        self._bangumi_times, self._bangumi_triggered, self._bangumi_update_checked = self._load_or_generate_bangumi_schedule()
                    if self._bangumi_task is None or self._bangumi_task.done():
                        # 每天第一个番剧时间点先检查追番更新
                        if not self._bangumi_update_checked:
                            for bh, bm in self._bangumi_times:
                                key = f"{bh}:{bm:02d}"
                                if key not in self._bangumi_triggered and self._schedule_slot_due(now_dt, bh, bm):
                                    self._bangumi_update_checked = True
                                    self._bangumi_task = asyncio.create_task(self._check_bangumi_updates())
                                    self._bangumi_triggered.add(key)
                                    self._save_bangumi_schedule_state(self._bangumi_times, self._bangumi_triggered, True)
                                    trigger_log = self._load_json(PROACTIVE_TRIGGER_LOG_FILE, [])
                                    trigger_log.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "bangumi_update_check", "scheduled": key, "status": "triggered"})
                                    self._save_json(PROACTIVE_TRIGGER_LOG_FILE, trigger_log[-200:])
                                    logger.info(f"[BiliBot] 📺 触发每日追番更新检查（{key}）")
                                    break
                        else:
                            for bh, bm in self._bangumi_times:
                                key = f"{bh}:{bm:02d}"
                                if key not in self._bangumi_triggered and self._schedule_slot_due(now_dt, bh, bm):
                                    self._bangumi_task = asyncio.create_task(self._run_bangumi())
                                    self._bangumi_triggered.add(key)
                                    self._save_bangumi_schedule_state(self._bangumi_times, self._bangumi_triggered, self._bangumi_update_checked)
                                    trigger_log = self._load_json(PROACTIVE_TRIGGER_LOG_FILE, [])
                                    trigger_log.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "bangumi_watch", "scheduled": key, "status": "triggered"})
                                    self._save_json(PROACTIVE_TRIGGER_LOG_FILE, trigger_log[-200:])
                                    logger.info(f"[BiliBot] 🎬 触发主动看番（{key}）")
                                    break
                # 关注者动态图文巡视（每次任务媒体上下文独立，仅留文字摘要）
                if self.config.get("ENABLE_DYNAMIC_WATCH", False):
                    now_dt = datetime.now()
                    for dh, dm in getattr(self, "_dynamic_watch_times", []):
                        key = f"{dh}:{dm:02d}"
                        if key not in getattr(self, "_dynamic_watch_triggered", set()) and self._schedule_slot_due(now_dt, dh, dm):
                            if self._dynamic_watch_task is None or self._dynamic_watch_task.done():
                                self._dynamic_watch_task = asyncio.create_task(self._run_dynamic_watch())
                                self._dynamic_watch_triggered.add(key)
                                self._save_dynamic_watch_schedule_state(self._dynamic_watch_times, self._dynamic_watch_triggered)
                                trigger_log = self._load_json(PROACTIVE_TRIGGER_LOG_FILE, [])
                                trigger_log.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "dynamic_watch", "scheduled": key, "status": "triggered"})
                                self._save_json(PROACTIVE_TRIGGER_LOG_FILE, trigger_log[-200:])
                                logger.info(f"[BiliBot] 📰 触发关注动态巡视（{key}）")
                                break

                # 特别关注定时巡视
                if self.config.get("SPECIAL_FOLLOW_ENABLED", False):
                    now_dt = datetime.now()
                    today_str = now_dt.strftime("%Y-%m-%d")
                    sfsched = self._load_json(SPECIAL_FOLLOW_SCHEDULE_FILE, {})
                    if sfsched.get("date") != today_str:
                        self._special_follow_times, self._special_follow_triggered = self._generate_special_follow_schedule()
                        logger.info(f"[BiliBot] 📅 特关时间：{[f'{sh}:{sm:02d}' for sh,sm in self._special_follow_times]}")
                    elif not self._special_follow_times:
                        self._special_follow_times, self._special_follow_triggered = self._load_or_generate_special_follow_schedule()
                    for sh, sm in self._special_follow_times:
                        key = f"{sh}:{sm:02d}"
                        if key not in self._special_follow_triggered and self._schedule_slot_due(now_dt, sh, sm):
                            if self._special_follow_task is None or self._special_follow_task.done():
                                self._special_follow_task = asyncio.create_task(self._run_special_follow())
                                self._special_follow_triggered.add(key)
                                self._save_special_follow_schedule_state(self._special_follow_times, self._special_follow_triggered)
                                trigger_log = self._load_json(PROACTIVE_TRIGGER_LOG_FILE, [])
                                trigger_log.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "special_follow", "scheduled": key, "status": "triggered"})
                                self._save_json(PROACTIVE_TRIGGER_LOG_FILE, trigger_log[-200:])
                                logger.info(f"[BiliBot] ⭐ 触发特别关注巡视（{key}）")
                                break
                await asyncio.sleep(self.config.get("POLL_INTERVAL", 20))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[BiliBot] 主循环出错: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(30)
        self._running = False

    async def _check_and_refresh_cookie(self):
        valid, info = await self.check_cookie()
        if valid:
            logger.info(f"[BiliBot] Cookie OK: {info}")
            return
        logger.warning(f"[BiliBot] Cookie 失效: {info}")
        if self.config.get("COOKIE_AUTO_REFRESH", True):
            ok, msg = await self.refresh_cookie()
            logger.info(f"[BiliBot] 刷新{'成功' if ok else '失败'}: {msg}")

    async def terminate(self):
        await self._stop_bot()
        extension_registry = getattr(self, "_bilibot_extension_registry", None)
        if extension_registry is not None:
            try:
                await extension_registry.close()
            except Exception as exc:
                logger.warning(f"[BiliBot] 扩展 Host 关闭异常: {exc}")
        try:
            await self.event_runtime.close()
        except Exception as exc:
            logger.warning(f"[BiliBot] 动作队列关闭异常: {exc}")
        try:
            await self.layered_runtime.close()
        except Exception as exc:
            logger.warning(f"[BiliBot] 四层运行服务关闭异常: {exc}")
        # LLM 工具不在此手动注销：按名称删除可能误删其他插件覆盖注册的同名工具；
        # AstrBot 会按 handler_module_path 清理当前插件的工具。
        global _ACTIVE_BILIBOT
        if _ACTIVE_BILIBOT is self:
            _ACTIVE_BILIBOT = None
        self._cleanup_temp_files()
        logger.info("[BiliBot] 已停用")
    # ===== QQ命令 =====
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=850)
    async def on_group_bili_share(self, event: AstrMessageEvent):
        async for result in self._handle_bili_share(event, trigger_mode="auto"):
            yield result

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=850)
    async def on_private_bili_share(self, event: AstrMessageEvent):
        handled = False
        async for result in self._handle_bili_share(event, trigger_mode="auto"):
            handled = True
            yield result
        if handled:
            event.stop_event()

    @filter.command("bili解析")
    async def cmd_bili_parse(self, event: AstrMessageEvent):
        parts = (event.message_str or "").strip().split(maxsplit=1)
        target_text = parts[1].strip() if len(parts) >= 2 else self._collect_share_text(
            event,
            include_reply=True,
        )
        async for result in self._handle_bili_share(
            event,
            text_override=target_text,
            trigger_mode="manual",
            show_errors=True,
        ):
            yield result

    @filter.command("bili登录")
    async def cmd_login(self, event: AstrMessageEvent):
        qr_url, qrcode_key = await self._qr_login_generate()
        if not qr_url:
            yield event.plain_result("❌ 生成二维码失败")
            return
        self._login_qrcode_key = qrcode_key
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(qr_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        qr_path = os.path.join(DATA_DIR, "login_qr.png")
        with open(qr_path, "wb") as f:
            f.write(buf.getvalue())
        yield event.chain_result([Plain("📱 请用B站APP扫描下方二维码：\n扫码后发送 /bili确认"), Image.fromFileSystem(qr_path)])
    @filter.command("bili确认")
    async def cmd_login_confirm(self, event: AstrMessageEvent):
        if not self._login_qrcode_key:
            yield event.plain_result("❌ 没有待确认的登录")
            return
        for i in range(3):
            code,msg,cookies = await self._qr_login_poll(self._login_qrcode_key)
            if code==0:
                for k,ck in [("SESSDATA","SESSDATA"),("BILI_JCT","bili_jct"),("DEDE_USER_ID","DedeUserID"),("REFRESH_TOKEN","REFRESH_TOKEN")]:
                    if cookies.get(ck):
                        self.config[k]=cookies[ck]
                self.config.save_config()
                self._login_qrcode_key=None
                valid,info=await self.check_cookie()
                yield event.plain_result(f"✅ 登录成功！\n{info}")
                if not self._running:
                    await self._start_bot()
                    yield event.plain_result("🚀 后台任务已自动启动")
                return
            elif code==86090:
                yield event.plain_result(f"📱 {msg}")
                await asyncio.sleep(2)
            elif code==86101:
                yield event.plain_result(f"⏳ {msg}")
                await asyncio.sleep(3)
            elif code==86038:
                self._login_qrcode_key=None
                yield event.plain_result(f"❌ {msg}，请重新 /bili登录")
                return
            else:
                yield event.plain_result(f"❌ {msg}")
                return
        yield event.plain_result("⏳ 还没确认成功，请在手机上确认后再发 /bili确认")
    @filter.command("bili状态")
    async def cmd_status(self, event: AstrMessageEvent):
        valid,info=await self.check_cookie()
        mood,_=self._get_today_mood()
        env = self._get_environment_status()
        cmd_status = env["external_commands"]
        feature_status = env["features"]
        mc=len(self._memory)
        pc=len(self._load_json(USER_PROFILE_FILE,{}))
        pmc=len(self._load_json(PERMANENT_MEMORY_FILE,[]))
        evo=self._load_json(PERSONALITY_FILE,{})
        evo_ver=evo.get("version",0)
        evo_last=evo.get("last_evolve","从未")
        wl=self._load_json(WATCH_LOG_FILE,[])
        today_watched=len([l for l in wl if l.get("time","").startswith(datetime.now().strftime("%Y-%m-%d"))])
        dl=self._load_json(DYNAMIC_LOG_FILE,[])
        today_dynamic=len([l for l in dl if l.get("time","").startswith(datetime.now().strftime("%Y-%m-%d"))])
        schedule = self._get_schedule_snapshot()
        live_memory_count = sum(1 for m in self._memory if self._match_memory_type(m, {"live"}))
        owner_recommend_delivery = {
            "private_message": "B站私信",
            "comment": "评论区@",
            "both": "私信+评论区",
            "off": "关闭",
        }.get(self._owner_recommend_delivery(), "B站私信")
        private_scope = {
            "owner": "仅主人",
            "whitelist": "主人+白名单",
            "all": "全部安全用户",
        }.get(
            str(self.config.get("PRIVATE_MESSAGE_REPLY_SCOPE", "owner") or "owner").lower(),
            "关闭",
        )
        try:
            private_active_interval = max(
                60,
                int(self.config.get("PRIVATE_MESSAGE_POLL_INTERVAL", 60) or 60),
            )
            private_idle_interval = max(
                private_active_interval,
                int(
                    self.config.get("PRIVATE_MESSAGE_IDLE_POLL_INTERVAL", 180)
                    or 180
                ),
            )
        except (TypeError, ValueError):
            private_active_interval, private_idle_interval = 60, 180
        live_status = self._live_danmaku_status()
        runtime_status = await self.event_runtime.snapshot()
        runtime_priorities = runtime_status.get("event_priorities", {})
        runtime_actions = runtime_status.get("action_states", {})
        lines = [
            "📺 BiliBot 1.5.0 状态","━━━━━━━━━━━━",f"🍪 {info}",
            f"{'🟢 运行中' if self._running else '🔴 未运行'}",
            f"🧠 记忆:{mc}条 | 💎永久:{pmc}条 | 👤档案:{pc}个",
            f"   📊 今日:{sum(1 for m in self._memory if m.get('level')=='today')} | 近期:{sum(1 for m in self._memory if m.get('level')=='recent')} | 长期:{sum(1 for m in self._memory if m.get('level')=='long_term')} | 老化:{sum(1 for m in self._memory if m.get('aged'))}",
            f"🎭 心情:{mood} | 🌱性格v{evo_ver}（{evo_last[:10]}）",
            f"📹 今日已看:{today_watched}个视频 | 📝动态:{today_dynamic}条",
            f"🎯 主动时间:{', '.join(schedule['proactive_times']) if schedule['proactive_times'] else '未生成'}",
            f"📢 动态时间:{', '.join(schedule['dynamic_times']) if schedule['dynamic_times'] else '未生成'}",
            f"⭐ 特关时间:{', '.join(schedule['special_follow_times']) if schedule.get('special_follow_times') else '未启用'}",
            f"✅ 已触发主动:{', '.join(schedule['proactive_triggered']) if schedule['proactive_triggered'] else '暂无'}",
            f"✅ 已触发动态:{', '.join(schedule['dynamic_triggered']) if schedule['dynamic_triggered'] else '暂无'}",
            f"回复:{'✅' if self.config.get('ENABLE_REPLY',True) else '❌'} 好感:{'✅' if self.config.get('ENABLE_AFFECTION',True) else '❌'} 心情:{'✅' if self.config.get('ENABLE_MOOD',True) else '❌'}",
            f"✉️ B站私信:{'✅' if self.config.get('ENABLE_PRIVATE_MESSAGES',False) else '❌'} 回复:{'✅' if self.config.get('PRIVATE_MESSAGE_AUTO_REPLY',True) else '❌'}({private_scope}) 模型按需查询:{'✅' if self.config.get('PRIVATE_MESSAGE_BILI_SEARCH_ENABLED',True) else '❌'} 视频先看:{'✅' if self.config.get('PRIVATE_MESSAGE_AUTO_WATCH_VIDEO',True) else '❌'} 上下文重置:new 间隔:活跃{private_active_interval}/空闲{private_idle_interval}秒 危险拉黑:{'✅' if self.config.get('PRIVATE_MESSAGE_AUTO_BLOCK',True) else '❌'}",
            f"🎙️ 直播间互动:{'运行中' if live_status['running'] else ('已开启未运行' if live_status['enabled'] else '关闭')} | 房间:{live_status['room_id'] or '未配置'} | 轮询:{live_status['poll_interval']:g}秒 | 冷却:{live_status['cooldown']:g}秒 | 待回应:{live_status['pending_count']}",
            f"主动:{'✅' if self.config.get('ENABLE_PROACTIVE',False) else '❌'} 动态:{'✅' if self.config.get('ENABLE_DYNAMIC',False) else '❌'} 特关:{'✅' if self.config.get('SPECIAL_FOLLOW_ENABLED',False) else '❌'} 演化:{'✅' if self.config.get('ENABLE_PERSONALITY_EVOLUTION',False) else '❌'} 工具:{'✅' if self.config.get('ENABLE_LLM_TOOLS',True) else '❌'}",
            f"✉️ 主人推荐:{owner_recommend_delivery} | 最低{self.config.get('RECOMMEND_OWNER_MIN_SCORE', 8)}分 | 每日上限:{self.config.get('RECOMMEND_OWNER_DAILY_LIMIT', 1)}",
            f"🔍 联网搜索:{'✅ '+feature_status['web_search_backend'] if feature_status['web_search'] else '❌'} 判断模型:{'✅' if feature_status['web_search_judge'] else '❌(用主模型)'}",
            f"🎨 动态配图:{'✅ '+feature_status['image_gen_backend'] if feature_status['dynamic_image_generation'] else '❌'}{'（仅手动命令）' if feature_status.get('image_gen_backend') == 'novelai' else ''}",
            f"🧭 看片来源:关注 → 搜索(Bot自主决定) → 视频池({self._format_video_pool_config()})",
            f"🔗 群/私聊解析:{'✅' if self.config.get('ENABLE_BILI_SHARE_PARSE', False) else '❌'} 发原视频:{'✅' if self.config.get('BILI_SHARE_PARSE_SEND_VIDEO', True) else '❌'}",
            f"   解析触发 自动:{'✅' if self.config.get('BILI_SHARE_PARSE_AUTO_TRIGGER_ENABLED',True) else '❌'} 手动:{'✅' if self.config.get('BILI_SHARE_PARSE_MANUAL_TRIGGER_ENABLED',True) else '❌'} LLM:{'✅' if self.config.get('BILI_SHARE_PARSE_LLM_TRIGGER_ENABLED',True) else '❌'}",
            f"🎙️ 直播记忆:{live_memory_count}条 | 外部接口:v{self.memory_api.api_version}",
            f"🧱 统一调度:事件处理中{runtime_status['event_states']['processing']} | 队列{runtime_status.get('queue_depth', 0)} | 动作成功{runtime_actions.get('succeeded', 0)} | 失败{runtime_actions.get('failed', 0)} | 结果未知{runtime_actions.get('unknown', 0)}",
            f"🧭 近期优先级:管理员{runtime_priorities.get('admin', 0)} | @提及{runtime_priorities.get('direct_mention', 0)} | 对话{runtime_priorities.get('active_conversation', 0)} | 兴趣{runtime_priorities.get('interesting', 0)} | 普通{runtime_priorities.get('normal', 0)} | 后台{runtime_priorities.get('background', 0)}",
            f"🧭 看片筛选:{'✅' if self.config.get('ENABLE_PROACTIVE_LLM_PREFILTER', False) else '❌'} 最多拒绝:{self.config.get('PROACTIVE_LLM_PREFILTER_MAX_REJECTS', 3)}次 | 分区口味:{self._taste_window_days()}天",
            f"🎞️ 视频分段:{self.config.get('VIDEO_SEGMENT_MINUTES', 5)}分钟/段，最多{self.config.get('VIDEO_SEGMENT_MAX_COUNT', 10)}段",
            f"视频视觉Provider:{'✅' if env['llm']['video_provider'] else '❌'} 独立API:{'✅' if env['llm']['video_api'] else '❌'}",
            f"图片识别Provider:{'✅' if env['llm']['image_provider'] else '❌'} 独立API:{'✅' if env['llm']['image_api'] else '❌'}",
            f"外部命令 yt-dlp:{'✅' if cmd_status['yt-dlp'] else '❌'} ffmpeg:{'✅' if cmd_status['ffmpeg'] else '❌'} ffprobe:{'✅' if cmd_status['ffprobe'] else '❌'}",
            f"主动视频直读/截帧:{'✅' if feature_status['proactive_video_media'] else '❌'} 纯文本回退:{'✅' if feature_status['proactive_video_fallback_text'] else '❌'}",
        ]
        yield event.plain_result("\n".join(lines))
    @filter.command("bili直播")
    async def cmd_live_danmaku(self, event: AstrMessageEvent):
        """管理 BiliBot 本体进入指定直播间后的弹幕互动。"""
        raw = event.message_str.strip().split(maxsplit=2)
        action = raw[1].strip().lower() if len(raw) >= 2 else "状态"
        status = self._live_danmaku_status()
        if action in {"状态", "status"}:
            errors = []
            if status["listener_error"]:
                errors.append(f"监听：{status['listener_error']}")
            if status["send_error"]:
                errors.append(f"发送：{status['send_error']}")
            yield event.plain_result(
                "🎙️ BiliBot 直播间弹幕互动\n"
                f"开关：{'开启' if status['enabled'] else '关闭'}\n"
                f"监听：{'运行中' if status['running'] else '未运行'}"
                f"（{'已建立当前位置' if status['initialized'] else '等待首次轮询'}）\n"
                f"房间：{status['room_id'] or '未配置'}\n"
                f"轮询：{status['poll_interval']:g}秒 | 回复冷却：{status['cooldown']:g}秒\n"
                f"本次捕获：{status['recent_count']}条 | 待回复：{status['pending_count']}条\n"
                f"最近弹幕：{status['last_event'] or '暂无'}\n"
                f"发送退避：{status['send_backoff_seconds']}秒\n"
                f"最近错误：{'；'.join(errors) if errors else '无'}"
            )
            return
        if action in {"房间", "room"}:
            room_text = raw[2].strip() if len(raw) >= 3 else ""
            if not room_text.isdigit() or int(room_text) <= 0:
                yield event.plain_result("用法：/bili直播 房间 <直播间号>")
                return
            was_running = self._live_listener_running()
            if was_running:
                await self._stop_live_danmaku_listener()
            self.config["LIVE_DANMAKU_ROOM_ID"] = int(room_text)
            self.config.save_config()
            if was_running or self.config.get("ENABLE_LIVE_DANMAKU_REPLY", False):
                ok, message = await self._start_live_danmaku_listener()
                yield event.plain_result(
                    f"直播间已设为 {room_text}。{'✅ ' if ok else '⚠️ '}{message}"
                )
            else:
                yield event.plain_result(
                    f"直播间已设为 {room_text}；用 /bili直播 开始 启动监听。"
                )
            return
        if action in {"开始", "启动", "start"}:
            self.config["ENABLE_LIVE_DANMAKU_REPLY"] = True
            self.config.save_config()
            ok, message = await self._start_live_danmaku_listener()
            yield event.plain_result(("✅ " if ok else "⚠️ ") + message)
            return
        if action in {"停止", "stop"}:
            self.config["ENABLE_LIVE_DANMAKU_REPLY"] = False
            self.config.save_config()
            await self._stop_live_danmaku_listener()
            yield event.plain_result("已停止 BiliBot 直播间弹幕互动。")
            return
        if action in {"测试", "test"}:
            text = raw[2].strip() if len(raw) >= 3 else ""
            if not text:
                yield event.plain_result("用法：/bili直播 测试 <弹幕内容>")
                return
            sent = await self._send_live_danmaku_text(text)
            yield event.plain_result(
                f"✅ 已发送 {sent} 条测试弹幕。"
                if sent
                else f"❌ 发送失败：{self._live_send_last_error or '未知错误'}"
            )
            return
        yield event.plain_result(
            "用法：\n"
            "/bili直播 状态\n"
            "/bili直播 房间 <房间号>\n"
            "/bili直播 开始\n"
            "/bili直播 停止\n"
            "/bili直播 测试 <文字>"
        )
    @filter.command("bili计划")
    async def cmd_schedule(self, event: AstrMessageEvent):
        schedule = self._get_schedule_snapshot()
        lines = [
            f"📅 今日计划：{schedule['date']}",
            "━━━━━━━━━━━━",
            f"🎯 主动看视频时间：{', '.join(schedule['proactive_times']) if schedule['proactive_times'] else '未生成'}",
            f"✅ 已触发主动：{', '.join(schedule['proactive_triggered']) if schedule['proactive_triggered'] else '暂无'}",
            f"📢 动态发布时间：{', '.join(schedule['dynamic_times']) if schedule['dynamic_times'] else '未生成'}",
            f"✅ 已触发动态：{', '.join(schedule['dynamic_triggered']) if schedule['dynamic_triggered'] else '暂无'}",
            f"🎬 看番时间：{', '.join(schedule['bangumi_times']) if schedule.get('bangumi_times') else '未生成'}",
            f"✅ 已触发看番：{', '.join(schedule['bangumi_triggered']) if schedule.get('bangumi_triggered') else '暂无'}",
            f"⭐ 特关巡视时间：{', '.join(schedule['special_follow_times']) if schedule.get('special_follow_times') else '未启用'}",
            f"✅ 已触发特关：{', '.join(schedule['special_follow_triggered']) if schedule.get('special_follow_triggered') else '暂无'}",
        ]
        yield event.plain_result("\n".join(lines))
    @filter.command("bili分区")
    async def cmd_regions(self, event: AstrMessageEvent):
        """查看B站分区列表及编号，用于配置视频池"""
        lines = ["📂 B站分区列表", "━━━━━━━━━━━━",
                  "视频池可直接填中文：热门、推荐、每周必看、入站必刷",
                  "分区示例：排行榜:游戏 / 最新:单机游戏 / 游戏 / 单机游戏",
                  "仍兼容旧写法：ranking:4 / newlist:17；多个用逗号分隔", ""]
        for rid, zone in BILI_ZONES.items():
            lines.append(f"📁 {zone['name']}（可填：{zone['name']} / 排行榜:{zone['name']}，rid:{rid}）")
            if zone["children"]:
                subs = [f"{name}（最新:{name}，tid:{tid}）" for tid, name in zone["children"].items()]
                lines.append("  └ " + "、".join(subs))
        text = "\n".join(lines)
        if len(text) > 2000:
            mid_idx = len(lines) // 2
            yield event.plain_result("\n".join(lines[:mid_idx]))
            yield event.plain_result("\n".join(lines[mid_idx:]))
        else:
            yield event.plain_result(text)
    @filter.command("bili启动")
    async def cmd_start(self, event: AstrMessageEvent):
        if self._running:
            yield event.plain_result("⚠️ 已在运行")
            return
        if not self._has_cookie():
            yield event.plain_result("❌ 请先 /bili登录")
            return
        await self._start_bot()
        yield event.plain_result("🚀 已启动！")
    @filter.command("bili停止")
    async def cmd_stop(self, event: AstrMessageEvent):
        if not self._running:
            yield event.plain_result("⚠️ 没在运行")
            return
        await self._stop_bot()
        yield event.plain_result("⏹️ 已停止")
    @filter.command("bili主动")
    async def cmd_proactive(self, event: AstrMessageEvent):
        if not self._has_cookie():
            yield event.plain_result("❌ 请先 /bili登录")
            return
        if not self.config.get("ENABLE_PROACTIVE", False):
            yield event.plain_result("⚠️ 当前未开启主动看视频功能，请先用 /bili开关 主动")
            return
        if self._proactive_task is not None and not self._proactive_task.done():
            yield event.plain_result("⏳ 已有主动看视频任务在运行")
            return
        self._proactive_task = asyncio.create_task(self._run_proactive(max_watch=1))
        trigger_log = self._load_json(PROACTIVE_TRIGGER_LOG_FILE, [])
        trigger_log.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "manual_command",
            "scheduled": "bili主动",
            "status": "triggered",
        })
        self._save_json(PROACTIVE_TRIGGER_LOG_FILE, trigger_log[-200:])
        yield event.plain_result("🎯 已手动触发一次主动看视频")
    async def _tool_bili_watch_videos_result(self) -> str:
        if not self._has_cookie():
            return "未登录B站，无法执行主动看视频。请先使用 /bili登录 完成扫码登录。"
        if not self.config.get("ENABLE_PROACTIVE", False):
            return "主动看视频功能当前未开启。请先使用 /bili开关 主动 开启。"
        if self._proactive_task is not None and not self._proactive_task.done():
            return "已有主动看视频任务正在运行，无需重复触发。"
        self._proactive_task = asyncio.create_task(self._run_proactive(max_watch=1))
        trigger_log = self._load_json(PROACTIVE_TRIGGER_LOG_FILE, [])
        trigger_log.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "llm_tool",
            "scheduled": "bili_watch_videos",
            "status": "triggered",
        })
        self._save_json(PROACTIVE_TRIGGER_LOG_FILE, trigger_log[-200:])
        return "已在后台触发一次主动看B站视频流程。稍后可用 /bili日志 视频 查看结果。"
    def _bili_toggle_items(self):
        return {
            "回复": "ENABLE_REPLY",
            "私信": "ENABLE_PRIVATE_MESSAGES",
            "私信回复": "PRIVATE_MESSAGE_AUTO_REPLY",
            "私信拉黑": "PRIVATE_MESSAGE_AUTO_BLOCK",
            "直播回复": "ENABLE_LIVE_DANMAKU_REPLY",
            "主动": "ENABLE_PROACTIVE",
            "动态": "ENABLE_DYNAMIC",
            "好感": "ENABLE_AFFECTION",
            "心情": "ENABLE_MOOD",
            "演化": "ENABLE_PERSONALITY_EVOLUTION",
            "工具": "ENABLE_LLM_TOOLS",
            "解析": "ENABLE_BILI_SHARE_PARSE",
            "自动解析": "BILI_SHARE_PARSE_AUTO_TRIGGER_ENABLED",
            "手动解析": "BILI_SHARE_PARSE_MANUAL_TRIGGER_ENABLED",
            "LLM解析": "BILI_SHARE_PARSE_LLM_TRIGGER_ENABLED",
            "解析视频": "BILI_SHARE_PARSE_SEND_VIDEO",
            "筛选": "ENABLE_PROACTIVE_LLM_PREFILTER",
            "点赞": "PROACTIVE_LIKE",
            "投币": "PROACTIVE_COIN",
            "收藏": "PROACTIVE_FAV",
            "关注": "PROACTIVE_FOLLOW",
            "评论": "PROACTIVE_COMMENT",
        }

    @filter.command("bili开关")
    async def cmd_toggle(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        tm = self._bili_toggle_items()
        if len(parts)<2:
            lines = ["可切换功能："] + [f"  {n} ({'✅' if self.config.get(k,True) else '❌'})" for n,k in tm.items()] + ["","用法: /bili开关 回复","      /bili开关 全部 ← 一键开/关所有"]
            yield event.plain_result("\n".join(lines))
            return
        name=parts[1].strip()

        # ── 一键全部开/关 ──
        if name == "全部":
            # 私信监听和真实拉黑不纳入“一键全部”，避免误开启外部写操作。
            bulk_keys = [
                key for label, key in tm.items()
                if label not in {"私信", "私信回复", "私信拉黑", "直播回复"}
            ]
            # 任一主功能开着 → 全关；全关了 → 全开
            any_on = any(self.config.get(k, True) for k in bulk_keys)
            new_state = not any_on
            for k in bulk_keys:
                self.config[k] = new_state
            self.config.save_config()
            emoji = "✅ 全部开启" if new_state else "❌ 全部关闭"
            yield event.plain_result(
                f"{emoji}（{len(bulk_keys)}项；B站私信与私信拉黑需单独切换）"
            )
            return

        key=tm.get(name)
        if not key:
            yield event.plain_result(f"❌ 不认识：{name}")
            return
        cur=self.config.get(key,True)
        self.config[key]=not cur
        self.config.save_config()
        if key == "ENABLE_LIVE_DANMAKU_REPLY":
            if not cur and self._running:
                ok, message = await self._start_live_danmaku_listener()
                if not ok:
                    yield event.plain_result(f"直播回复: ⚠️ 已开启，但{message}")
                    return
            elif cur:
                await self._stop_live_danmaku_listener()
        yield event.plain_result(f"{name}: {'✅ 已开启' if not cur else '❌ 已关闭'}")
    @filter.command("bili刷新")
    async def cmd_refresh_cookie(self, event: AstrMessageEvent):
        yield event.plain_result("🔄 刷新中...")
        _,msg=await self.refresh_cookie()
        yield event.plain_result(msg)
    @filter.command("bili记忆")
    async def cmd_memory(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=2)
        type_alias = {
            "交流": {"chat"},
            "聊天": {"chat"},
            "评论": {"chat"},
            "视频": {"video"},
            "观影": {"video"},
            "动态": {"dynamic"},
            "直播": {"live"},
            "总结": {"user_summary"},
            "压缩": {"user_summary"},
        }
        if len(parts)<2:
            mc=len(self._memory)
            chat_count = len([m for m in self._memory if self._match_memory_type(m, {"chat"})])
            video_count = len([m for m in self._memory if self._match_memory_type(m, {"video"})])
            dynamic_count = len([m for m in self._memory if self._match_memory_type(m, {"dynamic"})])
            live_count = len([m for m in self._memory if self._match_memory_type(m, {"live"})])
            user_summary_count = len([m for m in self._memory if self._match_memory_type(m, {"user_summary"})])
            lvl = self._consolidation.get_stats()
            level_line = f"📊 级别: 今日:{lvl['today']} | 近期:{lvl['recent']} | 长期:{lvl['long_term']} | 老化:{lvl['aged']}"
            if lvl.get('no_level', 0) > 0:
                level_line += f" | ⚠无级别:{lvl['no_level']}"
            yield event.plain_result(
                "🧠 记忆统计\n"
                f"总计:{mc}\n"
                f"交流:{chat_count} | 视频:{video_count} | 动态:{dynamic_count} | 直播:{live_count} | 用户总结:{user_summary_count}\n"
                f"{level_line}\n\n"
                "用法:\n"
                "/bili记忆 <关键词>\n"
                "/bili记忆 <关键词> 视频 ← 只搜视频记忆\n"
                "/bili记忆 <关键词> 动态 ← 只搜动态记忆\n"
                "/bili记忆 <关键词> 直播 ← 只搜直播记忆\n"
                "/bili记忆 <关键词> 交流 ← 只搜交流记忆"
            )
            return
        query=parts[1]
        arg=parts[2] if len(parts)>2 else None
        source = None
        memory_types = None
        if arg:
            if arg == "all":
                source = None
            elif arg == "bilibili":
                source = arg
            elif arg in type_alias:
                memory_types = type_alias[arg]
            else:
                source = arg
        results = await self._search_memories(query, limit=5, source=source, memory_types=memory_types)
        if not results:
            yield event.plain_result(f"🧠 没找到「{query}」的记忆")
            return
        suffix = f"（{arg}）" if arg else ""
        lines = [f"🧠 关于「{query}」的记忆{suffix}：",""]
        for i,r in enumerate(results,1): lines.append(f"{i}. {r[:150]+'...' if len(r)>150 else r}")
        yield event.plain_result("\n".join(lines))

    @filter.command("bili清算")
    async def cmd_consolidation(self, event: AstrMessageEvent):
        """手动触发日终清算。"""
        yield event.plain_result("🌙 开始手动清算...")
        try:
            summary = await self._consolidation.run_daily()
            yield event.plain_result(f"🌙 清算完成\n{summary}")
        except Exception as e:
            yield event.plain_result(f"❌ 清算失败: {e}")

    @filter.command("bili周总结")
    async def cmd_weekly_summary(self, event: AstrMessageEvent):
        """手动触发周总结（生成并按配置投递）。"""
        yield event.plain_result("📅 正在回顾这一周...")
        try:
            summary, delivered, image_path = await self.run_weekly_summary()
            if not summary:
                yield event.plain_result("📅 这周没什么活动记录，没有生成总结")
                return
            via = "、".join(delivered) if delivered else "仅存档"
            if image_path:
                yield event.chain_result([Plain(f"📅 周总结已生成（{via}）"), Image.fromFileSystem(image_path)])
            else:
                yield event.plain_result(f"{summary}\n\n——已投递：{via}")
        except Exception as e:
            yield event.plain_result(f"❌ 周总结失败: {e}")

    @filter.command("bili清理老化")
    async def cmd_cleanup_aged(self, event: AstrMessageEvent):
        """清理所有 aged=true 的长期记忆。"""
        removed = await self._consolidation.cleanup_aged()
        if removed:
            yield event.plain_result(f"🗑️ 已清理 {removed} 条老化记忆")
        else:
            yield event.plain_result("✅ 没有需要清理的老化记忆")

    @filter.command("bili联动")
    async def cmd_memory_integration(self, event: AstrMessageEvent):
        """查看供直播伴侣调用的统一记忆接口状态。"""
        live_count = sum(1 for m in self._memory if self._match_memory_type(m, {"live"}))
        profile_count = len(self._load_json(USER_PROFILE_FILE, {}))
        yield event.plain_result(
            "🎙️ BiliBot 记忆联动\n"
            "━━━━━━━━━━━━\n"
            f"接口版本：v{self.memory_api.api_version}\n"
            f"用户画像：{profile_count} 个\n"
            f"直播记忆：{live_count} 条\n"
            "状态：✅ 等待直播插件按 B站 UID 读写"
        )

    @filter.command("bili迁移记忆")
    async def cmd_migrate_memory(self, event: AstrMessageEvent):
        """手动将无有效 level 的旧记忆迁移（chat→recent/7分，其他→long_term/8分）。"""
        async with self._memory_write_lock:
            migrated = self._consolidation._migrate_legacy_entries()
            if migrated:
                await self._replace_memory_snapshot(assume_locked=True)
        if migrated:
            yield event.plain_result(f"📦 已迁移 {migrated} 条旧记忆（chat→recent/其他→long_term）")
        else:
            yield event.plain_result("✅ 所有记忆已有有效 level，无需迁移")

    async def _tool_bili_search_memory_result(self, query: str, memory_type: str = "", source: str = "") -> str:
        type_alias = {
            "chat": {"chat"},
            "交流": {"chat"},
            "聊天": {"chat"},
            "评论": {"chat"},
            "video": {"video"},
            "视频": {"video"},
            "观影": {"video"},
            "dynamic": {"dynamic"},
            "动态": {"dynamic"},
            "live": {"live"},
            "直播": {"live"},
            "user_summary": {"user_summary"},
            "summary": {"user_summary"},
            "总结": {"user_summary"},
            "压缩": {"user_summary"},
        }
        selected_types = type_alias.get(memory_type.strip(), None) if memory_type else None
        selected_source = source.strip() or None
        if selected_source == "all":
            selected_source = None
        results = await self._search_memories(query, limit=5, source=selected_source, memory_types=selected_types)
        if not results:
            return f"没有找到与「{query}」相关的记忆。"
        return "\n".join([f"{i}. {r}" for i, r in enumerate(results, 1)])
    @filter.command("bili好感")
    async def cmd_affection(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts)>=2:
            uid=parts[1].strip()
            sc=self._affection.get(uid,0)
            lv=self._get_level(sc,uid)
            p=self._load_json(USER_PROFILE_FILE,{}).get(uid,{})
            lines=[f"👤 用户 {uid}",f"💛 {sc}分 | {LEVEL_NAMES[lv]}",f"📝 {p.get('impression','暂无')}"]
            if p.get("facts"): lines.append(f"📋 {'；'.join(p['facts'][-5:])}")
            yield event.plain_result("\n".join(lines))
            return
        if not self._affection:
            yield event.plain_result("💛 无记录")
            return
        sa=sorted(self._affection.items(),key=lambda x:x[1],reverse=True)[:10]
        lines=["💛 好感度 Top 10","━━━━━━━━━━━━"]
        ps=self._load_json(USER_PROFILE_FILE,{})
        for i,(uid,sc) in enumerate(sa,1):
            lv=self._get_level(sc,uid)
            imp=ps.get(uid,{}).get("impression","")
            lines.append(f"{i}. UID:{uid} | {sc}分 {LEVEL_NAMES[lv]}{' — '+imp[:20] if imp else ''}")
        yield event.plain_result("\n".join(lines))
    @filter.command("bili拉黑")
    async def cmd_block(self, event: AstrMessageEvent):
        """手动拉黑用户。用法: /bili拉黑 <UID>"""
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts)<2:
            yield event.plain_result("用法: /bili拉黑 <UID>")
            return
        uid=parts[1].strip()
        if not uid.isdigit():
            yield event.plain_result("❌ UID必须是数字")
            return
        if self._is_owner(uid):
            yield event.plain_result("❌ 不能拉黑主人！")
            return
        success = await self._block_user(int(uid))
        bl = self._load_json(os.path.join(DATA_DIR,"block_log.json"),{})
        bl[uid] = {"username":"手动拉黑","reason":"手动拉黑","time":datetime.now().strftime("%Y-%m-%d %H:%M")}
        self._save_json(os.path.join(DATA_DIR,"block_log.json"), bl)
        yield event.plain_result(f"{'✅' if success else '⚠️'} 已拉黑 UID:{uid}{'（B站API调用成功）' if success else '（B站API失败，但已加入本地黑名单）'}")
    @filter.command("bili解黑")
    async def cmd_unblock(self, event: AstrMessageEvent):
        """解除拉黑。用法: /bili解黑 <UID>"""
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts)<2:
            yield event.plain_result("用法: /bili解黑 <UID>")
            return
        uid=parts[1].strip()
        bl = self._load_json(os.path.join(DATA_DIR,"block_log.json"),{})
        if uid not in bl:
            yield event.plain_result(f"⚠️ UID:{uid} 不在黑名单中")
            return
        # B站解除拉黑: act=6
        try:
            d, _ = await self._http_post("https://api.bilibili.com/x/relation/modify", data={"fid":uid,"act":6,"re_src":11,"csrf":self.config.get("BILI_JCT","")})
            api_ok = d["code"]==0
        except Exception: api_ok=False
        del bl[uid]
        self._save_json(os.path.join(DATA_DIR,"block_log.json"), bl)
        # 重置好感度为0
        self._affection[uid] = 0
        self._save_json(AFFECTION_FILE, self._affection)
        yield event.plain_result(f"✅ 已解除拉黑 UID:{uid}，好感度重置为0{'' if api_ok else '（B站API失败，但已从本地黑名单移除）'}")
    @filter.command("bili黑名单")
    async def cmd_blocklist(self, event: AstrMessageEvent):
        """查看拉黑名单"""
        bl = self._load_json(os.path.join(DATA_DIR,"block_log.json"),{})
        if not bl:
            yield event.plain_result("🚫 黑名单为空")
            return
        lines = ["🚫 黑名单","━━━━━━━━━━━━"]
        for uid,info in bl.items():
            lines.append(f"UID:{uid} | {info.get('reason','未知')} | {info.get('time','')}")
        yield event.plain_result("\n".join(lines))
    @filter.command("bili清理")
    async def cmd_cleanup(self, event: AstrMessageEvent):
        """清理临时文件和过期数据。用法: /bili清理 [all]"""
        parts = event.message_str.strip().split(maxsplit=1)
        full_clean = len(parts) >= 2 and parts[1].strip() == "all"
        # 清理临时文件
        self._cleanup_temp_files()
        msg_lines = ["🗑️ 清理完成：", "  ✅ 临时图片/视频/二维码已清理"]
        if full_clean:
            # 清理过大的日志文件
            for log_file, max_entries, label in [
                (SECURITY_LOG_FILE, 200, "安全日志"),
                (PROACTIVE_TRIGGER_LOG_FILE, 100, "主动触发日志"),
                (WATCH_LOG_FILE, 100, "观影日志"),
                (DYNAMIC_LOG_FILE, 50, "动态日志"),
                (REPLY_LOG_FILE, 500, "回复日志"),
            ]:
                data = self._load_json(log_file, [])
                if isinstance(data, list) and len(data) > max_entries:
                    self._save_json(log_file, data[-max_entries:])
                    msg_lines.append(f"  ✅ {label}：{len(data)}→{max_entries}条")
            # 清理过期的 replied.json（只保留最近2000条）
            replied = self._load_json(REPLIED_FILE, [])
            if isinstance(replied, list) and len(replied) > 2000:
                self._save_json(REPLIED_FILE, replied[-2000:])
                msg_lines.append(f"  ✅ 已回复记录：{len(replied)}→2000条")
            msg_lines.append("")
            msg_lines.append("💡 提示：如需完全重置，手动删除 plugin_data/astrbot_plugin_bilibili_ai_bot 目录")
        else:
            msg_lines.append("")
            msg_lines.append("💡 /bili清理 all ← 同时压缩日志文件")
        yield event.plain_result("\n".join(msg_lines))
    @filter.command("bili性格")
    async def cmd_personality(self, event: AstrMessageEvent):
        """查看性格演化记录。用法: /bili性格"""
        evo = self._normalize_personality_state(
            self._load_json(PERSONALITY_FILE, {})
        )
        readiness = await self._personality_evolution_readiness()
        lines = ["🌱 性格演化", "━━━━━━━━━━━━"]
        lines.append(
            f"模式：每周一次｜自动演化：{'开启' if self.config.get('ENABLE_PERSONALITY_EVOLUTION', False) else '关闭'}"
        )
        lines.append(
            f"数据准备：{readiness['days']}/{readiness['minimum_days']} 天"
            f"（{'已就绪' if readiness['ready'] else '继续积累'}）"
        )
        block = evo.get("dynamic_block", {})
        block_lines = []
        if block.get("recent_state"):
            block_lines.append(f"  状态：{block['recent_state']}")
        for label, key in (
            ("喜好", "recent_preferences"),
            ("感想", "recent_thoughts"),
            ("反思", "recent_reflections"),
        ):
            values = block.get(key, [])
            if values:
                block_lines.append(f"  {label}：{'；'.join(values)}")
        if block_lines:
            lines.append("【近期动态区块】")
            lines.extend(block_lines)
        traits = evo.get("evolved_traits", [])
        if traits:
            lines.append("【手动/旧版成长变化】")
            for i, t in enumerate(traits, 1):
                lines.append(f"  {i}. [{t.get('time','')}] {t.get('change','')}")
                if t.get("trigger"): lines.append(f"     ↳ 触发：{t['trigger']}")
        habits = evo.get("speech_habits", [])
        if habits:
            lines.append("【说话习惯】")
            for i, h in enumerate(habits, 1): lines.append(f"  {i}. {h}")
        opinions = evo.get("opinions", [])
        if opinions:
            lines.append("【对事物的看法】")
            for i, o in enumerate(opinions, 1): lines.append(f"  {i}. {o}")
        ref = evo.get("last_reflection", "")
        if ref: lines.append(f"\n💭 最近反思：{ref}")
        memes = [
            item for item in evo.get("memes", [])
            if self._meme_is_active(item, datetime.now().strftime("%Y-%m-%d"))
        ]
        if memes:
            lines.append("【近期低频表达】")
            for item in memes:
                lines.append(
                    f"  · {item.get('phrase', '')}（至 {item.get('expires_at', '')}）"
                )
        lines.append(
            f"\n📅 上次演化：{evo.get('last_evolve') or '从未'} | "
            f"版本：v{evo.get('version',0)} | 可回滚快照：{len(evo.get('history', []))}"
        )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili兴趣")
    async def cmd_interest(self, event: AstrMessageEvent):
        """查看近期视频口味、具体兴趣证据和已沉淀偏好。"""
        lifecycle_items = []
        layered = getattr(self, "layered_runtime", None)
        if layered is not None and getattr(layered, "is_open", False):
            try:
                lifecycle_items = await layered.preferences.current(limit=20)
            except Exception as exc:
                logger.debug(f"[BiliBot] 兴趣状态读取失败，使用本地副本: {exc}")
        if not lifecycle_items:
            lifecycle_items = self._lifecycle_preference_items()
        yield event.plain_result(
            self._format_interest_report(lifecycle_items=lifecycle_items)
        )

    @filter.command("bili性格编辑")
    async def cmd_personality_edit(self, event: AstrMessageEvent):
        """手动添加/编辑性格。用法:
        /bili性格编辑 习惯 <内容> — 添加说话习惯
        /bili性格编辑 看法 <内容> — 添加看法
        /bili性格编辑 变化 <内容> — 添加成长变化"""
        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("用法：\n/bili性格编辑 习惯 <内容>\n/bili性格编辑 看法 <内容>\n/bili性格编辑 变化 <内容>")
            return
        category, content = parts[1].strip(), parts[2].strip()
        evo = self._load_json(PERSONALITY_FILE, {})
        if not evo: evo = {"version":0,"last_evolve":"","evolved_traits":[],"speech_habits":[],"opinions":[],"last_reflection":""}
        if category == "习惯":
            evo.setdefault("speech_habits", []).append(content)
            evo["speech_habits"] = evo["speech_habits"][-5:]
            yield event.plain_result(f"✅ 已添加说话习惯：{content}")
        elif category == "看法":
            evo.setdefault("opinions", []).append(content)
            evo["opinions"] = evo["opinions"][-5:]
            yield event.plain_result(f"✅ 已添加看法：{content}")
        elif category == "变化":
            evo.setdefault("evolved_traits", []).append({"time": datetime.now().strftime("%Y-%m-%d"), "change": content, "trigger": "手动添加"})
            evo["evolved_traits"] = evo["evolved_traits"][-10:]
            yield event.plain_result(f"✅ 已添加成长变化：{content}")
        else:
            yield event.plain_result("❌ 类别不对，可选：习惯、看法、变化")
            return
        evo["version"] = evo.get("version", 0) + 1
        self._save_json(PERSONALITY_FILE, evo)

    @filter.command("bili性格删除")
    async def cmd_personality_delete(self, event: AstrMessageEvent):
        """删除性格演化条目。用法:
        /bili性格删除 习惯 <序号>
        /bili性格删除 看法 <序号>
        /bili性格删除 变化 <序号>"""
        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("用法：/bili性格删除 <习惯|看法|变化> <序号>")
            return
        category, idx_str = parts[1].strip(), parts[2].strip()
        if not idx_str.isdigit():
            yield event.plain_result("❌ 序号必须是数字")
            return
        idx = int(idx_str) - 1
        evo = self._load_json(PERSONALITY_FILE, {})
        if not evo:
            yield event.plain_result("🌱 没有演化记录")
            return
        key_map = {"习惯": "speech_habits", "看法": "opinions", "变化": "evolved_traits"}
        key = key_map.get(category)
        if not key:
            yield event.plain_result("❌ 类别不对，可选：习惯、看法、变化")
            return
        items = evo.get(key, [])
        if idx < 0 or idx >= len(items):
            yield event.plain_result(f"❌ 序号超范围（1-{len(items)}）")
            return
        removed = items.pop(idx)
        evo["version"] = evo.get("version", 0) + 1
        self._save_json(PERSONALITY_FILE, evo)
        desc = removed.get("change", removed) if isinstance(removed, dict) else removed
        yield event.plain_result(f"✅ 已删除：{desc}")

    @filter.command("bili性格回滚")
    async def cmd_personality_rollback(self, event: AstrMessageEvent):
        """回滚最近一次自动演化动态区块；不影响核心人设和手动条目。"""
        success, message = self._rollback_personality()
        yield event.plain_result(("✅ " if success else "⚠️ ") + message)

    @filter.command("bili日志")
    async def cmd_daily_log(self, event: AstrMessageEvent):
        """统一日志入口。用法: /bili日志 <视频|番剧|动态|回复> [日期YYYY-MM-DD]"""
        parts = event.message_str.strip().split(maxsplit=2)
        kind = parts[1].strip() if len(parts) >= 2 else ""
        date_given = len(parts) >= 3
        target_date = parts[2].strip() if date_given else datetime.now().strftime("%Y-%m-%d")
        # 兼容旧用法：/bili日志 2026-07-01 → 视频日志
        if kind and kind[0].isdigit():
            kind, target_date, date_given = "视频", kind, True
        renderers = {
            "视频": self._render_video_log,
            "番剧": self._render_bangumi_log,
            "动态": self._render_dynamic_log,
            "回复": self._render_reply_log,
        }
        if kind not in renderers:
            yield event.plain_result(
                "📋 日志查询\n"
                "━━━━━━━━━━━━\n"
                "请打全命令选择要看的日志：\n"
                "/bili日志 视频 — 主动看视频&评论记录\n"
                "/bili日志 番剧 — 看番记录\n"
                "/bili日志 动态 — 动态发布记录\n"
                "/bili日志 回复 — 评论回复记录\n"
                "\n可加日期查往天，如：/bili日志 视频 2026-07-01（默认今天）"
            )
            return
        yield event.plain_result(renderers[kind](target_date, date_given))

    def _render_video_log(self, target_date, date_given):
        wl = self._load_json(WATCH_LOG_FILE, [])
        today_watch = [l for l in wl if l.get("time", "").startswith(target_date)]
        pl = self._load_json(PROACTIVE_LOG_FILE, [])
        today_comment = [l for l in pl if l.get("time", "").startswith(target_date) and l.get("type") != "bangumi"]
        if not today_watch and not today_comment:
            return f"📋 {target_date} 没有主动行为记录"
        lines = [f"📋 {target_date} 主动行为日志", "━━━━━━━━━━━━"]
        if today_watch:
            lines.append(f"\n🎬 看了 {len(today_watch)} 个视频：")
            for i, w in enumerate(today_watch, 1):
                score = w.get("score", "?")
                actions = " ".join(w.get("actions", [])) or "无互动"
                lines.append(f"  {i}. 「{w.get('title','?')[:30]}」")
                lines.append(f"     🔗 bilibili.com/video/{w.get('bvid','')}")
                lines.append(f"     UP:{w.get('up_name','?')} | {score}分 | {w.get('mood','?')}")
                if w.get("review"): lines.append(f"     📝 {w['review'][:60]}")
                lines.append(f"     {actions}")
        if today_comment:
            lines.append(f"\n💬 发了 {len(today_comment)} 条评论：")
            for i, c in enumerate(today_comment, 1):
                lines.append(f"  {i}. 「{c.get('title','?')[:30]}」")
                lines.append(f"     💬 {c.get('comment','?')[:80]}")
        return "\n".join(lines)

    def _render_bangumi_log(self, target_date, date_given):
        log = self._load_json(BANGUMI_WATCH_LOG_FILE, [])
        today_log = [l for l in log if l.get("time", "").startswith(target_date)]
        if not today_log:
            return f"🎬 {target_date} 没有看番记录"
        lines = [f"🎬 {target_date} 看番日志（共 {len(today_log)} 集）", "━━━━━━━━━━━━"]
        for i, l in enumerate(today_log, 1):
            t = l.get("time", "?")
            time_part = t.split(" ", 1)[1] if " " in t else t
            lines.append(f"{i}. [{time_part}] 《{l.get('title', '?')}》第{l.get('ep_index', '?')}话")
            lines.append(f"   ⭐{l.get('score', '?')}/10 {l.get('mood', '')} {l.get('review', '')[:40]}")
            if l.get("comment"):
                lines.append(f"   💬 {l['comment'][:40]}")
        return "\n".join(lines)

    def _render_dynamic_log(self, target_date, date_given):
        log = self._load_json(DYNAMIC_LOG_FILE, [])
        if date_given:
            log = [l for l in log if l.get("time", "").startswith(target_date)]
            if not log:
                return f"📝 {target_date} 没有动态记录"
            header = f"📝 {target_date} 动态记录"
        else:
            if not log:
                return "📝 还没有动态记录"
            header = "📝 最近动态记录"
        lines = [header, "━━━━━━━━━━━━"]
        for i, l in enumerate(log[-10:], 1):
            img = "🖼️" if l.get("has_image") else "📄"
            lines.append(f"{i}. [{l.get('time','')}] {img}")
            lines.append(f"   {l.get('text','')[:60]}...")
        return "\n".join(lines)

    def _render_reply_log(self, target_date, date_given):
        # 优先从独立回复日志读取
        reply_log = self._load_json(REPLY_LOG_FILE, [])
        today_replies = [r for r in reply_log if r.get("time", "").startswith(target_date)]
        # 兼容旧数据：也从 memory.json 补充
        if not today_replies:
            memory = self._load_json(MEMORY_FILE, [])
            chats = [m for m in memory if m.get("memory_type") == "chat" and m.get("time", "").startswith(target_date)]
            if not chats:
                return f"💬 {target_date} 没有回复记录"
            lines = [f"💬 {target_date} 回复日志（共 {len(chats)} 条，来自记忆）", "━━━━━━━━━━━━"]
            for i, c in enumerate(chats, 1):
                username = c.get("username", "?")
                t = c.get("time", "?")
                time_part = t.split(" ", 1)[1] if " " in t else t
                text = c.get("text", "")
                user_msg = ""
                bot_reply = ""
                if "说：" in text and "| Bot回复：" in text:
                    after_said = text.split("说：", 1)[1]
                    parts2 = after_said.split(" | Bot回复：", 1)
                    user_msg = parts2[0].strip()[:60]
                    bot_reply = parts2[1].strip()[:60] if len(parts2) > 1 else ""
                else:
                    user_msg = text[:80]
                lines.append(f"{i}. [{time_part}] {username}")
                lines.append(f"   📨 {user_msg}")
                if bot_reply:
                    lines.append(f"   💬 {bot_reply}")
            return "\n".join(lines)
        lines = [f"💬 {target_date} 回复日志（共 {len(today_replies)} 条）", "━━━━━━━━━━━━"]
        for i, r in enumerate(today_replies, 1):
            t = r.get("time", "?")
            time_part = t.split(" ", 1)[1] if " " in t else t
            lines.append(f"{i}. [{time_part}] {r.get('username', '?')}")
            lines.append(f"   📨 {r.get('content', '?')[:60]}")
            lines.append(f"   💬 {r.get('reply', '?')[:60]}")
            lines.append(f"   💛 好感{'+' if r.get('score_delta', 0) >= 0 else ''}{r.get('score_delta', 0)}")
        return "\n".join(lines)

    @filter.command("bili永久记忆")
    async def cmd_permanent_memory(self, event: AstrMessageEvent):
        """查看/删除永久记忆。用法: /bili永久记忆 | /bili永久记忆 删除 <序号>"""
        parts = event.message_str.strip().split(maxsplit=2)
        perm = self._load_json(PERMANENT_MEMORY_FILE, [])
        if len(parts) >= 3 and parts[1] == "删除":
            idx_str = parts[2].strip()
            if not idx_str.isdigit():
                yield event.plain_result("❌ 序号必须是数字")
                return
            idx = int(idx_str) - 1
            if idx < 0 or idx >= len(perm):
                yield event.plain_result(f"❌ 序号超范围（1-{len(perm)}）")
                return
            removed = perm.pop(idx)
            self._save_json(PERMANENT_MEMORY_FILE, perm)
            yield event.plain_result(f"✅ 已删除永久记忆：{removed.get('text','')[:50]}")
            return
        if not perm:
            yield event.plain_result("💎 还没有永久记忆")
            return
        lines = [f"💎 永久记忆（{len(perm)}/20）", "━━━━━━━━━━━━"]
        for i, p in enumerate(perm, 1):
            lines.append(f"  {i}. [{p.get('time','?')}] {p.get('text','')[:80]}")
        lines.append("\n删除用: /bili永久记忆 删除 <序号>")
        yield event.plain_result("\n".join(lines))

    @filter.command("bili动态")
    async def cmd_dynamic(self, event: AstrMessageEvent):
        """手动发布动态"""
        if not self._has_cookie():
            yield event.plain_result("❌ 请先 /bili登录")
            return
        yield event.plain_result("📢 正在发布动态...")
        await self._run_dynamic(human_initiated=True)
        yield event.plain_result("📢 动态发布流程已完成，请查看日志")

    @filter.command("bili看番")
    async def cmd_watch_bangumi(self, event: AstrMessageEvent):
        """手动触发看番。用法: /bili看番 [season_id]"""
        if not self.config.get("ENABLE_BANGUMI", False):
            yield event.plain_result("⚠️ 番剧功能未开启（ENABLE_BANGUMI）")
            return
        if not self._has_cookie():
            yield event.plain_result("⚠️ 未登录B站")
            return
        if self._bangumi_task is not None and not self._bangumi_task.done():
            yield event.plain_result("⚠️ 已经在看番了，等这轮看完吧")
            return
        parts = event.message_str.strip().split(maxsplit=1)
        season_id = None
        if len(parts) >= 2 and parts[1].strip().isdigit():
            season_id = int(parts[1].strip())
        self._bangumi_task = asyncio.create_task(self._run_bangumi(season_id=season_id))
        yield event.plain_result(f"🎬 开始看番{'（sid=' + str(season_id) + '）' if season_id else '（随机选番）'}...")

    @filter.command("bili番剧记忆")
    async def cmd_bangumi_memory(self, event: AstrMessageEvent):
        """查看番剧追番记忆。用法: /bili番剧记忆"""
        mem = self._load_bangumi_memory()
        if not mem:
            yield event.plain_result("🎬 还没有看过任何番剧")
            return
        lines = [f"🎬 追番记忆（共 {len(mem)} 部）", "━━━━━━━━━━━━"]
        for sid, record in sorted(mem.items(), key=lambda x: x[1].get("episodes", [{}])[-1].get("watched_at", ""), reverse=True)[:15]:
            title = record.get("title", "?")
            total = record.get("total_watched", 0)
            watched_eps = record.get("watched_eps", [])
            last_score = record.get("last_score", "?")
            ep_str = ",".join(watched_eps[:20]) if watched_eps else "?"
            lines.append(f"  《{title}》已看{total}集 [{ep_str}] 最近评分:{last_score}")
        yield event.plain_result("\n".join(lines))

    @filter.command("biliUMO")
    async def cmd_umo(self, event: AstrMessageEvent):
        """获取当前会话 UMO 并自动记录到周总结/恶意告警配置。"""
        umo = event.unified_msg_origin
        saved = []
        if not (self.config.get("ABUSE_ALERT_QQ_UMO", "") or "").strip():
            self.config["ABUSE_ALERT_QQ_UMO"] = umo
            saved.append("恶意告警")
        if not (self.config.get("WEEKLY_SUMMARY_QQ_UMO", "") or "").strip():
            self.config["WEEKLY_SUMMARY_QQ_UMO"] = umo
            saved.append("周总结")
        if not (self.config.get("OWNER_QQ_UMO", "") or "").strip():
            self.config["OWNER_QQ_UMO"] = umo
            saved.append("主人跨端活动/分享")
        if saved:
            self.config.save_config()
            yield event.plain_result(f"当前 UMO：{umo}\n✅ 已自动填入：{'、'.join(saved)}的 QQ UMO 配置")
        else:
            yield event.plain_result(f"当前 UMO：{umo}\n（恶意告警和周总结的 UMO 都已有值，未覆盖）")

    @filter.command("bili帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result("📺 BiliBot 命令\n━━━━━━━━━━━━\n/bili登录 — 扫码登录\n/bili确认 — 确认扫码\n/bili状态 — 运行状态\n/bili直播 — 进入指定直播间参与弹幕互动\n/bili计划 — 查看今日主动/动态/看番时间\n/bili分区 — 查看视频池中文填法和分区名\n/bili启动 — 启动\n/bili停止 — 停止\n/bili主动 — 立刻触发一次主动看视频\n/bili解析 [链接/BV号] — 解析指定、引用或上一个视频\n/bili开关 — 功能开关\n/bili刷新 — 刷新Cookie\n/bili记忆 — 搜索记忆\n/bili好感 — 好感度\n/bili拉黑 — 手动拉黑\n/bili解黑 — 解除拉黑\n/bili黑名单 — 查看黑名单\n/bili兴趣 — 查看近期视频口味与已沉淀偏好\n/bili性格 — 查看性格演化\n/bili性格编辑 — 手动编辑性格\n/bili性格删除 — 删除演化条目\n/bili日志 视频 — 主动看视频&评论记录\n/bili日志 番剧 — 看番记录\n/bili日志 动态 — 动态发布记录\n/bili日志 回复 — 评论回复记录\n/bili开关 解析 — 视频解析总开关\n/bili开关 自动解析 — 聊天链接自动解析开关\n/bili开关 手动解析 — /bili解析 命令开关\n/bili开关 LLM解析 — bili_parse_video 工具开关\n/bili开关 解析视频 — 是否发送原视频切片\n/bili开关 筛选 — 主动看视频前标题筛选\n/bili开关 直播回复 — 直播间弹幕互动开关\n/bili联动 — 查看直播伴侣联动状态\n/bili看番 — 手动触发看番\n/bili番剧记忆 — 查看追番进度\n/bili永久记忆 — 查看/删除永久记忆\n/bili动态 — 手动发动态\n/bili绑定 — 绑定QQ与B站UID\n/bili解绑 — 解除绑定\n/bili清理 — 清理临时文件\n/bili帮助 — 本帮助\n/biliUMO — 获取当前UMO并自动填入配置\n━━━━━━━━━━━━\n💡 首次用 /bili登录\n💡 视频池配置不会背编号时，用 /bili分区 查中文填法")

    # ===== QQ↔B站 记忆互通 =====
    @filter.command("bili绑定")
    async def cmd_bind(self, event: AstrMessageEvent):
        """绑定QQ与B站UID: /bili绑定 12345"""
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法：/bili绑定 <B站UID>")
            return
        bili_uid = parts[1].strip()
        if not bili_uid.isdigit():
            yield event.plain_result("⚠️ B站UID应为数字")
            return
        qq_id = str(event.get_sender_id())
        bindings = self._load_json(BINDING_FILE, {})
        bindings[qq_id] = bili_uid
        self._save_json(BINDING_FILE, bindings)
        yield event.plain_result(f"✅ 已绑定 QQ:{qq_id} ↔ B站UID:{bili_uid}")
    @filter.command("bili解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """解除QQ与B站绑定"""
        qq_id = str(event.get_sender_id())
        bindings = self._load_json(BINDING_FILE, {})
        if qq_id not in bindings:
            yield event.plain_result("⚠️ 你还没有绑定B站UID")
            return
        del bindings[qq_id]
        self._save_json(BINDING_FILE, bindings)
        yield event.plain_result("✅ 已解除绑定")

    def _set_cross_platform_activity(self, kind, stage, **details):
        self._cross_platform_activity = {"kind": kind, "stage": stage, "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **{key: str(value)[:160] for key, value in details.items() if value not in (None, "")}}

    def _clear_cross_platform_activity(self, kind=None):
        if not kind or self._cross_platform_activity.get("kind") == kind:
            self._cross_platform_activity = {}

    def _cross_platform_activity_context(self):
        item = self._cross_platform_activity
        if not item:
            return ""
        labels = {"proactive": "主动浏览", "bangumi": "追番", "dynamic": "发布动态", "dynamic_watch": "巡视关注动态"}
        text = f"【当前活动】正在{labels.get(item.get('kind'), item.get('kind', '处理任务'))}：{item.get('stage', '进行中')}"
        if item.get('title'): text += f"；内容：《{item['title']}》"
        if item.get('episode'): text += f"；{item['episode']}"
        return text + "。仅在用户询问当前活动或自然相关时提及；不得猜测未记录的进度。"

    @filter.on_llm_request()
    async def inject_bili_memory(self, event: AstrMessageEvent, req: ProviderRequest):
        """QQ对话自动注入B站侧记忆：永久记忆 + 语义检索相关记忆"""
        try:
            await self._maybe_trigger_proactive_from_llm(event, req)
            msg = event.message_str or ""
            if not msg or msg.startswith("/"):
                return
            if self._inject_recent_group_share_into_request(event, req):
                logger.debug("[BiliBot] recent group share context injected before user message")
            qq_id = str(event.get_sender_id())
            bindings = self._load_json(BINDING_FILE, {})
            if qq_id not in bindings:
                return
            bili_uid = str(bindings[qq_id])
            # Current activity is a short-lived fact and is separate from history
            # sharing. It is available only to the QQ identity bound to OWNER_MID.
            owner_mid = str(self.config.get("OWNER_MID", "") or "").strip()
            if not owner_mid or bili_uid != owner_mid:
                return
            if self.config.get("ENABLE_CROSS_PLATFORM_ACTIVITY_STATUS", True):
                activity = self._cross_platform_activity_context()
                if activity:
                    self._inject_context_block_before_user(req, activity)
            # Historical memory remains opt-in and separately isolated.
            if self.config.get("MEMORY_ISOLATION_MODE", "isolated") != "safe_share":
                return
            if not self.config.get("ENABLE_SAFE_CROSS_PLATFORM_MEMORY", False):
                return

            from .core.security.redact import contains_credentials, redact_outbound
            blocked_prefixes = [str(value).strip().lower() for value in self.config.get("MEMORY_BLOCKED_PREFIXES", []) if str(value).strip()]
            blocked_keywords = [str(value).strip().lower() for value in self.config.get("MEMORY_BLOCKED_KEYWORDS", []) if str(value).strip()]
            safe_memories = []
            recent = self.memory_api.get_recent_memories(
                user_id=bili_uid,
                hours=24 * 7,
                limit=12,
                reader_scope="admin",
            )
            for item in recent:
                if str(item.get("memory_type", "chat")) not in {"chat", "video", "dynamic", "user_summary"}:
                    continue
                text = str(item.get("text", "") or "").strip()
                lowered = text.lower()
                if not text or any(lowered.startswith(prefix) for prefix in blocked_prefixes):
                    continue
                if any(keyword in lowered for keyword in blocked_keywords):
                    continue
                if contains_credentials(text):
                    continue
                redacted, _ = redact_outbound(text, internal=True)
                redacted = redacted.replace(str(bili_uid), "[UID已隐藏]")
                if redacted and len(redacted) >= 6:
                    safe_memories.append(redacted[:260])
                if len(safe_memories) >= 3:
                    break
            if not safe_memories:
                return
            policy = str(self.config.get("CROSS_PLATFORM_MEMORY_PROMPT", "") or "")[:600]
            bind_ctx = (
                "【B站侧安全共享摘要】\n"
                f"共享规则：{policy}\n"
                + "\n".join(f"- {text}" for text in safe_memories)
                + "\n这些内容已做硬脱敏，只能作为轻量生活背景；不得反推出UID、第三方身份、私信原文、账号凭据或系统信息。"
            )
            self._inject_context_block_before_user(req, bind_ctx)
            logger.debug("[BiliBot] 已向主人侧注入脱敏后的B站趣事摘要")
        except Exception as e:
            logger.error(f"[BiliBot] 记忆注入失败: {e}")

    # capture_qq_memory 已移除（v1.3.0），QQ记忆不再单独存储
