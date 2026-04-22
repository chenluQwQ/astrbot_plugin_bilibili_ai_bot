"""
AstrBot Plugin - Bilibili Bot 1.2.0
自动回复评论、好感度、记忆、心情、用户画像、主动视频、动态发布、Web管理面板。
拆分版本：核心逻辑分布在 core/ 下的 Mixin 模块中。
"""
import sys
import io, os, time, asyncio, traceback
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
    WebSearchMixin, VideoMixin, ReplyMixin,
    ProactiveMixin, DynamicMixin, ScheduleMixin,
)

_astrbot_site_packages = os.path.join(os.path.expanduser("~"), ".astrbot", "data", "site-packages")
if os.path.isdir(_astrbot_site_packages) and _astrbot_site_packages not in sys.path:
    sys.path.insert(0, _astrbot_site_packages)

@register("astrbot_plugin_bilibili_ai_bot","chenluQwQ","B站 AI Bot — 自动回复评论、好感度、记忆、心情、用户画像、主动视频、性格演化、动态发布","1.2.0","https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot")
class BiliBiliBot(Star, UtilsMixin, LLMMixin, VisionMixin, MemoryMixin, AffectionMixin, PersonalityMixin, BilibiliAPIMixin, WebSearchMixin, VideoMixin, ReplyMixin, ProactiveMixin, DynamicMixin, ScheduleMixin):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._ensure_data_dir()
        self._running = False
        self._task = None
        self._proactive_task = None
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
        self._llm_cooldown_until = 0
        self._proactive_times, self._proactive_triggered = [], set()
        self._dynamic_task = None
        self._dynamic_times, self._dynamic_triggered = [], set()
        self._web_panel = None
        self._log_environment_warnings()
        if self._has_cookie():
            asyncio.create_task(self._auto_start())
        if self.config.get("ENABLE_WEB_PANEL", False):
            asyncio.create_task(self._start_web_panel())

    async def _start_web_panel(self):
        try:
            from .web_panel import WebPanel
            port = self.config.get("WEB_PANEL_PORT", 5001)
            password = self.config.get("WEB_PANEL_PASSWORD", "admin123")
            self._web_panel = WebPanel(self, port=port, password=password)
            await self._web_panel.start()
        except Exception as e:
            logger.error(f"[BiliBot] Web面板启动失败: {e}")

    async def _auto_start(self):
        await asyncio.sleep(3)
        valid, _ = await self.check_cookie()
        if valid: await self._start_bot(); logger.info("[BiliBot] 自动启动")
        else: logger.warning("[BiliBot] Cookie无效")
    async def _start_bot(self):
        if self._running: return
        self._mark_overdue_schedule_as_triggered_on_startup()
        self._running = True; self._task = asyncio.create_task(self._main_loop()); logger.info("[BiliBot] 启动")
    async def _stop_bot(self):
        self._running = False
        if self._task: self._task.cancel(); self._task = None
        if self._proactive_task and not self._proactive_task.done(): self._proactive_task.cancel(); self._proactive_task = None
        if self._dynamic_task and not self._dynamic_task.done(): self._dynamic_task.cancel(); self._dynamic_task = None
        logger.info("[BiliBot] 停止")

    async def _main_loop(self):
        logger.info("[BiliBot] 主循环开始")
        while self._running:
            try:
                await self._maybe_evolve_personality()
                h = datetime.now().hour; ss = self.config.get("SLEEP_START", 2); se = self.config.get("SLEEP_END", 8)
                if ss <= h < se: await asyncio.sleep(60); continue
                ci = self.config.get("COOKIE_CHECK_INTERVAL", 6) * 3600
                if time.time() - self._last_cookie_check > ci: await self._check_and_refresh_cookie()
                self._last_cookie_check = time.time()
                if self.config.get("ENABLE_PROACTIVE", False):
                    now_dt = datetime.now(); today_str = now_dt.strftime("%Y-%m-%d")
                    sched = self._load_json(SCHEDULE_FILE, {})
                    if sched.get("date") != today_str:
                        self._proactive_times, self._proactive_triggered = self._generate_daily_schedule()
                        logger.info(f"[BiliBot] 📅 新的一天！主动视频时间：{[f'{ph}:{pm:02d}' for ph,pm in self._proactive_times]}")
                    elif not self._proactive_times:
                        self._proactive_times, self._proactive_triggered = self._load_or_generate_schedule()
                    for ph, pm in self._proactive_times:
                        key = f"{ph}:{pm:02d}"
                        if key not in self._proactive_triggered and (now_dt.hour > ph or (now_dt.hour == ph and now_dt.minute >= pm)):
                            if self._proactive_task is None or self._proactive_task.done():
                                self._proactive_task = asyncio.create_task(self._run_proactive())
                                self._proactive_triggered.add(key)
                                self._save_schedule_state(self._proactive_times, self._proactive_triggered)
                                trigger_log = self._load_json(PROACTIVE_TRIGGER_LOG_FILE, [])
                                trigger_log.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "proactive_video", "scheduled": key, "status": "triggered"})
                                self._save_json(PROACTIVE_TRIGGER_LOG_FILE, trigger_log[-200:])
                                logger.info(f"[BiliBot] 🎯 触发主动视频（{key}）")
                if self.config.get("ENABLE_DYNAMIC", False):
                    now_dt = datetime.now(); today_str = now_dt.strftime("%Y-%m-%d")
                    sched = self._load_json(DYNAMIC_SCHEDULE_FILE, {})
                    if sched.get("date") != today_str:
                        self._dynamic_times, self._dynamic_triggered = self._generate_dynamic_schedule()
                        logger.info(f"[BiliBot] 📅 动态时间：{[f'{dh}:{dm:02d}' for dh,dm in self._dynamic_times]}")
                    elif not self._dynamic_times:
                        self._dynamic_times, self._dynamic_triggered = self._load_or_generate_dynamic_schedule()
                    for dh, dm in self._dynamic_times:
                        key = f"{dh}:{dm:02d}"
                        if key not in self._dynamic_triggered and (now_dt.hour > dh or (now_dt.hour == dh and now_dt.minute >= dm)):
                            if self._dynamic_task is None or self._dynamic_task.done():
                                self._dynamic_task = asyncio.create_task(self._run_dynamic())
                                self._dynamic_triggered.add(key)
                                self._save_dynamic_schedule_state(self._dynamic_times, self._dynamic_triggered)
                                logger.info(f"[BiliBot] 📢 触发动态发布（{key}）")
                if self.config.get("ENABLE_REPLY", True): await self._poll_unified()
                await asyncio.sleep(self.config.get("POLL_INTERVAL", 20))
            except asyncio.CancelledError: break
            except Exception as e: logger.error(f"[BiliBot] 主循环出错: {e}\n{traceback.format_exc()}"); await asyncio.sleep(30)
        self._running = False

    async def _check_and_refresh_cookie(self):
        valid, info = await self.check_cookie()
        if valid: logger.info(f"[BiliBot] Cookie OK: {info}"); return
        logger.warning(f"[BiliBot] Cookie 失效: {info}")
        if self.config.get("COOKIE_AUTO_REFRESH", True):
            ok, msg = await self.refresh_cookie(); logger.info(f"[BiliBot] 刷新{'成功' if ok else '失败'}: {msg}")

    async def terminate(self):
        await self._stop_bot()
        if self._web_panel: await self._web_panel.stop()
        self._cleanup_temp_files()
        logger.info("[BiliBot] 已停用")
    # ===== QQ命令 =====
    @filter.command("bili登录")
    async def cmd_login(self, event: AstrMessageEvent):
        qr_url, qrcode_key = await self._qr_login_generate()
        if not qr_url:
            yield event.plain_result("❌ 生成二维码失败")
            return
        self._login_qrcode_key = qrcode_key
        import qrcode; qr=qrcode.QRCode(version=1,box_size=8,border=2); qr.add_data(qr_url); qr.make(fit=True)
        img=qr.make_image(fill_color="black",back_color="white")
        buf=io.BytesIO()
        img.save(buf,format="PNG")
        buf.seek(0)
        qr_path=os.path.join(DATA_DIR,"login_qr.png")
        with open(qr_path,"wb") as f: f.write(buf.getvalue())
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
        lines = [
            f"📺 BiliBot 1.1.1 状态","━━━━━━━━━━━━",f"🍪 {info}",
            f"{'🟢 运行中' if self._running else '🔴 未运行'}",
            f"🧠 记忆:{mc}条 | 💎永久:{pmc}条 | 👤档案:{pc}个",
            f"🎭 心情:{mood} | 🌱性格v{evo_ver}（{evo_last[:10]}）",
            f"📹 今日已看:{today_watched}个视频 | 📝动态:{today_dynamic}条",
            f"🎯 主动时间:{', '.join(schedule['proactive_times']) if schedule['proactive_times'] else '未生成'}",
            f"📢 动态时间:{', '.join(schedule['dynamic_times']) if schedule['dynamic_times'] else '未生成'}",
            f"✅ 已触发主动:{', '.join(schedule['proactive_triggered']) if schedule['proactive_triggered'] else '暂无'}",
            f"✅ 已触发动态:{', '.join(schedule['dynamic_triggered']) if schedule['dynamic_triggered'] else '暂无'}",
            f"回复:{'✅' if self.config.get('ENABLE_REPLY',True) else '❌'} 好感:{'✅' if self.config.get('ENABLE_AFFECTION',True) else '❌'} 心情:{'✅' if self.config.get('ENABLE_MOOD',True) else '❌'}",
            f"主动:{'✅' if self.config.get('ENABLE_PROACTIVE',False) else '❌'} 动态:{'✅' if self.config.get('ENABLE_DYNAMIC',False) else '❌'} 演化:{'✅' if self.config.get('ENABLE_PERSONALITY_EVOLUTION',True) else '❌'}",
            f"🔍 联网搜索:{'✅ '+feature_status['web_search_backend'] if feature_status['web_search'] else '❌'} 判断模型:{'✅' if feature_status['web_search_judge'] else '❌(用主模型)'}",
            f"📦 视频池:{', '.join(self.config.get('PROACTIVE_VIDEO_POOLS', ['popular']))}",
            f"视频视觉Provider:{'✅' if env['llm']['video_provider'] else '❌'} 独立API:{'✅' if env['llm']['video_api'] else '❌'}",
            f"图片识别Provider:{'✅' if env['llm']['image_provider'] else '❌'} 独立API:{'✅' if env['llm']['image_api'] else '❌'}",
            f"外部命令 yt-dlp:{'✅' if cmd_status['yt-dlp'] else '❌'} ffmpeg:{'✅' if cmd_status['ffmpeg'] else '❌'} ffprobe:{'✅' if cmd_status['ffprobe'] else '❌'}",
            f"主动视频直读/截帧:{'✅' if feature_status['proactive_video_media'] else '❌'} 纯文本回退:{'✅' if feature_status['proactive_video_fallback_text'] else '❌'}",
        ]
        yield event.plain_result("\n".join(lines))
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
        ]
        yield event.plain_result("\n".join(lines))
    @filter.command("bili分区")
    async def cmd_regions(self, event: AstrMessageEvent):
        """查看B站分区列表及编号，用于配置视频池"""
        lines = ["📂 B站分区列表", "━━━━━━━━━━━━",
                  "填法：ranking:rid 或 newlist:tid",
                  "逗号可写多个如 ranking:4,160", ""]
        for rid, zone in BILI_ZONES.items():
            lines.append(f"📁 {zone['name']} (rid:{rid})")
            if zone["children"]:
                subs = [f"{name}({tid})" for tid, name in zone["children"].items()]
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
        return "已在后台触发一次主动看B站视频流程。稍后可用 /bili日志 查看结果。"
    @filter.llm_tool(name="bili_watch_videos")
    async def tool_bili_watch_videos(self, event: AstrMessageEvent, **kwargs):
        """触发一次主动看B站视频流程。

        当用户明确要求你去看看/刷刷/随机看一些B站视频时使用。
        """
        yield event.plain_result(await self._tool_bili_watch_videos_result())
    @filter.command("bili开关")
    async def cmd_toggle(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts)<2:
            tm = {"回复":"ENABLE_REPLY","主动":"ENABLE_PROACTIVE","动态":"ENABLE_DYNAMIC","好感":"ENABLE_AFFECTION","心情":"ENABLE_MOOD","演化":"ENABLE_PERSONALITY_EVOLUTION","点赞":"PROACTIVE_LIKE","投币":"PROACTIVE_COIN","收藏":"PROACTIVE_FAV","关注":"PROACTIVE_FOLLOW","评论":"PROACTIVE_COMMENT"}
            lines = ["可切换功能："] + [f"  {n} ({'✅' if self.config.get(k,True) else '❌'})" for n,k in tm.items()] + ["","用法: /bili开关 回复"]
            yield event.plain_result("\n".join(lines))
            return
        name=parts[1].strip()
        tm = {"回复":"ENABLE_REPLY","主动":"ENABLE_PROACTIVE","动态":"ENABLE_DYNAMIC","好感":"ENABLE_AFFECTION","心情":"ENABLE_MOOD","演化":"ENABLE_PERSONALITY_EVOLUTION","点赞":"PROACTIVE_LIKE","投币":"PROACTIVE_COIN","收藏":"PROACTIVE_FAV","关注":"PROACTIVE_FOLLOW","评论":"PROACTIVE_COMMENT"}
        key=tm.get(name)
        if not key:
            yield event.plain_result(f"❌ 不认识：{name}")
            return
        cur=self.config.get(key,True)
        self.config[key]=not cur
        self.config.save_config()
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
            "总结": {"user_summary"},
            "压缩": {"user_summary"},
        }
        if len(parts)<2:
            mc=len(self._memory)
            bc=len([m for m in self._memory if m.get("source")=="bilibili"])
            qc=len([m for m in self._memory if m.get("source")=="qq"])
            chat_count = len([m for m in self._memory if self._match_memory_type(m, {"chat"})])
            video_count = len([m for m in self._memory if self._match_memory_type(m, {"video"})])
            dynamic_count = len([m for m in self._memory if self._match_memory_type(m, {"dynamic"})])
            user_summary_count = len([m for m in self._memory if self._match_memory_type(m, {"user_summary"})])
            yield event.plain_result(
                "🧠 记忆统计\n"
                f"总计:{mc} | B站:{bc} | QQ:{qc}\n"
                f"交流:{chat_count} | 视频:{video_count} | 动态:{dynamic_count} | 用户总结:{user_summary_count}\n\n"
                "用法:\n"
                "/bili记忆 <关键词>\n"
                "/bili记忆 <关键词> qq ← 只搜QQ\n"
                "/bili记忆 <关键词> 视频 ← 只搜视频记忆\n"
                "/bili记忆 <关键词> 动态 ← 只搜动态记忆\n"
                "/bili记忆 <关键词> 交流 ← 只搜交流记忆"
            ); return
        query=parts[1]
        arg=parts[2] if len(parts)>2 else None
        source = None
        memory_types = None
        if arg:
            if arg == "all":
                source = None
            elif arg in ("qq", "bilibili"):
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
    @filter.llm_tool(name="bili_search_memory")
    async def tool_bili_search_memory(self, event: AstrMessageEvent, query: str, memory_type: str = "", source: str = "", **kwargs):
        """搜索插件记忆，在回答和B站经历、视频、动态、跨平台互动相关的问题时使用。

        Args:
            query(string): 要搜索的关键词或问题
            memory_type(string): 记忆类型，可选 chat/video/dynamic/user_summary 或 交流/视频/动态/总结
            source(string): 记忆来源，可选 bilibili/qq/all
        """
        yield event.plain_result(
            await self._tool_bili_search_memory_result(
                query=query,
                memory_type=memory_type,
                source=source,
            )
        )
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
        evo = self._load_json(PERSONALITY_FILE, {})
        if not evo:
            yield event.plain_result("🌱 还没有性格演化记录")
            return
        lines = ["🌱 性格演化", "━━━━━━━━━━━━"]
        traits = evo.get("evolved_traits", [])
        if traits:
            lines.append("【成长变化】")
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
        lines.append(f"\n📅 上次演化：{evo.get('last_evolve','未知')} | 版本：v{evo.get('version',0)}")
        yield event.plain_result("\n".join(lines))

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

    @filter.command("bili日志")
    async def cmd_daily_log(self, event: AstrMessageEvent):
        """查看今天的视频观看和评论日志。用法: /bili日志 [日期YYYY-MM-DD]"""
        parts = event.message_str.strip().split(maxsplit=1)
        target_date = parts[1].strip() if len(parts) >= 2 else datetime.now().strftime("%Y-%m-%d")
        # 观看日志
        wl = self._load_json(WATCH_LOG_FILE, [])
        today_watch = [l for l in wl if l.get("time", "").startswith(target_date)]
        # 评论日志
        pl = self._load_json(PROACTIVE_LOG_FILE, [])
        today_comment = [l for l in pl if l.get("time", "").startswith(target_date)]
        if not today_watch and not today_comment:
            yield event.plain_result(f"📋 {target_date} 没有主动行为记录")
            return
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
        yield event.plain_result("\n".join(lines))

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
        await self._run_dynamic()
        yield event.plain_result("📢 动态发布流程已完成，请查看日志")

    @filter.command("bili动态日志")
    async def cmd_dynamic_log(self, event: AstrMessageEvent):
        """查看动态发布日志"""
        log = self._load_json(DYNAMIC_LOG_FILE, [])
        if not log:
            yield event.plain_result("📝 还没有动态记录")
            return
        lines = ["📝 最近动态记录", "━━━━━━━━━━━━"]
        for i, l in enumerate(log[-10:], 1):
            img = "🖼️" if l.get("has_image") else "📄"
            lines.append(f"{i}. [{l.get('time','')}] {img}")
            lines.append(f"   {l.get('text','')[:60]}...")
        yield event.plain_result("\n".join(lines))

    @filter.command("bili帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result("📺 BiliBot 命令\n━━━━━━━━━━━━\n/bili登录 — 扫码登录\n/bili确认 — 确认扫码\n/bili状态 — 运行状态\n/bili计划 — 查看今日主动/动态时间\n/bili分区 — 查看B站分区编号（配置视频池用）\n/bili启动 — 启动\n/bili停止 — 停止\n/bili主动 — 立刻触发一次主动看视频\n/bili开关 — 功能开关\n/bili刷新 — 刷新Cookie\n/bili记忆 — 搜索记忆\n/bili好感 — 好感度\n/bili拉黑 — 手动拉黑\n/bili解黑 — 解除拉黑\n/bili黑名单 — 查看黑名单\n/bili性格 — 查看性格演化\n/bili性格编辑 — 手动编辑性格\n/bili性格删除 — 删除演化条目\n/bili日志 — 今日视频/评论日志\n/bili永久记忆 — 查看/删除永久记忆\n/bili动态 — 手动发动态\n/bili动态日志 — 动态记录\n/bili绑定 — 绑定QQ与B站UID\n/bili解绑 — 解除绑定\n/bili清理 — 清理临时文件\n/bili帮助 — 本帮助\n━━━━━━━━━━━━\n💡 首次用 /bili登录\n💡 直接在聊天里让 Bot 去随机看B站视频，也会尝试触发一次主动看视频")

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

    @filter.on_llm_request()
    async def inject_bili_memory(self, event: AstrMessageEvent, req: ProviderRequest):
        """QQ对话提到B站相关关键词时，注入B站侧的永久记忆"""
        try:
            await self._maybe_trigger_proactive_from_llm(event, req)
            msg = event.message_str or ""
            if not any(kw in msg.lower() for kw in BILI_MENTION_KEYWORDS):
                return
            qq_id = str(event.get_sender_id())
            bindings = self._load_json(BINDING_FILE, {})
            if qq_id not in bindings:
                return
            bili_uid = bindings[qq_id]
            perm = self._load_json(PERMANENT_MEMORY_FILE, [])
            perm_texts = [f"[{p.get('time','?')}] {p['text']}" for p in perm[-10:] if p.get("text")]
            semantic_texts = await self._get_user_semantic_memories(bili_uid, msg)
            if not perm_texts and not semantic_texts:
                return
            sections = []
            if perm_texts:
                sections.append("【B站侧长期记忆】\n" + "\n".join(perm_texts))
            if semantic_texts:
                sections.append("【与该用户相关的B站/跨平台记忆】\n" + "\n".join(semantic_texts[:5]))
            req.system_prompt += f"\n\n【该用户已绑定B站UID:{bili_uid}】\n" + "\n\n".join(sections)
            logger.debug(f"[BiliBot] QQ→B站记忆注入：perm={len(perm_texts)} semantic={len(semantic_texts)}")
        except Exception as e:
            logger.error(f"[BiliBot] 记忆注入失败: {e}")

    @filter.on_llm_response()
    async def capture_qq_memory(self, event: AstrMessageEvent, resp: LLMResponse):
        """抓取QQ对话，存入qq_memory.json供B站侧语义检索"""
        try:
            qq_id = str(event.get_sender_id())
            bindings = self._load_json(BINDING_FILE, {})
            if qq_id not in bindings:
                return
            user_msg = (event.message_str or "").strip()
            ai_reply = (resp.completion_text or "").strip() if resp and resp.completion_text else ""
            if not user_msg or len(user_msg) < 5 or not ai_reply:
                return
            if user_msg.startswith("/"):
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            username = event.get_sender_name() or qq_id
            bili_uid = bindings[qq_id]
            text = f"[QQ|{now}] {username}说：{user_msg[:100]} | 回复：{ai_reply[:100]}"
            emb = await self._get_embedding(text)
            rec = {"rpid": f"qq_{int(datetime.now().timestamp())}","thread_id":"qq","user_id":bili_uid,"time":now,"text":text,"source":"qq"}
            if emb: rec["embedding"] = emb
            qq_mem = self._load_json(QQ_MEMORY_FILE, [])
            qq_mem.append(rec)
            self._save_json(QQ_MEMORY_FILE, qq_mem)
            logger.debug(f"[BiliBot] QQ记忆存入: {text[:50]}")
        except Exception as e:
            logger.error(f"[BiliBot] QQ记忆捕获失败: {e}")
