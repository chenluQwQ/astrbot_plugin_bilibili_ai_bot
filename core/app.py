"""WebUI、工具、命令看得见的插件：一组有名字的公开方法。

它是 ``HostView``：每个方法一行转发到对应模块，``HOST_API`` 就是这些入口背后用到的全部内部名字。
WebUI 和工具只经 ``app_of(plugin)`` 拿它，不碰 ``plugin._xxx``。

另外几样是 WebUI / 工具自己的状态，以前借住在插件上，现在住这里（见 ``__init__``）。
"""
from __future__ import annotations

import os
from datetime import datetime

from .config import (
    DATA_DIR, LAYERED_DB_FILE, MEMORY_FILE)
from .domain.preferences import Preferences
from .domain.user_profiles import normalize as normalize_user_profile
from .infra.host_view import HostView
from .storage.migration import imported_json_files, stow_json_files

BILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


def app_of(plugin):
    """插件的 ``BotApp``；测试里的假插件没有就现建一个挂上去。"""
    app = getattr(plugin, "app", None)
    if app is None:
        app = plugin.app = BotApp(plugin)
    return app


class BotApp(HostView):
    HOST_API = frozenset({
        "activity_logs", "activities", "affection", "bangumi", "bangumi_memory", "processed_keys", "people", "memory_docs", "states",
        "bili_auth", "bili_bangumi", "bili_comments", "bili_dynamics", "bili_http", "bili_lookups", "bili_messages",
        "bili_users", "bili_videos", "cache_files", "comment_context", "day_schedules", "dynamic_post", "environment",
        "json_store",
        "memories", "memory_entry", "memory_recall", "mood", "now_doing", "owner_recommend", "proactive_browse",
        "qq_chat", "roles", "schedule_files", "schedule_rules", "share", "share_links", "share_reaction",
        "share_session", "summaries",
        "taste", "video_pools", "watch_on_request",
    })

    def __init__(self, host):
        super().__init__(host)
        # WebUI 兴趣页：缓存、连续失败次数、熔断到什么时候、最后一次读成功的结果
        self.web_interest_cache = None
        self.web_interest_failures = 0
        self.web_interest_circuit_open_until = 0.0
        self.web_interest_last_good = None
        # WebUI 记忆页的「清理无关记忆」
        self.memory_cleanup_task = None
        self.memory_cleanup_status = None
        # 工具看片转后台：进行中的任务（按视频）和看完后发消息的任务
        self.tool_watch_jobs = {}
        self.tool_watch_followup_tasks = set()
        # 扩展：WebUI 注册时建好，卸载时关掉
        self.extension_registry = None
        self.extension_dispatcher = None
        # /bili登录 生成的二维码，等 /bili确认
        self.login_qrcode_key = None

    # 配置：AstrBot 的配置对象（改了要 save_config）和读成有类型的 settings
    @property
    def config(self):
        return self._host.config

    @property
    def settings(self):
        return self._host.settings

    # 已经是公开接口的状态主人和运行时，原样给出去
    @property
    def user_profiles(self):
        return self._host.user_profiles

    @property
    def affection(self):
        return self._host.affection

    @property
    def blocklist(self):
        return self._host.blocklist

    @property
    def security_log(self):
        return self._host.security_log

    @property
    def personality(self):
        return self._host.personality

    @property
    def personality_evolution(self):
        return self._host.personality_evolution

    @property
    def replied_comments(self):
        return self._host.replied_comments

    def preferences(self):
        return Preferences(self._host)

    @property
    def memory_api(self):
        return getattr(self._host, "memory_api", None)

    @property
    def layered_runtime(self):
        return getattr(self._host, "layered_runtime", None)

    @property
    def event_runtime(self):
        return getattr(self._host, "event_runtime", None)

    @property
    def astrbot_context(self):
        """AstrBot 的 Context：工具管理器、插件列表、模型列表。"""
        return getattr(self._host, "context", None)

    # 运行和登录
    def is_running(self):
        return bool(getattr(self._host, "_running", False))

    async def start(self):
        return await self._host.runner.start()

    async def stop(self):
        return await self._host.runner.stop()

    def has_cookie(self):
        return self.bili_http._has_cookie()

    async def check_cookie(self):
        return await self.bili_auth.check_cookie()

    async def refresh_cookie(self):
        return await self.bili_auth.refresh_cookie()

    def environment_status(self):
        return self.environment._get_environment_status()

    def cleanup_temp_files(self):
        return self.cache_files._cleanup_temp_files()

    async def bili_nav(self):
        return await self.bili_http._http_get(BILI_NAV_URL)

    async def qr_login_generate(self):
        return await self.bili_auth._qr_login_generate()

    async def qr_login_poll(self, key):
        return await self.bili_auth._qr_login_poll(key)

    # 日程
    def schedule_snapshot(self):
        return self.day_schedules._get_schedule_snapshot()

    async def ensure_daily_plan(self, force=False):
        # 本类自己有个 daily_plan()（读计划文件），每日计划对象从插件上取
        return await self._host.daily_plan._ensure_autonomous_daily_plan(force=force)

    def clear_day_schedules(self):
        return self.day_schedules._clear_day_schedules()

    def override_day_schedule(self, kind, times, done, *, update_checked=None):
        return self.day_schedules._override_day_schedule(kind, times, done, update_checked=update_checked)

    def set_proactive_windows(self, windows):
        self.schedule_files._proactive_windows = windows

    def parse_time(self, value):
        return self.schedule_rules._parse_time_value(value)

    def parse_window(self, value):
        return self.schedule_rules._parse_window_value(value)

    def is_awake_minute(self, minute):
        return self.schedule_rules._is_awake_minute(minute)

    def daily_plan_fingerprint(self):
        return self._host.daily_plan._autonomous_config_fingerprint()

    def daily_plan(self):
        return self.states.daily_plan.read({})

    def save_daily_plan(self, plan):
        return self.states.daily_plan.write(plan)

    def run_activity_now(self, kind, **kwargs):
        return self.activities._run_activity_now(kind, **kwargs)

    def video_pool_summary(self):
        return self.video_pools._format_video_pool_config()

    def taste_window_days(self):
        return self.taste._taste_window_days()

    def owner_recommend_delivery(self):
        return self.owner_recommend._owner_recommend_delivery()

    def proactive_trigger_log(self):
        return self.activity_logs.trigger.recent()

    def record_proactive_trigger(self, entry):
        self.activity_logs.trigger.append(entry)

    async def run_weekly_summary(self):
        return await self.summaries.run_weekly_summary()

    # 今天做了什么
    def reply_log(self):
        return self.activity_logs.reply.recent()

    def watch_log(self):
        return self.activity_logs.watch.recent()

    def bangumi_watch_log(self):
        return self.activity_logs.bangumi.recent()

    def comment_context_log(self, target_date, date_given=False):
        return self.comment_context._render_comment_context_log(target_date, date_given)

    async def test_comment_context(self, bvid, question=""):
        return await self.comment_context._test_comment_context(bvid, question)

    def prune_activity_logs(self):
        """``/bili清理 all`` 用：删 30 天前的活动日志，返回每种删了几条。"""
        return self.activity_logs.prune()

    def forget_schedule_states(self):
        """WebUI 改了日程配置、手动重新生成时：删掉今天的计划和五份日程，下次读到空的就重新生成。"""
        for name in ("daily_plan", "schedule", "dynamic_schedule", "dynamic_watch_schedule", "bangumi_schedule",
                     "special_follow_schedule"):
            getattr(self.states, name).delete()

    def export_json(self):
        """``/bili导出`` 用：数据库里的数据按旧格式写回 json，返回写了哪些文件。"""
        return self.activity_logs.export_json() + self.processed_keys.export_json() + self.people.export_json() + self.memory_docs.export_json() + self.states.export_json()

    def old_json_files(self):
        """``/bili清理旧`` 用：已经完整进库、之后没再改过的旧 json，``[(文件名, 字节数)]``。

        有哪组数据没接上数据库就返回 None：那组这会儿还在读写 json，不能挪。
        """
        groups = (self.activity_logs, self.processed_keys, self.people, self.memory_docs, self.states)
        if not all(group.in_database for group in groups):
            return None
        names = imported_json_files(LAYERED_DB_FILE, DATA_DIR)
        return [(name, os.path.getsize(os.path.join(DATA_DIR, name))) for name in names]

    def stow_old_json(self, names):
        return stow_json_files(DATA_DIR, names)

    def dynamic_log(self):
        return self.activity_logs.dynamic.recent()

    def proactive_log(self):
        return self.activity_logs.proactive.recent()

    # 心情、关系、画像、兴趣
    def today_mood(self):
        return self.mood.today()

    def relationship_level(self, score, uid=None):
        return self.affection.level(score, uid)

    def normalize_user_profile(self, raw):
        return normalize_user_profile(raw)

    def interest_report(self, lifecycle_items):
        return self.taste._format_interest_report(lifecycle_items=lifecycle_items)

    # 记忆和缓存
    async def count_memories(self, **filters):
        """按条件数记忆（条件见 ``Memories.find``）；不交出整份列表。"""
        memories = getattr(self._host, "memories", None)
        return await memories.count(**filters) if memories is not None else 0

    async def find_memories(self, **filters):
        memories = getattr(self._host, "memories", None)
        return await memories.find(**filters) if memories is not None else []

    async def search_memories(self, query_text, limit=5, source=None, memory_types=None,
                              user_id=None, score_threshold=0.5, *, reader_scope):
        return await self.memory_recall._search_memories(
            query_text, limit=limit, source=source, memory_types=memory_types,
            user_id=user_id, score_threshold=score_threshold, reader_scope=reader_scope,
        )

    def memory_consolidation(self):
        return getattr(self._host, "_consolidation", None)

    @property
    def memory_write_lock(self):
        return self.memories.lock

    async def replace_memory_snapshot(self, assume_locked=False):
        return await self.memories.sync(assume_locked=assume_locked)

    def memory_file_entries(self):
        """``memory.json`` 原样读出来（回复日志的旧数据从这里补）。"""
        return self.json_store._load_json(MEMORY_FILE, [])

    def permanent_memories(self):
        return self._host.self_memories.entries()

    def delete_permanent_memory(self, number):
        """删第几条（从 1 开始），返回删掉的文字；没有这一条抛 ``SelfMemoryRejected``。"""
        return self._host.self_memories.delete(number)

    def open_memos(self):
        """备忘录里还开着的（待办、已排），最新的在前。"""
        memos = getattr(self._host, "memos", None)
        return memos.open_items() if memos is not None else []

    def drop_memo(self, memo_id, reason="主人删除"):
        memos = getattr(self._host, "memos", None)
        return bool(memos is not None and memos.drop(memo_id, reason))

    def cache_stats(self):
        return self.cache_files._cache_stats()

    def purge_cache(self, mode="normal"):
        return self.cache_files._purge_cache(mode)

    # B站站内只读查询（参数原样交给 B站接口那层）
    async def search_users(self, keyword, **options):
        return await self.bili_users.search_bilibili_users(keyword, **options)

    async def search_videos(self, keyword, **options):
        return await self.bili_videos.search_bilibili_videos(keyword, **options)

    async def search_bangumi(self, keyword, **options):
        return await self.bili_bangumi.search_bilibili_bangumi(keyword, **options)

    async def up_info(self, mid):
        return await self.bili_users.get_up_info(mid)

    async def up_recent_videos(self, mid, **options):
        return await self.bili_users.get_up_recent_videos(mid, **options)

    async def up_recent_dynamics(self, mid, **options):
        return await self.bili_dynamics.get_up_recent_dynamics(mid, **options)

    async def public_fav_folders(self, mid, limit=10):
        return await self.bili_videos._get_public_fav_folders(mid, limit=limit)

    async def fav_folder_videos(self, media_id, ps=20):
        return await self.bili_videos._get_fav_folder_videos(media_id, ps=ps)

    async def following_updates(self, **options):
        return await self.bili_dynamics.get_following_updates(**options)

    async def following_live(self):
        return await self.bili_users.get_following_live()

    async def bangumi_detail(self, **options):
        return await self.bili_bangumi.get_bangumi_detail(**options)

    async def bangumi_trending(self, **options):
        return await self.bili_bangumi.get_bangumi_trending(**options)

    async def bangumi_timeline(self, **options):
        return await self.bili_bangumi.get_bangumi_timeline(**options)

    # 工具：身份、番剧、看片
    def is_owner(self, uid):
        return self.roles.is_owner(uid)

    def bangumi_memory(self):
        return self.bangumi_memory.all()

    async def recall_own_memory(self, kind, keyword, target=""):
        """QQ 的回忆工具和 B站回复前查资料共用 ``bili_lookups.memory``；QQ 这边只有管理员能调，按管理员读。"""
        from .infra.tool_loop import ToolMiss
        try:
            return await self.bili_lookups.memory(kind, keyword, reader_scope="admin", target=target)
        except ToolMiss as miss:
            return miss.detail

    async def followed_bangumi(self, follow_status=0):
        return await self.bili_bangumi._get_followed_bangumi(follow_status=follow_status)

    async def watch_video(self, video_id, memory_source="tool_watch", *, force_rewatch=False):
        return await self.watch_on_request._watch_video_and_save_memory(
            video_id, memory_source=memory_source, force_rewatch=force_rewatch,
        )

    async def watch_videos_report(self):
        if not self.bili_http._has_cookie():
            return "未登录B站，无法执行主动看视频。请先使用 /bili登录 完成扫码登录。"
        if not self.settings.get("ENABLE_PROACTIVE"):
            return "主动看视频功能当前未开启。请先使用 /bili开关 主动 开启。"
        if self.activities._run_activity_now("proactive", max_watch=1) is None:
            return "已有主动看视频任务正在运行，无需重复触发。"
        self.activity_logs.trigger.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "llm_tool",
            "scheduled": "bili_watch_videos",
            "status": "triggered",
        })
        return "已在后台触发一次主动看B站视频流程。稍后可用 /bili日志 视频 查看结果。"

    async def watch_bangumi_report(self, season_id=None, ep_id=None):
        return await self.bangumi.watch_on_request(season_id=season_id, ep_id=ep_id)

    # 工具：QQ 分享
    def collect_share_text(self, event, include_reply=False):
        return self.share_links._collect_share_text(event, include_reply=include_reply)

    def handle_share(self, event, text_override=None, trigger_mode="auto", show_errors=False):
        return self.share._handle_bili_share(
            event, text_override=text_override, trigger_mode=trigger_mode, show_errors=show_errors,
        )

    def share_scene(self, event):
        return self.share_session._share_scene(event)

    # QQ 对话调模型前
    async def maybe_trigger_proactive_from_llm(self, event, req):
        return await self.proactive_browse._maybe_trigger_proactive_from_llm(event, req)

    def inject_recent_group_share(self, event, req):
        return self.share_session._inject_recent_group_share_into_request(event, req)

    def inject_context_block(self, req, block):
        return self.qq_chat._inject_context_block_before_user(req, block)

    def cross_platform_activity_context(self):
        return self.now_doing.prompt()

    def qq_bindings(self):
        return self.people.qq_bindings.read({})

    def save_qq_bindings(self, bindings):
        return self.people.qq_bindings.write(bindings)

    async def send_watch_followup(self, umo, watch_message, *, scene, user_text, failed=False):
        return await self.share_reaction._send_tool_watch_followup(
            umo, watch_message, scene=scene, user_text=user_text, failed=failed,
        )

    async def send_private_video_share(self, receiver_mid, video_info):
        return await self.bili_messages._send_bili_private_video_share(receiver_mid, video_info)

    # 工具：站内操作
    async def video_aid(self, bvid):
        return await self.bili_videos._get_video_oid(bvid)

    async def comment(self, oid, text):
        return await self.bili_comments._send_comment(oid, text)

    async def comment_detailed(self, oid, text, *, sync_to_dynamic=False):
        """``(成功, 原因)``，原因见 ``BiliComments._send_comment_detailed``。"""
        return await self.bili_comments._send_comment_detailed(oid, text, sync_to_dynamic=sync_to_dynamic)

    async def delete_comment(self, oid, reply_type, rpid):
        """``(成功, 原因)``，原因见 ``BiliComments._delete_comment``。"""
        return await self.bili_comments._delete_comment(oid, reply_type, rpid)

    def record_owner_feedback(self, liked, points, bvid=""):
        return self._host.owner_feedback.record(liked, points, bvid)

    def comment_syncs(self):
        return self.dynamic_post.comment_syncs()

    def comment_synced(self, key, text, title=""):
        self.dynamic_post.comment_synced(key, text, title)

    async def like(self, aid):
        return await self.bili_videos._like_video(aid)

    async def coin(self, aid, num=1):
        return await self.bili_videos._coin_video(aid, num=num)

    async def favorite(self, aid):
        return await self.bili_videos._fav_video(aid)

    async def follow(self, mid):
        return await self.bili_users._follow_user(mid)

    async def block(self, mid):
        return await self.bili_users._block_user(mid)

    async def unblock(self, mid):
        """解除拉黑；成功返回 True。请求出错原样抛出（结果未知是 ``WriteResultUnknownError``）。"""
        return await self.bili_users._unblock_user(mid)
