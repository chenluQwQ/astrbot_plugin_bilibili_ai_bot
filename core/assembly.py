"""装配：插件对象建好后，把各个状态主人、运行时、启停、工具和 WebUI 挂上去。

``main.py`` 的 ``__init__`` 只调 ``assemble``。各模块挂在插件对象上，彼此通过 ``HostView`` 的依赖清单访问。
"""
from __future__ import annotations

import os

from astrbot.api import logger
from .app import BotApp
from .config import (
    MEMORY_DOC_FILES, PEOPLE_FILES, DATA_DIR, LAYERED_DB_FILE, MEMORY_FILE,
    WEB_SEARCH_CACHE_FILE,
    STATE_FILES)
from .consolidation import ConsolidationEngine
from .domain._memory_entry import MemoryEntry
from .domain.affection import Affection, Milestones
from .domain.bangumi_memory import BangumiMemory
from .domain.blocklist import BlockList
from .domain.up_skip import UpSkip
from .domain.dynamic_log import DynamicLog
from .domain.memory import memories_for
from .domain.mood import Mood
from .domain.personality import Personality
from .domain.preferences import Preferences
from .domain.private_inbox import PrivateInbox
from .domain.replied_comments import RepliedComments
from .domain.replied_content import RepliedContent
from .domain.seen_videos import SeenVideos
from .domain.reply_log import ReplyLog
from .domain.summary_archive import SummaryArchive
from .domain.taste import Taste
from .domain.user_profiles import UserProfiles
from .domain.profile_candidates import ProfileCandidates
from .domain.self_memory import SelfMemories
from .domain.video_discovery import VideoDiscovery
from .domain.video_watch import VideoWatch
from .features.bangumi import Bangumi
from .features._abuse_alert import AbuseAlert
from .features._memo_tool import MemoTool
from .features._self_memory_tool import SelfMemoryTool
from .features.memo_review import MemoReview
from .features.dynamic_sources import DynamicSources
from .features.favorite_digest import FavoriteDigest
from .features._tell_owner import TellOwner
from .features._owner_feedback import OwnerFeedback
from .features.memo_watch import MemoWatch
from .features._reply_consequences import ReplyAftermath
from .features._reply_gate import ReplyGate
from .features._reply_writing import ReplyWriting
from .features.comment_context import CommentContext
from .features.comment_reply import CommentReply
from .features.dynamic_post import DynamicPost
from .features._dynamic_context import DynamicContext
from .features.dynamic_watch import DynamicWatch
from .features.memory_compress import MemoryCompress
from .features.memory_recall import MemoryRecall
from .features.memory_startup import MemoryStartup
from .features.memory_writer import MemoryWriter
from .features._outward_actions import OutwardActions
from .features._owner_recommend import OwnerRecommend
from .features._private_lookup import PrivateLookup
from .features._bili_lookups import BiliLookups
from .features._private_safety import PrivateSafety
from .features.personality_evolution import PersonalityEvolution
from .features.private_poll import PrivatePoll
from .features.private_reply import PrivateReply
from .features.proactive_browse import ProactiveBrowse
from .features.reply_context import ReplyContext
from .features._activity_digest import ActivityDigest
from .features.search_judge import SearchJudge
from .features.share import ShareHandler
from .features.special_follow import SpecialFollow
from .features.summaries import Summaries
from .features._video_evaluate import VideoEvaluate
from .features._watch_video import WatchVideo
from .features._video_context import VideoContext
from .features.watch_on_request import WatchOnRequest
from .features._share_delivery import ShareDelivery
from .features._share_reaction import ShareReaction
from .features._share_session import ShareSession
from .infra.bili.auth import BiliAuth
from .infra.bili.bangumi import BiliBangumi
from .infra.bili.comments import BiliComments
from .infra.bili.dynamics import BiliDynamics
from .infra.bili.http import BiliHttp
from .infra.bili.users import BiliUsers
from .infra.bili.videos import BiliVideos
from .infra.bili.wbi import BiliWbi
from .infra.bili.video_pools import BiliVideoPools
from .infra.bili.messages import BiliMessages
from .infra.bili.share_links import ShareLinks
from .infra.cache_files import CacheFiles
from .infra.environment import Environment
from .infra.models import Models
from .infra.process import Processes
from .infra.qq_chat import QQChat
from .infra.search import WebSearch
from .infra.summary_image import SummaryImage
from .infra.vision import Vision
from .infra.video_files import VideoFiles
from .infra.ytdlp_update import YtDlpUpdater
from .layered_runtime import LayeredRuntime
from .lifecycle.now_doing import NowDoing
from .lifecycle.schedule_rules import ScheduleRules
from .memory_api import BiliBotMemoryAPI
from .runner import Runner
from .activities import Activities
from .day_schedules import DaySchedules
from .features.daily_plan import DailyPlan
from .schedule_files import ScheduleFiles
from .runtime import EventRuntime
from .security.inbound import InboundGuard
from .security.log import SecurityLog
from .security.roles import Roles
from .storage.activity_log import ActivityLogs
from .storage.keyed_json import KeyedDocs, StateDocs
from .storage.processed_keys import ProcessedKeys
from .storage.json_file import JsonFile
from .storage.json_store import JsonStore


def assemble(bot):
    bot.json_store = JsonStore()
    bot.cache_files = CacheFiles(bot)
    bot.cache_files._ensure_data_dir()
    bot.processes = Processes()
    bot.environment = Environment(bot)
    bot.bili_http = BiliHttp(bot)
    bot._running = False
    bot.runner = Runner(bot)
    bot.bili_auth = BiliAuth(bot)
    bot.bili_wbi = BiliWbi(bot)
    bot.bili_comments = BiliComments(bot)
    bot.bili_videos = BiliVideos(bot)
    bot.bili_dynamics = BiliDynamics(bot)
    bot.models = Models(bot)
    bot._proactive_task = None
    bot._bangumi_task = None
    bot.now_doing = NowDoing()
    bot.schedule_rules = ScheduleRules(lambda: bot.settings)
    # 五种活动今天的日程存在 activity_runs（Runner.initialize 里打开），只经 day_schedules 读写。
    bot.day_schedules = DaySchedules(bot)
    bot.schedule_files = ScheduleFiles(bot)
    bot.daily_plan = DailyPlan(bot)
    bot.activities = Activities(bot)
    # 去重账本（回复过的评论、@、评论过的视频、语义去重库）：同上，启动时接上 sqlite
    bot.processed_keys = ProcessedKeys(DATA_DIR)
    # 小状态（心情、日程、各种冷却和游标）：整份一行，同上
    bot.states = StateDocs(DATA_DIR, STATE_FILES)
    bot.replied_comments = RepliedComments(bot.processed_keys.comment, bot.processed_keys.at)
    bot.replied_content = RepliedContent(bot.processed_keys.content)
    # 十种活动日志：先读写 json，Runner.initialize 里接上 sqlite
    bot.activity_logs = ActivityLogs(DATA_DIR)
    bot.reply_log = ReplyLog(bot.activity_logs.reply)
    bot.roles = Roles(lambda: bot.settings)
    # 人和关系（好感度、画像、里程碑、画像候选、QQ 绑定）：同上，启动时接上 sqlite；
    # 主人满分、补画像门槛标记在接上之后才做（Runner.initialize），免得拿过期的 json 写回去
    bot.people = KeyedDocs(DATA_DIR, PEOPLE_FILES)
    # 番剧进度、视频记忆、外部记忆：同一张表，一行一部番 / 一个视频
    bot.memory_docs = KeyedDocs(DATA_DIR, MEMORY_DOC_FILES)
    bot.affection = Affection(
        bot.people.affection, bot.roles.is_owner, bot.people.profile_reached,
        lambda: bot.settings.int("PROFILE_AFFECTION_LINE", minimum=1, maximum=100),
    )
    bot.milestones = Milestones(bot.people.milestones)
    bot.user_profiles = UserProfiles(bot.people.user_profiles)
    bot.self_memories = SelfMemories(bot.states.permanent_memory)
    bot.profile_candidates = ProfileCandidates(
        bot.people.profile_candidates, bot.affection.reached_profile_line, bot.roles.is_owner,
    )
    bot.private_inbox = PrivateInbox(bot.states.private_messages)
    bot.bili_messages = BiliMessages(bot)
    bot.private_poll = PrivatePoll(bot)
    bot.private_reply = PrivateReply(bot)
    bot.private_lookup = PrivateLookup(bot)
    bot.bili_lookups = BiliLookups(bot)
    bot.private_safety = PrivateSafety(bot)
    bot.security_log = SecurityLog(bot.activity_logs.security)
    bot.inbound_guard = InboundGuard(bot.security_log)
    bot.bili_users = BiliUsers(bot)
    bot.bangumi_memory = BangumiMemory(bot.memory_docs.bangumi_memory)
    bot.bili_bangumi = BiliBangumi(bot)
    bot.bangumi = Bangumi(bot)
    bot.dynamic_log = DynamicLog(bot.activity_logs.dynamic)
    bot.personality = Personality(bot.states.personality)
    bot.personality_evolution = PersonalityEvolution(bot)
    bot.mood = Mood(bot.states.mood, lambda: bool(bot.settings.get("ENABLE_MOOD")))
    bot.yt_dlp_updater = YtDlpUpdater(bot, JsonFile(os.path.join(DATA_DIR, "ytdlp_update.json")))
    bot.blocklist = BlockList(
        bot.states.block_log,
        bot.states.block_count,
    )
    bot.up_skip = UpSkip(bot.states.up_skip)
    bot.memory_entry = MemoryEntry(bot)
    bot.memories = memories_for(bot)
    bot.memories.load_backup(bot.json_store._load_json(MEMORY_FILE, []))
    bot.seen_videos = SeenVideos(bot)
    bot.memory_startup = MemoryStartup(bot)
    bot.memory_writer = MemoryWriter(bot)
    bot.memory_recall = MemoryRecall(bot)
    bot.memory_compress = MemoryCompress(bot)
    bot._embed_client = None
    bot._video_vision_client = None
    bot._image_vision_client = None
    bot._web_search_client = None
    bot._dynamic_task = None
    bot._special_follow_task = None
    bot._dynamic_watch_task = None
    bot.vision = Vision(bot)
    bot.video_files = VideoFiles(bot)
    bot.video_watch = VideoWatch(bot)
    bot.watch_on_request = WatchOnRequest(bot)
    bot.video_context = VideoContext(bot)
    bot.dynamic_context = DynamicContext(bot)
    bot.reply_context = ReplyContext(bot)
    bot.web_search = WebSearch(bot, JsonFile(WEB_SEARCH_CACHE_FILE))
    bot.search_judge = SearchJudge(bot)
    bot.app = BotApp(bot)
    bot.memo_tool = MemoTool(bot)
    bot.self_memory_tool = SelfMemoryTool(bot)
    bot.memo_review = MemoReview(bot)
    bot.favorite_digest = FavoriteDigest(bot)
    bot.tell_owner = TellOwner(bot)
    bot.owner_feedback = OwnerFeedback(bot)
    bot.memo_watch = MemoWatch(bot)
    bot.comment_context = CommentContext(bot)
    bot.reply_gate = ReplyGate(bot)
    bot.reply_writing = ReplyWriting(bot)
    bot.reply_aftermath = ReplyAftermath(bot)
    bot.abuse_alert = AbuseAlert(bot)
    bot.comment_reply = CommentReply(bot)
    bot.dynamic_post = DynamicPost(bot)
    bot.dynamic_sources = DynamicSources(bot)
    bot.share_links = ShareLinks(bot)
    bot.qq_chat = QQChat(bot)
    bot.share_session = ShareSession(bot)
    bot.share_delivery = ShareDelivery(bot)
    bot.share_reaction = ShareReaction(bot)
    bot.share = ShareHandler(bot)
    bot.activity_digest = ActivityDigest(bot)
    bot.summary_image = SummaryImage(bot)
    bot.summary_archive = SummaryArchive(bot)
    bot.summaries = Summaries(bot)
    bot.preferences = Preferences(bot)
    bot.video_pools = BiliVideoPools(bot)
    bot.taste = Taste(bot)
    bot.video_discovery = VideoDiscovery(bot)
    bot.video_evaluate = VideoEvaluate(bot)
    bot.outward_actions = OutwardActions(bot)
    bot.owner_recommend = OwnerRecommend(bot)
    bot.watch_video = WatchVideo(bot)
    bot.proactive_browse = ProactiveBrowse(bot)
    bot.special_follow = SpecialFollow(bot)
    bot.dynamic_watch = DynamicWatch(bot)
    bot.layered_runtime = LayeredRuntime(bot.config, LAYERED_DB_FILE)
    try:
        action_timeout = float(
            bot.settings.get("BEHAVIOR_ACTION_TIMEOUT_SECONDS") or 45
        )
    except (TypeError, ValueError):
        action_timeout = 45
    bot.event_runtime = EventRuntime(
        observer=bot.layered_runtime,
        action_timeout=action_timeout,
    )
    bot.environment._log_environment_warnings()
    # ── 记忆清算引擎 & 外部接口 ──
    bot._consolidation = ConsolidationEngine(bot)
    bot.memory_api = BiliBotMemoryAPI(bot)

    # 注册 FunctionTool 工具（结果回到 LLM 重新生成）
    from .tools import create_tools
    llm_tools = create_tools(bot)
    bot.layered_runtime.register_tools(llm_tools)
    bot.context.add_llm_tools(*llm_tools)
    tool_names = ", ".join(tool.name for tool in llm_tools)
    logger.info(f"[BiliBot] LLM工具已精简注册: {len(llm_tools)} 个 ({tool_names})")

    # 注册 WebUI 控制面板
    from .web import register_webui
    register_webui(bot, bot.context)
