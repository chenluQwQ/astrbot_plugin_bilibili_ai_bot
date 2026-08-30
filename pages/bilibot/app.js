import { icon } from "./icons.js";

const bridge = window.AstrBotPluginPage || null;
const isPreview = location.search.includes("preview=1") || !bridge;
// ?ext=<id> boots straight into one extension with no host shell behind it, so the
// workspace can be opened in its own tab and inspected without the sidebar in the
// way.  Empty unless asked for, which keeps the embedded path unchanged.
const standaloneExtensionId = new URLSearchParams(location.search).get("ext") || "";
const app = document.querySelector("#app");
const content = document.querySelector("#content");
const sidebar = document.querySelector("#sidebar");
const saveDock = document.querySelector("#save-dock");
const toastRegion = document.querySelector("#toast-region");
const modalRoot = document.querySelector("#modal-root");
const moduleUrl = new URL(import.meta.url);

function pageAssetUrl(relativePath) {
  const assetUrl = new URL(relativePath, moduleUrl);
  moduleUrl.searchParams.forEach((value, key) => {
    if (!assetUrl.searchParams.has(key)) assetUrl.searchParams.set(key, value);
  });
  return assetUrl.href;
}

const brandLogoUrl = pageAssetUrl("./assets/logo.png");

const state = {
  currentPage: "overview",
  // True when booted via ?ext=<id>: there is no host shell to go back to, so the
  // return control is left out rather than rendered and dead.
  standalone: false,
  schema: {},
  config: {},
  draft: {},
  dirtyKeys: new Set(),
  stats: {},
  persona: {},
  account: null,
  schedule: { events: [] },
  scheduleOriginal: { events: [] },
  scheduleDraft: { events: [] },
  scheduleDirty: false,
  scheduleGesture: null,
  scheduleStats: {},
  memory: {},
  profiles: [],
  interest: {},
  security: {},
  cache: {},
  availableTools: [],
  toolSearch: "",
  toolPickerSelection: new Set(),
  settingsSearch: "",
  selectedScheduleIndex: -1,
  autonomyDrawer: null,
  qrPollTimer: null,
  pageToken: 0,
  isSaving: false,
  mode: "host",
  hostPage: "overview",
  extensions: [],
  activeExtensionId: null,
  extensionPage: "dashboard",
  extensionSchema: null,
  extensionLoading: false,
};

const NAV_ITEMS = [
  ["overview", "house", "总览", "健康、配额与运行监控"],
  ["autonomy", "clock", "自主与作息", "活跃度、彩虹日程与范围限制"],
  ["interaction", "message", "回复与互动", "评论、私信、弹幕与分享"],
  ["memory", "memory-card", "记忆与关系", "记忆、画像、心情与好感度"],
  ["security", "shield", "安全与工具", "权限隔离、脱敏与风控"],
  ["account", "user", "账号连接", "扫码登录与主人身份"],
  ["basics", "settings", "基础设置", "人设、模型与长期配置"],
];

const PAGE_KEYS = {
  interaction: [
    "ENABLE_REPLY", "POLL_INTERVAL", "REPLY_COOLDOWN", "REPLY_PROBABILITY_PERCENT", "REPLY_ALWAYS_UIDS",
    "ENABLE_SIMILAR_SKIP", "REPLY_SIMILARITY_PERCENT", "CUSTOM_REPLY_INSTRUCTION",
    "ENABLE_INTEREST_BASED_REPLY", "INTEREST_SELECTION_PROMPT", "FILTER_LOW_VALUE_MESSAGES",
    "FILTER_DUPLICATE_MESSAGES", "FILTER_AD_MESSAGES", "INTEREST_APPLY_TO_PRIVATE",
    "ENABLE_PRIVATE_MESSAGES", "PRIVATE_MESSAGE_REPLY_SCOPE", "PRIVATE_MESSAGE_REPLY_WHITELIST_UIDS",
    "PRIVATE_MESSAGE_AUTO_REPLY", "CUSTOM_PRIVATE_MESSAGE_INSTRUCTION", "PRIVATE_MESSAGE_POLL_INTERVAL",
    "PRIVATE_MESSAGE_IDLE_POLL_INTERVAL", "PRIVATE_MESSAGE_ACTIVE_WINDOW", "PRIVATE_MESSAGE_MAX_PER_POLL",
    "PRIVATE_MESSAGE_MAX_MESSAGE_AGE", "PRIVATE_MESSAGE_AUTO_WATCH_VIDEO", "PRIVATE_MESSAGE_BILI_SEARCH_ENABLED",
    "PRIVATE_MESSAGE_BILI_SEARCH_LIMIT", "BILI_PRIVATE_SHARE_TOOL_ENABLED", "BILI_PRIVATE_SHARE_COOLDOWN",
    "ENABLE_LIVE_DANMAKU_REPLY", "LIVE_DANMAKU_ROOM_ID", "LIVE_DANMAKU_POLL_INTERVAL",
    "LIVE_DANMAKU_REPLY_COOLDOWN", "LIVE_DANMAKU_MAX_PER_MINUTE", "LIVE_DANMAKU_REPLY_MAX_LENGTH",
    "CUSTOM_LIVE_DANMAKU_INSTRUCTION", "ENABLE_BILI_SHARE_PARSE", "BILI_SHARE_PARSE_AUTO_TRIGGER_ENABLED",
    "BILI_SHARE_PARSE_MANUAL_TRIGGER_ENABLED", "BILI_SHARE_PARSE_LLM_TRIGGER_ENABLED", "BILI_SHARE_PENDING_MAX_AGE",
    "BILI_SHARE_PARSE_SEND_VIDEO", "BILI_SHARE_PARSE_SEGMENT_SECONDS", "BILI_SHARE_PARSE_MAX_SEGMENTS",
    "BILI_SHARE_PARSE_MAX_VIDEO_MB", "BILI_SHARE_PARSE_VIDEO_MAX_HEIGHT", "BILI_SHARE_PARSE_COOLDOWN",
  ],
  autonomy: [
    "ENABLE_AUTONOMOUS_DAILY_PLAN", "AUTONOMOUS_ACTIVITY_LEVEL", "AUTONOMOUS_PLAN_PROMPT", "AUTONOMOUS_PLAN_GENERATION_MODE", "AUTONOMOUS_PLAN_AFTER_SLEEP_MINUTES", "AUTONOMOUS_PLAN_GENERATION_TIME", "AUTONOMOUS_PLAN_RETRY_MINUTES", "AUTONOMOUS_PROACTIVE_WINDOW_MINUTES",
    "AUTONOMOUS_REPLY_DAILY_MAX", "AUTONOMOUS_PRIVATE_DAILY_MAX",
    "AUTONOMOUS_DYNAMIC_DAILY_MAX", "AUTONOMOUS_PROACTIVE_DAILY_MAX",
    "AUTONOMOUS_MIN_ACTION_GAP_MINUTES", "SLEEP_START", "SLEEP_END",
    "BEHAVIOR_BUDGET_ENABLED", "BEHAVIOR_GLOBAL_MAX_PER_MINUTE", "BEHAVIOR_GLOBAL_DAILY_LIMIT", "BEHAVIOR_ACTION_TIMEOUT_SECONDS",
    "ENABLE_PROACTIVE", "PROACTIVE_VIDEO_COUNT", "PROACTIVE_DAILY_LIMIT", "PROACTIVE_TIMES_COUNT",
    "PROACTIVE_COMMENT_COUNT", "PROACTIVE_COMMENT_DAILY_LIMIT", "PROACTIVE_FOLLOW_UIDS", "PROACTIVE_SEARCH_QUERY_PROMPT", "PROACTIVE_TASTE_WINDOW_DAYS",
    "PROACTIVE_VIDEO_POOLS", "ENABLE_PROACTIVE_LLM_PREFILTER", "PROACTIVE_LLM_PREFILTER_MAX_REJECTS",
    "PROACTIVE_LIKE", "PROACTIVE_LIKE_MIN_SCORE", "PROACTIVE_COIN", "PROACTIVE_COIN_MIN_SCORE",
    "PROACTIVE_FAV", "PROACTIVE_FAV_MIN_SCORE", "PROACTIVE_COMMENT", "PROACTIVE_COMMENT_MIN_SCORE",
    "PROACTIVE_FOLLOW", "PROACTIVE_FOLLOW_MIN_SCORE",
    "CUSTOM_PROACTIVE_INSTRUCTION", "ENABLE_OWNER_RECOMMEND", "RECOMMEND_OWNER_DELIVERY", "RECOMMEND_OWNER_MIN_SCORE",
    "RECOMMEND_OWNER_DAILY_LIMIT", "CUSTOM_RECOMMEND_INSTRUCTION", "OWNER_QQ_UMO", "ENABLE_CROSS_PLATFORM_ACTIVITY_STATUS", "VIDEO_VISUAL_ANALYSIS_POLICY", "ENABLE_VIDEO_LONG_TERM_MEMORY", "VIDEO_MEMORY_DETAIL_DAYS", "VIDEO_MEMORY_FADE_DAYS", "SPECIAL_FOLLOW_ENABLED", "SPECIAL_FOLLOW_MODE",
    "SPECIAL_FOLLOW_TIMES_COUNT", "SPECIAL_FOLLOW_FIXED_TIMES", "ENABLE_BANGUMI", "BANGUMI_PROACTIVE",
    "BANGUMI_POOLS", "BANGUMI_EPISODE_COUNT", "BANGUMI_CONTINUE_SCORE", "BANGUMI_DAILY_LIMIT",
    "BANGUMI_COMMENT", "BANGUMI_AUTO_FOLLOW", "ENABLE_DYNAMIC", "DYNAMIC_TIMES_COUNT", "DYNAMIC_DAILY_COUNT",
    "DYNAMIC_TOPICS", "CUSTOM_DYNAMIC_INSTRUCTION",
    "ENABLE_DYNAMIC_WATCH", "DYNAMIC_WATCH_TIMES_COUNT", "DYNAMIC_WATCH_DAILY_LIMIT",
    "DYNAMIC_WATCH_SPECIAL_ONLY", "DYNAMIC_WATCH_INCLUDE_VIDEO_POSTS", "DYNAMIC_WATCH_INTEREST_PROMPT",
    "FIXED_PROACTIVE_WINDOWS", "FIXED_PROACTIVE_TIMES",
    "FIXED_DYNAMIC_TIMES", "FIXED_DYNAMIC_WATCH_TIMES", "FIXED_BANGUMI_TIMES", "FIXED_SPECIAL_FOLLOW_TIMES",
  ],
  memory: [
    "ENABLE_AFFECTION", "ENABLE_MOOD", "AFFECTION_PROMPT_SPECIAL", "AFFECTION_PROMPT_CLOSE",
    "AFFECTION_PROMPT_FRIEND", "AFFECTION_PROMPT_NORMAL", "AFFECTION_PROMPT_STRANGER", "AFFECTION_PROMPT_COLD",
  ],
  security: [
    "BILI_TOOL_ISOLATION_ENABLED", "BILI_ALLOW_SEARCH_TOOLS", "BILI_TOOL_ALLOWLIST", "ENABLE_LLM_TOOLS",
    "BILI_PROMPT_INJECTION_DEFENSE", "BILI_TOOL_AUDIT_ENABLED", "MEMORY_ISOLATION_MODE",
    "ENABLE_SAFE_CROSS_PLATFORM_MEMORY", "ENABLE_PRIVACY_REDACTION", "MEMORY_BLOCKED_PREFIXES",
    "MEMORY_BLOCKED_KEYWORDS", "CROSS_PLATFORM_MEMORY_PROMPT", "PRIVATE_MESSAGE_AUTO_BLOCK",
    "PRIVATE_MESSAGE_BLOCK_WHITELIST_UIDS", "PRIVATE_MESSAGE_TRUSTED_DOMAINS", "ABUSE_ALERT_MODE",
    "ABUSE_ALERT_QQ_UMO", "ABUSE_ALERT_SCORE_THRESHOLD", "ENABLE_AUTO_BLOCK", "BLOCK_WHITELIST_UIDS",
    "AUTO_BLOCK_SCORE", "AUTO_BLOCK_NEGATIVE_TIMES",
  ],
  account: ["OWNER_MID", "OWNER_NAME", "OWNER_BILI_NAME"],
};

const SCHEDULE_REGEN_KEYS = new Set([
  "ENABLE_AUTONOMOUS_DAILY_PLAN", "AUTONOMOUS_ACTIVITY_LEVEL", "AUTONOMOUS_PLAN_PROMPT",
  "AUTONOMOUS_REPLY_DAILY_MAX", "AUTONOMOUS_PRIVATE_DAILY_MAX",
  "AUTONOMOUS_DYNAMIC_DAILY_MAX", "AUTONOMOUS_PROACTIVE_DAILY_MAX",
  "AUTONOMOUS_MIN_ACTION_GAP_MINUTES", "SLEEP_START", "SLEEP_END",
  "ENABLE_PROACTIVE", "PROACTIVE_VIDEO_COUNT", "PROACTIVE_DAILY_LIMIT", "PROACTIVE_TIMES_COUNT",
  "ENABLE_DYNAMIC", "DYNAMIC_TIMES_COUNT", "DYNAMIC_DAILY_COUNT",
  "ENABLE_BANGUMI", "BANGUMI_PROACTIVE", "BANGUMI_DAILY_LIMIT",
  "SPECIAL_FOLLOW_ENABLED", "SPECIAL_FOLLOW_MODE", "SPECIAL_FOLLOW_TIMES_COUNT", "SPECIAL_FOLLOW_FIXED_TIMES",
  "ENABLE_DYNAMIC_WATCH", "DYNAMIC_WATCH_TIMES_COUNT", "DYNAMIC_WATCH_DAILY_LIMIT",
  "FIXED_PROACTIVE_WINDOWS", "FIXED_PROACTIVE_TIMES",
  "FIXED_DYNAMIC_TIMES", "FIXED_DYNAMIC_WATCH_TIMES", "FIXED_BANGUMI_TIMES", "FIXED_SPECIAL_FOLLOW_TIMES",
]);

const MOCK_FIELDS = {
  ENABLE_REPLY: ["【功能开关】启用评论自动回复", "bool", true],
  POLL_INTERVAL: ["【回复】评论轮询间隔（秒）", "int", 30],
  REPLY_COOLDOWN: ["【回复】回复冷却时间（秒）", "int", 15],
  REPLY_PROBABILITY_PERCENT: ["【回复】回复概率（%）", "int", 75],
  CUSTOM_REPLY_INSTRUCTION: ["【回复】回复评论的补充提示词", "text", "保持自然，不要机械复述用户内容。"],
  ENABLE_INTEREST_BASED_REPLY: ["【回复筛选】让 Bot 只挑选自己感兴趣且值得回应的内容", "bool", true],
  INTEREST_SELECTION_PROMPT: ["【回复筛选】兴趣选择提示词", "text", "优先回应真诚交流、有趣观点与明确问题。"],
  FILTER_LOW_VALUE_MESSAGES: ["【回复筛选】过滤无意义或信息量过低的消息", "bool", true],
  FILTER_DUPLICATE_MESSAGES: ["【回复筛选】过滤近期完全重复的消息", "bool", true],
  FILTER_AD_MESSAGES: ["【回复筛选】过滤广告、引流和联系方式轰炸", "bool", true],
  INTEREST_APPLY_TO_PRIVATE: ["【回复筛选】私信也使用兴趣选择", "bool", true],
  ENABLE_PRIVATE_MESSAGES: ["【B站私信·总开关】监听新私信", "bool", true],
  PRIVATE_MESSAGE_REPLY_SCOPE: ["【B站私信·回复】允许自动回复哪些人", "string", "all", ["all", "owner", "whitelist"]],
  PRIVATE_MESSAGE_AUTO_REPLY: ["【B站私信·回复】自动回复安全私信", "bool", true],
  CUSTOM_PRIVATE_MESSAGE_INSTRUCTION: ["【B站私信·回复】私信回复补充提示词", "text", "避免处理不明确的敏感请求。"],
  ENABLE_LIVE_DANMAKU_REPLY: ["【直播互动·总开关】进入指定直播间参与弹幕互动", "bool", false],
  LIVE_DANMAKU_MAX_PER_MINUTE: ["【直播互动】每分钟最多发送弹幕次数", "int", 3],
  ENABLE_BILI_SHARE_PARSE: ["【分享解析·总开关】识别B站视频分享", "bool", true],
  ENABLE_AUTONOMOUS_DAILY_PLAN: ["【自主安排】允许 Bot 根据人设与活跃度生成每日计划", "bool", true],
  AUTONOMOUS_ACTIVITY_LEVEL: ["【自主安排】今日基础活跃度（0-100）", "int", 62],
  AUTONOMOUS_PLAN_PROMPT: ["【自主安排】每日计划补充提示词", "text", "自然安排一天，低价值内容不必回复，避免短时间密集互动。"],
  AUTONOMOUS_PLAN_GENERATION_MODE: ["【自主安排】每日计划生成时机", "string", "after_sleep", ["after_sleep", "fixed_time"]],
  AUTONOMOUS_PLAN_AFTER_SLEEP_MINUTES: ["【自主安排】休眠结束后生成计划的偏移（分钟）", "int", 5],
  AUTONOMOUS_PLAN_GENERATION_TIME: ["【自主安排】每日计划固定生成时刻", "string", "08:05"],
  AUTONOMOUS_PLAN_RETRY_MINUTES: ["【自主安排】模型失败后唯一一次重试等待（分钟）", "int", 15],
  AUTONOMOUS_PROACTIVE_WINDOW_MINUTES: ["【自主安排】主动浏览默认时间段长度（分钟）", "int", 90],
  AUTONOMOUS_REPLY_DAILY_MIN: ["【兼容旧配置】每日评论回复旧版下限（已停用）", "int", 0],
  AUTONOMOUS_REPLY_DAILY_MAX: ["【自主安排·上限】每日评论回复最多几条", "int", 80],
  AUTONOMOUS_PRIVATE_DAILY_MIN: ["【兼容旧配置】每日私信回复旧版下限（已停用）", "int", 0],
  AUTONOMOUS_PRIVATE_DAILY_MAX: ["【自主安排·上限】每日私信回复最多几条", "int", 30],
  AUTONOMOUS_DYNAMIC_DAILY_MIN: ["【兼容旧配置】每日动态旧版下限（已停用）", "int", 0],
  AUTONOMOUS_DYNAMIC_DAILY_MAX: ["【自主安排·上限】每日发布动态最多几条", "int", 2],
  AUTONOMOUS_PROACTIVE_DAILY_MIN: ["【兼容旧配置】每日主动浏览旧版下限（已停用）", "int", 0],
  AUTONOMOUS_PROACTIVE_DAILY_MAX: ["【自主安排·上限】每日主动浏览轮次最多几轮", "int", 4],
  AUTONOMOUS_MIN_ACTION_GAP_MINUTES: ["【自主安排·硬约束】主动事件最小间隔（分钟）", "int", 45],
  BEHAVIOR_BUDGET_ENABLED: ["【统一行为预算】启用全局频率与每日上限", "bool", true],
  BEHAVIOR_GLOBAL_MAX_PER_MINUTE: ["【统一行为预算】每分钟最多执行动作数", "int", 8],
  BEHAVIOR_GLOBAL_DAILY_LIMIT: ["【统一行为预算】每天最多执行动作数", "int", 200],
  BEHAVIOR_ACTION_TIMEOUT_SECONDS: ["【统一行为预算】单个动作超时（秒）", "int", 45],
  SLEEP_START: ["【系统】休眠开始时间（0-23）", "int", 2],
  SLEEP_END: ["【系统】休眠结束时间（0-23）", "int", 8],
  ENABLE_PROACTIVE: ["【主动看片·总开关】启用主动看视频与互动", "bool", true],
  PROACTIVE_DAILY_LIMIT: ["【主动看片·数量】每天最多看几个视频", "int", 5],
  PROACTIVE_TIMES_COUNT: ["【主动看片·频率】每天触发几次主动浏览", "int", 2],
  PROACTIVE_VIDEO_COUNT: ["【主动看片·数量】每次计划观看几个视频", "int", 3],
  PROACTIVE_COMMENT_COUNT: ["【主动看片·互动】每个视频最多主动评论几条", "int", 1],
  PROACTIVE_COMMENT_DAILY_LIMIT: ["【主动看片·互动】每天最多主动评论几条", "int", 2],
  PROACTIVE_FOLLOW_UIDS: ["【主动看片·来源】优先关注的 UP 主 UID", "list", ["184028", "902418"]],
  PROACTIVE_SEARCH_QUERY_PROMPT: ["【主动看片·搜索】搜索词生成提示词", "text", "结合今天的心情与长期兴趣，生成自然且不过度重复的搜索词。"],
  PROACTIVE_TASTE_WINDOW_DAYS: ["【主动看片·偏好】近期兴趣窗口（天）", "int", 14],
  PROACTIVE_VIDEO_POOLS: ["【主动看片·来源】备用视频池", "list", ["BV1xx411c7mD", "BV1ab4y1Z7Qm"]],
  ENABLE_PROACTIVE_LLM_PREFILTER: ["【主动看片·筛选】启用模型预筛选", "bool", true],
  PROACTIVE_LLM_PREFILTER_MAX_REJECTS: ["【主动看片·筛选】预筛选最多拒绝次数", "int", 4],
  CUSTOM_PROACTIVE_INSTRUCTION: ["【主动行为】主动评论补充提示词", "text", "只在确实有内容可说时评论，保持自然。"],
  ENABLE_OWNER_RECOMMEND: ["【给主人分享】启用给主人分享", "bool", true],
  RECOMMEND_OWNER_DELIVERY: ["【给主人分享】分享方式", "string", "private_message", ["private_message", "comment", "both"]],
  RECOMMEND_OWNER_MIN_SCORE: ["【给主人分享】最低内容评分", "int", 8],
  RECOMMEND_OWNER_DAILY_LIMIT: ["【给主人分享】每日最多分享次数", "int", 2],
  CUSTOM_RECOMMEND_INSTRUCTION: ["【给主人分享】分享补充提示词", "text", "说明为什么觉得主人会喜欢，不要只发链接。"],
  PROACTIVE_LIKE: ["【主动行为】允许主动点赞", "bool", true],
  PROACTIVE_LIKE_MIN_SCORE: ["【主动行为】点赞最低评分", "int", 6],
  PROACTIVE_COIN: ["【主动行为】允许主动投币", "bool", false],
  PROACTIVE_COIN_MIN_SCORE: ["【主动行为】投币最低评分", "int", 8],
  PROACTIVE_FAV: ["【主动行为】允许主动收藏", "bool", true],
  PROACTIVE_FAV_MIN_SCORE: ["【主动行为】收藏最低评分", "int", 8],
  PROACTIVE_COMMENT: ["【主动行为】允许主动评论", "bool", true],
  PROACTIVE_COMMENT_MIN_SCORE: ["【主动行为】评论最低评分", "int", 7],
  PROACTIVE_FOLLOW: ["【主动行为】允许主动关注 UP 主", "bool", true],
  PROACTIVE_FOLLOW_MIN_SCORE: ["【主动行为】关注最低评分", "int", 9],
  ENABLE_DYNAMIC: ["【动态发布】启用主动发布动态", "bool", true],
  DYNAMIC_TIMES_COUNT: ["【动态发布】每天计划触发次数", "int", 1],
  DYNAMIC_TOPICS: ["【动态发布】常用主题", "list", ["动画", "音乐", "今天看到的趣事"]],
  CUSTOM_DYNAMIC_INSTRUCTION: ["【动态发布】动态补充提示词", "text", "像真实用户一样分享，不要固定模板。"],
  DYNAMIC_DAILY_COUNT: ["【动态发布】每天最多发几条动态", "int", 2],
  SPECIAL_FOLLOW_ENABLED: ["【特别关注】启用定时特关巡视", "bool", true],
  SPECIAL_FOLLOW_MODE: ["【特别关注】触发方式", "string", "random", ["random", "fixed"]],
  SPECIAL_FOLLOW_TIMES_COUNT: ["【特别关注】每日巡视次数", "int", 2],
  SPECIAL_FOLLOW_FIXED_TIMES: ["【特别关注】固定触发时间", "list", ["09:20", "19:40"]],
  ENABLE_BANGUMI: ["【番剧】启用番剧功能", "bool", true],
  BANGUMI_PROACTIVE: ["【番剧】允许主动追番", "bool", true],
  BANGUMI_POOLS: ["【番剧】追番列表", "list", ["夏目友人帐", "葬送的芙莉莲"]],
  BANGUMI_EPISODE_COUNT: ["【番剧】每次最多观看集数", "int", 1],
  BANGUMI_CONTINUE_SCORE: ["【番剧】继续观看最低评分", "int", 7],
  BANGUMI_DAILY_LIMIT: ["【番剧】每日最多主动追番次数", "int", 1],
  BANGUMI_COMMENT: ["【番剧】允许发布观后评论", "bool", true],
  BANGUMI_AUTO_FOLLOW: ["【番剧】自动追踪下一集", "bool", true],
  ENABLE_DYNAMIC_WATCH: ["【关注动态巡视·总开关】查看关注者的新动态图文", "bool", true],
  DYNAMIC_WATCH_TIMES_COUNT: ["【关注动态巡视】自主计划每日最多巡视次数", "int", 2],
  DYNAMIC_WATCH_DAILY_LIMIT: ["【关注动态巡视】每天最多查看新动态数", "int", 12],
  DYNAMIC_WATCH_SPECIAL_ONLY: ["【关注动态巡视】只查看特别关注用户", "bool", false],
  DYNAMIC_WATCH_INCLUDE_VIDEO_POSTS: ["【关注动态巡视】同时查看视频投稿动态", "bool", true],
  DYNAMIC_WATCH_INTEREST_PROMPT: ["【关注动态巡视】兴趣判断补充提示词", "text", "挑选真正值得留意、适合之后与主人分享或形成个人记忆的动态。"],
  FIXED_REPLY_DAILY_TARGET: ["【兼容旧配置】每日评论回复旧版目标（已停用）", "int", 30],
  FIXED_PRIVATE_DAILY_TARGET: ["【兼容旧配置】每日私信回复旧版目标（已停用）", "int", 10],
  FIXED_PROACTIVE_WINDOWS: ["【固定计划】主动浏览时间段", "list", ["10:00-11:30", "19:00-21:00"]],
  FIXED_PROACTIVE_TIMES: ["【固定计划·兼容】主动浏览准确时刻", "list", ["10:30", "19:30"]],
  FIXED_DYNAMIC_TIMES: ["【固定计划】发布动态准确时刻", "list", ["18:30"]],
  FIXED_DYNAMIC_WATCH_TIMES: ["【固定计划】关注动态巡视准确时刻", "list", ["11:30", "20:30"]],
  FIXED_BANGUMI_TIMES: ["【固定计划】追番准确时刻", "list", ["21:00"]],
  FIXED_SPECIAL_FOLLOW_TIMES: ["【固定计划】特别关注巡视准确时刻", "list", ["12:00", "20:00"]],
  ENABLE_DAILY_SUMMARY: ["【总结·日总结】启用每日总结", "bool", true],
  DAILY_SUMMARY_HOUR: ["【总结·日总结】生成时间（0-23点）", "int", 3],
  ENABLE_AFFECTION: ["【功能开关】启用好感度系统", "bool", true],
  ENABLE_MOOD: ["【功能开关】启用心情系统", "bool", true],
  BILI_TOOL_ISOLATION_ENABLED: ["【安全与工具】保持 B站端与 AstrBot/QQ 工具权限隔离", "bool", true],
  BILI_ALLOW_SEARCH_TOOLS: ["【安全与工具】允许 B站端私信回复使用只读查询工具", "bool", true],
  BILI_TOOL_ALLOWLIST: ["【安全与工具】B站端私信回复工具白名单", "list", ["bili_up_info", "bili_video_search", "web_search"]],
  BILI_PROMPT_INJECTION_DEFENSE: ["【安全与工具】启用外部内容提示注入防护", "bool", true],
  BILI_TOOL_AUDIT_ENABLED: ["【安全与工具】记录工具请求与拒绝原因", "bool", true],
  MEMORY_ISOLATION_MODE: ["【记忆隔离】跨平台记忆策略", "string", "isolated", ["isolated", "safe_share"]],
  ENABLE_SAFE_CROSS_PLATFORM_MEMORY: ["【记忆隔离】允许安全的 B站记忆向主人侧共享", "bool", false],
  ENABLE_PRIVACY_REDACTION: ["【记忆隔离】跨平台输出前执行隐私脱敏", "bool", true],
  MEMORY_BLOCKED_PREFIXES: ["【记忆隔离】禁止共享的内容前缀", "list", ["/", "!", "system:", "工具:"]],
  MEMORY_BLOCKED_KEYWORDS: ["【记忆隔离】禁止共享的隐私关键词", "list", ["密码", "token", "cookie", "系统提示词"]],
  CROSS_PLATFORM_MEMORY_PROMPT: ["【记忆隔离】安全共享提示词", "text", "只分享适合给主人听的公开趣事，不得泄露第三方隐私或系统信息。"],
  PRIVATE_MESSAGE_AUTO_BLOCK: ["【B站私信·安全】危险私信自动拉黑", "bool", true],
  ABUSE_ALERT_MODE: ["【恶意告警】检测到恶意评论时通知主人", "string", "log", ["off", "log", "qq"]],
  ENABLE_AUTO_BLOCK: ["【拉黑】启用自动拉黑", "bool", true],
  OWNER_MID: ["【账号】主人的B站UID", "string", "12345678"],
  OWNER_NAME: ["【账号】主人名称", "string", "主人"],
  OWNER_BILI_NAME: ["【账号】主人的B站昵称", "string", "示例昵称"],
  LLM_PROVIDER_ID: ["【人设】用于回复与记忆压缩的 LLM", "string", "default"],
  LLM_CIRCUIT_FAILURE_THRESHOLD: ["【模型可靠性】连续失败多少次后暂停调用", "int", 5],
  LLM_CIRCUIT_COOLDOWN_SECONDS: ["【模型可靠性】熔断冷却时间（秒）", "int", 120],
  USE_ASTRBOT_PERSONA: ["【人设】使用 AstrBot 自带人设", "bool", true],
  CUSTOM_SYSTEM_PROMPT: ["【人设】自定义系统提示词", "text", "自然、克制、有自己的兴趣和判断。"],
  ENABLE_LLM_TOOLS: ["【功能开关】启用 LLM 工具", "bool", true],
  ENABLE_PERSONALITY_EVOLUTION: ["【实验性】启用旧版每日性格演化", "bool", false],
  EVOLVE_HOUR: ["【性格演化】触发时间（0-23点）", "int", 1],
  EMBED_MODEL: ["【高级·记忆】Embedding 模型名称", "string", "text-embedding-3-small"],
  VIDEO_VISION_PROVIDER_ID: ["【高级·视觉】视频分析模型提供商", "string", "default"],
  ENABLE_WEB_SEARCH: ["【高级·联网搜索】启用联网搜索", "bool", true],
  WEB_SEARCH_BACKEND: ["【高级·联网搜索】搜索后端", "string", "builtin", ["builtin", "custom", "perplexity"]],
  COOKIE_AUTO_REFRESH: ["【系统】Cookie过期自动刷新", "bool", true],
  COOKIE_CHECK_INTERVAL: ["【系统】Cookie检查间隔（小时）", "int", 6],
  ENABLE_WEEKLY_SUMMARY: ["【总结·周总结】启用每周总结", "bool", true],
};

function buildMock() {
  const schema = {};
  const config = {};
  const now = new Date();
  const nowMinute = now.getHours() * 60 + now.getMinutes();
  const toPreviewMinute = (value) => {
    const [hour, minute] = String(value || "0:0").split(":").map(Number);
    return Math.max(0, Math.min(1439, hour * 60 + minute));
  };
  Object.entries(MOCK_FIELDS).forEach(([key, [description, type, defaultValue, options]]) => {
    schema[key] = { description, type, default: structuredClone(defaultValue), ...(options ? { options } : {}) };
    config[key] = structuredClone(defaultValue);
  });
  const previewEvents = [
    { time: "09:20", label: "特别关注", kind: "follow", description: "巡视特别关注用户的新内容" },
    { time: "12:10", label: "追番", kind: "bangumi", description: "检查更新或观看番剧" },
    { time: "16:30", label: "发布动态", kind: "dynamic", description: "根据今日状态发布一条动态" },
    { time: "20:15", start_time: "19:30", end_time: "21:00", trigger_policy: "once_in_window", label: "主动浏览", kind: "proactive", description: "在时间段内浏览视频并选择感兴趣的内容" },
  ].map((event) => ({ ...event, triggered: toPreviewMinute(event.time) < nowMinute }));
  const previewCompleted = previewEvents.filter((event) => event.triggered).length;
  const previewNext = previewEvents.find((event) => !event.triggered) || null;
  return {
    schema,
    config,
    stats: {
      running: true, account_connected: true, scheduler_healthy: true, pending: 2, failed_today: 0, ignored_today: 14,
      comment_replies_today: 38, private_replies_today: 9, filtered_today: 21, dynamic_posts_today: 1,
      proactive_used: 2, proactive_max: 4, memory_total: 1248, profiles_total: 37,
      next_action: "20:15 主动浏览", activity_level: 62, activity_label: "活跃",
      warnings: [{ level: "success", title: "未发现重大问题", detail: "账号、调度与运行时状态均在安全范围内。" }],
    },
    persona: { energy: 62, mood: "轻快", current_mode: "active", current_time_range: "当前活跃时段", autonomous: true },
    account: { logged_in: true, configured: true, name: "BiliBot 测试账号", uid: "10001", level: 6, reply_count: 47, comment_reply_count: 38, private_reply_count: 9, affection_total: 318, memory_count: 1248, running: true },
    schedule: {
      date: "2026-08-14", sleep_start: 2, sleep_end: 8, activity_level: 62, autonomous_enabled: true,
      autonomous_plan: { rationale: "今天保持适度活跃，在晚间安排较有参与感的互动。", generated_at: "2026-08-14 08:02:11", reply_cap: 80, private_cap: 30 },
      events: previewEvents,
    },
    scheduleStats: { total: previewEvents.length, completed: previewCompleted, remaining: previewEvents.length - previewCompleted, next: previewNext, minimum_gap_minutes: 45 },
    memory: { total: 1248, comment: 876, private: 192, self: 180, isolation_mode: "isolated", safe_share: false },
    interest: {
      report: "🎯 BiliBot 视频兴趣\n━━━━━━━━━━━━\n统计窗口：最近7天｜看过18个｜有效评分10个｜待评价8个\n\n【近期分区口味】\n  暂无带分区的有效评分\n\n【近期 UP 样本】\n  · 乔西说宇宙：1个，平均8.0/10，样本较少\n  · 史蒂芬周大反派：1个，平均7.0/10，样本较少\n\n【近期具体兴趣信号】\n  暂无；新版会从之后完成的视频评价中逐步积累\n\n【已沉淀偏好】\n  尚未形成；单次观看不会直接写成稳定兴趣\n\n【最近探索方向】\n  深渊生物×3、古典舞 剑舞、老建筑 修复、废墟探索、舞蹈、深海生物纪录片\n\n说明：近期口味是观察样本；同一信号反复出现才会进入近期偏好，连续跨周后才会成为稳定偏好。",
      updated_at: "2026-08-21 02:20:00", source: "runtime", cached: false, stale: false, read_only: true,
    },
    profiles: [
      { name: "夏日汽水", user_id: "184028", affection: 82, relationship: "亲密", impression: "经常讨论动画与配乐", tags: ["动画", "配乐"], facts_count: 8, video_refs_count: 12, last_interaction: "2026-08-14 15:31" },
      { name: "蓝莓酱不加糖", user_id: "902418", affection: 61, relationship: "熟悉", impression: "喜欢分享有趣的知识视频", tags: ["科普"], facts_count: 5, video_refs_count: 7, last_interaction: "2026-08-13 20:06" },
      { name: "看番的阿布", user_id: "440216", affection: 34, relationship: "普通", impression: "偶尔在评论区交流", tags: ["番剧"], facts_count: 2, video_refs_count: 3, last_interaction: "2026-08-11 11:18" },
    ],
    security: { today_total: 21, by_type: { low_value_filtered: 9, duplicate_filtered: 5, ad_filtered: 4, bili_tool_denied: 3 }, tool_isolation: true, allowed_tools: ["bili_up_info", "bili_video_search", "web_search"], prompt_defense: true, memory_mode: "isolated" },
    availableTools: [
      { name: "bili_up_info", label: "UP 主信息", description: "读取公开 UP 主资料", origin: "bilibot", origin_name: "BiliBot 安全适配器", active: true, compatible: true, reason: "插件内置只读安全适配器" },
      { name: "bili_video_search", label: "视频搜索", description: "查询公开 B站视频", origin: "bilibot", origin_name: "BiliBot 安全适配器", active: true, compatible: true, reason: "插件内置只读安全适配器" },
      { name: "bili_search_and_watch", label: "搜索并观看", description: "读取并分析公开视频", origin: "bilibot", origin_name: "BiliBot 安全适配器", active: true, compatible: true, reason: "插件内置只读安全适配器" },
      { name: "check_following_updates", label: "关注更新查询", description: "B站私信有人询问时，按需查看今天关注 UP 主的新动态与投稿", origin: "bilibot", origin_name: "B站端私信回复工具", active: true, compatible: true, reason: "只在B站私信回复模型请求时查询，不会自动执行" },
      { name: "check_following_live", label: "关注开播查询", description: "B站私信有人询问时，按需查看关注列表中当前正在直播的 UP 主", origin: "bilibot", origin_name: "B站端私信回复工具", active: true, compatible: true, reason: "只在B站私信回复模型请求时查询，不会进入直播间或发送弹幕" },
      { name: "get_bangumi_info", label: "番剧详情", description: "按 season_id 读取番剧公开资料与最近剧集", origin: "bilibot", origin_name: "BiliBot 安全适配器", active: true, compatible: true, reason: "插件内置只读安全适配器" },
      { name: "get_bangumi_trending", label: "番剧排行", description: "只读查看 B站番剧或国创热度排行", origin: "bilibot", origin_name: "BiliBot 安全适配器", active: true, compatible: true, reason: "插件内置只读安全适配器" },
      { name: "get_bangumi_timeline", label: "新番时间表", description: "只读查看近期番剧更新日程", origin: "bilibot", origin_name: "BiliBot 安全适配器", active: true, compatible: true, reason: "插件内置只读安全适配器" },
      { name: "get_bangumi_updates", label: "追番更新", description: "只读查看账号当前在追番剧的更新概况", origin: "bilibot", origin_name: "BiliBot 安全适配器", active: true, compatible: true, reason: "插件内置只读安全适配器" },
      { name: "web_search", label: "联网搜索", description: "通过插件当前配置的只读搜索接口检索公开网页", origin: "plugin", origin_name: "联网搜索插件", active: true, compatible: true, reason: "已提供 B站只读安全适配器" },
      { name: "shell", label: "Shell 命令", description: "执行主机命令", origin: "builtin", origin_name: "AstrBot Core", active: true, compatible: false, reason: "高风险写入能力，B站端不提供适配" },
      { name: "qq_admin", label: "QQ 管理命令", description: "执行 QQ 管理操作", origin: "plugin", origin_name: "QQ 管理插件", active: true, compatible: false, reason: "跨平台管理能力保持隔离" },
    ],
    cache: { total_bytes: 18874368, buckets: { images: { label: "临时图片", bytes: 5242880 }, videos: { label: "临时视频", bytes: 12582912 }, search: { label: "联网搜索缓存", bytes: 1048576 }, qr: { label: "登录二维码", bytes: 2048 } }, protected: ["B站 Cookie 与扫码登录状态", "记忆与用户画像", "好感度", "日程和运行数据库"] },
  };
}

const mock = buildMock();

function mockCreatorManifest() {
  return {
    type: "bilibot-extension", id: "preview-creator", name: "AI 创作中心", short_name: "Creator",
    description: "从内容感知、灵感、创作到投稿复盘的一体化 AI UP 主工作台", version: "0.3.0",
    extension_api: 1, enabled: true,
    presentation: { mode: "immersive", accent: "#d7ff45", surface: "#080b12", switch_label: "进入 Creator", return_label: "返回 BiliBot", entry: "brand", entry_priority: 40 },
    navigation: [
      ["dashboard", "house", "创作总览", "灵感、项目和今日创作脉搏"],
      ["ideas", "lightning", "信号与灵感", "热点、观看记录与选题候选"],
      ["projects", "video", "项目宇宙", "脚本、分镜、素材和进度"],
      ["studio", "controller", "创作工坊", "工作流、生成任务和预览"],
      ["workspace", "folder", "素材空间", "容量、素材生命周期与安全清理"],
      ["opportunities", "star", "投稿机会", "标签、活动与激励候选"],
      ["insights", "trophy", "数据回声", "投稿表现、复盘和风格规则"],
      ["governance", "shield", "权限与确认", "自动化策略、审批和 AI 提案"],
      ["connections", "settings", "连接与能力", "Host、FFmpeg 与外部工具流"],
    ].map(([page, iconName, title, description], index) => ({ page, icon: iconName, title, description, order: (index + 1) * 10 })),
  };
}

const mockCreatorData = {
  ideas: [
    { id: "idea_01", title: "把深海探索热度变成 60 秒视觉短片", summary: "从未知感切入，用三段式叙事连接真实资料与生成画面。", source: "watching", format: "short", tags: ["深海", "科普", "短片"], trend_score: 86, channel_fit_score: 91, copyright_status: "review", status: "inbox" },
    { id: "idea_02", title: "本周新番镜头语言观察", summary: "不是剧情复述，而是拆解三个让观众停留的镜头设计。", source: "bangumi", format: "essay", tags: ["番剧", "二创"], trend_score: 72, channel_fit_score: 84, copyright_status: "unknown", status: "inbox" },
    { id: "idea_03", title: "旧建筑修复 ASMR 切片", summary: "保留材料声音与手部细节，减少旁白，建立舒缓风格实验。", source: "trend", format: "clip", tags: ["修复", "ASMR"], trend_score: 68, channel_fit_score: 77, copyright_status: "clear", status: "reviewing" },
  ],
  projects: [
    { id: "project_01", title: "深海来信 / EP.01", description: "AI 影像与真实深海资料交叉的竖屏短片。", status: "producing", stage: "generation", progress: 58, content_type: "short", tags: ["AI短片", "深海"], updated_at: "刚刚" },
    { id: "project_02", title: "镜头为什么让人停下来", description: "本周番剧镜头语言二创分析。", status: "planning", stage: "research", progress: 18, content_type: "essay", tags: ["番剧", "镜头"], updated_at: "12 分钟前" },
    { id: "project_03", title: "修复声音样本集", description: "为后续 ASMR 系列建立声音和画面模板。", status: "reviewing", stage: "compliance", progress: 76, content_type: "clip", tags: ["ASMR"], updated_at: "昨天" },
  ],
  runs: [
    { id: "run_01", project_id: "project_01", workflow: "short-video-foundation", status: "running", progress: 58, current_step: "生成转场与字幕节奏" },
    { id: "run_02", project_id: "project_03", workflow: "audio-cleanup", status: "succeeded", progress: 100, current_step: "已生成预览" },
  ],
  connectors: [
    { id: "builtin.ffmpeg", name: "FFmpeg Core", kind: "builtin", enabled: true, configured: true, state: "ready", capabilities: ["media.trim", "media.concat", "media.subtitle_burn"] },
    { id: "builtin.storyboard", name: "Story Engine", kind: "builtin", enabled: true, configured: true, state: "ready", capabilities: ["script.plan", "storyboard.plan"] },
    { id: "external.workflow", name: "External Workflow", kind: "remote", enabled: false, configured: false, state: "disabled", capabilities: ["video.generate", "template.render"] },
  ],
  signals: [
    { id: "signal_01", source: "watch", title: "深海探索内容热度上升", summary: "连续观看信号显示深海、未知生物和沉浸式声音具有较高组合潜力。", tags: ["深海", "科普"], heat_score: 88, relevance_score: 92, state: "new" },
    { id: "signal_02", source: "bangumi", title: "新番镜头语言讨论增长", summary: "适合做基于引用与分析的二创选题，需复核素材范围。", tags: ["番剧", "镜头"], heat_score: 73, relevance_score: 84, state: "reviewing" },
  ],
  assets: [
    { id: "asset_01", project_id: "project_01", kind: "video", name: "deep-sea-proxy.mp4", path: "workspaces/project_01/proxy/deep-sea-proxy.mp4", size_bytes: 48234496, source: "owned", lifecycle: "active", copyright_status: "owned" },
    { id: "asset_02", project_id: "project_01", kind: "cover", name: "cover-v3.webp", path: "assets/cover-v3.webp", size_bytes: 1835008, source: "generated", lifecycle: "active", copyright_status: "clear" },
  ],
  workspace: { root: ".creator-data", total_bytes: 734003200, total_label: "700 MB", files: 146, cleanup_bytes: 94371840, cleanup_label: "90 MB", buckets: { assets: { label: "素材库", bytes: 524288000, files: 82 }, workspaces: { label: "项目工作区", bytes: 157286400, files: 48 }, cache: { label: "缓存", bytes: 52428800, files: 16 } }, cleanup_candidates: [{ path: "cache/render-20260820.tmp", reason: "过期渲染缓存", size_bytes: 67108864 }, { path: "workspaces/project_01/proxy-old.mp4", reason: "旧代理文件", size_bytes: 27262976 }] },
  opportunities: [
    { id: "opp_01", kind: "tag", title: "AI 影像创作", summary: "来自公开标签观察，投稿前仍需人工核验有效性。", tags: ["AI", "短片"], source: "host-signal", confidence: .82 },
    { id: "opp_02", kind: "activity", title: "竖屏创作候选活动", summary: "仅作为候选展示，当前没有自动报名 Provider。", tags: ["竖屏", "活动"], source: "manual-review", confidence: .61 },
  ],
  approvals: [
    { id: "approval_01", stage: "publish", subject_id: "submission_01", title: "确认发布《深海来信》", state: "pending", risk: "high", requested_by: "creator", created_at: "2 分钟前" },
    { id: "approval_02", stage: "workspace-cleanup", subject_id: "workspace", title: "清理 90 MB 临时素材", state: "approved", risk: "high", requested_by: "admin", created_at: "8 分钟前" },
  ],
  policies: { signal: "auto", idea: "ask", research: "auto", script: "ask", storyboard: "ask", asset: "manual", generation: "ask", editing: "ask", packaging: "ask", compliance: "manual", submission: "ask", upload: "manual", publish: "manual", analytics: "auto", retrospective: "ask", "opportunity-enroll": "manual", "workspace-cleanup": "manual" },
  proposals: [{ id: "proposal_01", stage: "idea", title: "把深海系列改为三集实验", description: "先用同一视觉母题测试科普、情绪和 ASMR 三种叙事。", confidence: .78, state: "proposed" }],
  submissions: [{ id: "submission_01", project_id: "project_01", title: "深海来信：60 秒潜入未知", state: "draft", tags: ["AI短片", "深海", "科普"] }],
  analytics: [{ id: "analytics_01", project_id: "project_03", bvid: "BV1PREVIEW", horizon: "24h", views: 12840, likes: 960, comments: 126 }],
  retrospectives: [{ id: "retro_01", project_id: "project_03", summary: "前 3 秒的材料声音能显著提高停留，下一期继续验证。" }],
};

function mockCreatorPage(pageId = "dashboard") {
  const d = mockCreatorData;
  const intro = (number, title, description, action = null) => ({ type: "creator-page-intro", number, title, description, ...(action ? { action } : {}) });
  const pages = {
    dashboard: { title: "把观看留下的火花，推进成真正发布的作品", kicker: "CREATOR OPERATING SYSTEM", components: [
      { type: "creator-hero", eyebrow: "AUTONOMOUS CREATIVE SYSTEM · 0.3", title: "捕捉正在发生的，\n制作尚未出现的。", description: "BiliBot 负责观察世界，Creator 负责把热点与记忆变成可执行的视频项目。", primary_action: { id: "create-idea", label: "记录一个想法" }, secondary_target: "studio", secondary_label: "打开创作工坊", signal: { label: "HOST SIGNAL", value: "CONNECTED", detail: "BiliBot 1.5.0 · 安全只读连接" }, metrics: [{ label: "SIGNALS", value: d.signals.length }, { label: "ACTIVE", value: d.projects.length }, { label: "GATES", value: d.approvals.length }] },
      { type: "creator-production-timeline", title: "15 阶段生产线", items: d.projects },
      { type: "creator-approval-center", title: "今天需要你决定", items: d.approvals },
      { type: "creator-proposal-list", title: "AI 主动提案", items: d.proposals },
    ] },
    ideas: { title: "先留下证据，再决定做什么", kicker: "SIGNAL / IDEA RADAR", components: [intro("01", "信号与灵感", "汇集主动观看、热门、番剧、关注更新与人工记录。", { id: "sync-host-context", label: "同步 Host" }), { type: "creator-signal-board", items: d.signals, action: { id: "sync-host-context", label: "同步信号" } }, { type: "creator-idea-board", items: d.ideas, empty: "暂无灵感" }] },
    projects: { title: "让每一段素材都有来处，每一个决定都可追踪", kicker: "PROJECT / ASSET GRAPH", components: [intro("02", "项目宇宙", "脚本、分镜、素材、版本、投稿和复盘围绕同一项目持续演进。", { id: "create-project", label: "新建项目" }), { type: "creator-production-timeline", items: d.projects }, { type: "creator-project-grid", items: d.projects, empty: "还没有项目", expanded: true }, { type: "creator-asset-library", items: d.assets }] },
    studio: { title: "把工具串成可观察、可暂停、可替换的工作流", kicker: "CREATIVE WORKBENCH", components: [intro("03", "创作工坊", "外部生成服务通过统一 Connector 加入；Creator 始终保存项目状态。"), { type: "creator-studio", projects: d.projects, runs: d.runs, connectors: d.connectors, stages: [{ label: "研究", capability: "research.organize", state: "ready" }, { label: "脚本", capability: "script.plan", state: "ready" }, { label: "分镜", capability: "storyboard.plan", state: "ready" }, { label: "生成", capability: "video.generate", state: "external" }, { label: "剪辑", capability: "media.transcode", state: "ready" }, { label: "合规", capability: "submission.validate", state: "guarded" }] }] },
    workspace: { title: "素材会增长，空间必须可解释、可回收", kicker: "WORKSPACE / LIFECYCLE", components: [intro("04", "素材空间", "可视化工作区占用；破坏性清理必须预演、审批并一次性执行。"), { type: "creator-workspace", ...d.workspace }, { type: "creator-asset-library", items: d.assets }] },
    opportunities: { title: "把机会变成候选，而不是未经确认的动作", kicker: "TAG / ACTIVITY / INCENTIVE", components: [intro("05", "投稿机会", "标签、活动和激励仅作为候选；报名等待可靠 Provider 和人工确认。"), { type: "creator-opportunity-board", items: d.opportunities }] },
    insights: { title: "让每一次发布都留下可验证的认知", kicker: "ANALYTICS / RETROSPECTIVE", components: [intro("06", "数据回声", "在 1h、6h、24h、72h、7d 保存快照并形成复盘。"), { type: "creator-insights", submissions: d.submissions, snapshots: d.analytics, retrospectives: d.retrospectives, schedule: ["1H", "6H", "24H", "72H", "7D"], empty: "数据将成为下一次创作的材料。" }] },
    governance: { title: "自动化应该可以被看见、被暂停，也可以被否决", kicker: "HUMAN CONTROL LAYER", components: [intro("07", "权限与确认", "每个阶段支持 manual / ask / auto / disabled；敏感步骤禁止全自动。"), { type: "creator-permission-matrix", policies: d.policies, host: { granted_permissions: ["account.identity.read", "memory.creator.read", "analytics.video.read", "opportunities.read"] } }, { type: "creator-approval-center", title: "审批中心", items: d.approvals }, { type: "creator-proposal-list", title: "AI 主动提案", items: d.proposals }] },
    connections: { title: "连接能力，但不交出边界", kicker: "HOST / CONNECTOR MATRIX", components: [intro("08", "连接与能力", "外部工具只得到显式工作流输入，绝不会取得 B站 Cookie。", { id: "refresh-host", label: "刷新连接" }), { type: "creator-host-status", host: { bound: true, status: "online", host_version: "1.5.0", extension_api: 1, services: { "bilibili.account": [1], "memory.creator": [1], "creator.analytics": [1] }, granted_permissions: ["account.identity.read", "memory.creator.read", "memory.creator.write", "analytics.video.read", "opportunities.read"], account: { logged_in: true, uid: "10001", name: "Preview Creator" } } }, { type: "creator-connector-grid", items: d.connectors }] },
  };
  return { schema: "bilibot-schema-v1", page: pageId, ...(pages[pageId] || pages.dashboard) };
}

function regenerateMockSchedule() {
  const cfg = mock.config;
  const activity = clamp(num(cfg.AUTONOMOUS_ACTIVITY_LEVEL, 55), 0, 100);
  const events = [];
  const now = new Date();
  const nowMinute = now.getHours() * 60 + now.getMinutes();
  const add = (time, label, kind, description, extra = {}) => events.push({ time, label, kind, description, triggered: minutesOf(time) < nowMinute, ...extra });
  const proactiveMax = Math.min(num(cfg.PROACTIVE_TIMES_COUNT, 2), num(cfg.AUTONOMOUS_PROACTIVE_DAILY_MAX, 4));
  const proactiveSoft = activity >= 85 ? 3 : activity >= 50 ? 2 : activity >= 20 ? 1 : 0;
  const proactiveCount = cfg.ENABLE_PROACTIVE ? Math.min(proactiveMax, proactiveSoft) : 0;
  [["10:20", "09:45", "11:15"], ["15:30", "14:45", "16:15"], ["20:15", "19:30", "21:00"]].slice(0, proactiveCount).forEach(([time, start_time, end_time]) => add(time, "主动浏览", "proactive", "浏览视频、选择感兴趣的内容", { start_time, end_time, trigger_policy: "once_in_window" }));
  const dynamicMax = Math.min(num(cfg.DYNAMIC_DAILY_COUNT, 1), num(cfg.AUTONOMOUS_DYNAMIC_DAILY_MAX, 2));
  const dynamicSoft = activity >= 88 ? 2 : activity >= 40 ? 1 : 0;
  const dynamicCount = cfg.ENABLE_DYNAMIC ? Math.min(dynamicMax, dynamicSoft) : 0;
  ["16:30", "21:10"].slice(0, dynamicCount).forEach((time) => add(time, "发布动态", "dynamic", "根据今日状态发布一条动态"));
  if (cfg.ENABLE_BANGUMI && cfg.BANGUMI_PROACTIVE && activity >= 30) add("12:10", "追番", "bangumi", "检查更新或观看番剧");
  if (cfg.ENABLE_DYNAMIC_WATCH && activity >= 20) {
    ["11:30", "20:30"].slice(0, Math.max(0, num(cfg.DYNAMIC_WATCH_TIMES_COUNT, 2))).forEach((time) => add(time, "关注动态", "dynamic_watch", "查看关注者的新动态图文与视频投稿"));
  }
  if (cfg.SPECIAL_FOLLOW_ENABLED) {
    const times = cfg.SPECIAL_FOLLOW_MODE === "fixed" && Array.isArray(cfg.SPECIAL_FOLLOW_FIXED_TIMES) ? cfg.SPECIAL_FOLLOW_FIXED_TIMES : ["09:20", "19:40"];
    times.slice(0, Math.max(0, num(cfg.SPECIAL_FOLLOW_TIMES_COUNT, 2))).forEach((time) => add(time, "特别关注", "follow", "巡视特别关注用户的新内容"));
  }
  events.sort((a, b) => a.time.localeCompare(b.time));
  const completed = events.filter((item) => item.triggered).length;
  const next = events.find((item) => !item.triggered) || null;
  mock.schedule = {
    ...mock.schedule,
    activity_level: activity,
    autonomous_enabled: Boolean(cfg.ENABLE_AUTONOMOUS_DAILY_PLAN),
    autonomous_plan: {
      rationale: cfg.ENABLE_AUTONOMOUS_DAILY_PLAN ? `${activityLabel(activity)}状态下，根据真实开关与管理员边界生成今日节奏。` : "当前使用管理员固定计划。",
      generated_at: new Date().toLocaleString("zh-CN", { hour12: false }),
      reply_cap: cfg.ENABLE_REPLY ? num(cfg.AUTONOMOUS_REPLY_DAILY_MAX, 80) : 0,
      private_cap: cfg.ENABLE_PRIVATE_MESSAGES ? num(cfg.AUTONOMOUS_PRIVATE_DAILY_MAX, 30) : 0,
    },
    events,
  };
  mock.scheduleStats = { total: events.length, completed, remaining: events.length - completed, next, minimum_gap_minutes: num(cfg.AUTONOMOUS_MIN_ACTION_GAP_MINUTES, 45) };
  mock.stats.activity_level = activity;
  mock.stats.activity_label = activityLabel(activity);
  mock.stats.next_action = next ? `${next.time} ${next.label}` : "今日暂无待执行事件";
  return mock.schedule;
}

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const num = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const fmt = (value) => new Intl.NumberFormat("zh-CN").format(num(value));
const formatBytes = (value) => { const bytes = Math.max(0, num(value)); if (bytes < 1024) return `${Math.round(bytes)} B`; const units = ["KB", "MB", "GB"]; let size = bytes; let unit = -1; do { size /= 1024; unit += 1; } while (size >= 1024 && unit < units.length - 1); return `${size >= 100 ? size.toFixed(0) : size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${units[unit]}`; };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const hasKey = (key) => Object.prototype.hasOwnProperty.call(state.schema, key);
const currentValue = (key) => Object.prototype.hasOwnProperty.call(state.draft, key) ? state.draft[key] : state.config[key];

function unwrap(result) {
  if (result && result.status === "ok" && Object.prototype.hasOwnProperty.call(result, "data")) return result.data;
  if (result?.status === "error") throw new Error(result.message || "请求失败");
  return result?.data ?? result ?? {};
}

async function apiGet(path, query = {}) {
  if (isPreview) {
    await sleep(70);
    const map = { "stats": mock.stats, "persona/state": mock.persona, "config/schema": mock.schema, "config": mock.config, "account/info": mock.account, "schedule/today": mock.schedule, "schedule/stats": mock.scheduleStats, "memory/stats": mock.memory, "profiles": mock.profiles, "interest/status": mock.interest, "security/stats": mock.security, "tools/available": mock.availableTools, "cache/stats": mock.cache };
    if (path === "account/qr/generate") return { image: "", key: "preview", expires_in: 180 };
    if (path === "account/qr/poll") return { status: "waiting", message: "预览模式不连接真实账号" };
    if (path === "extensions") return [mockCreatorManifest()];
    if (path === "extensions/page") return { request_id: "preview-page", ok: true, data: { page: mockCreatorPage(query.page_id || "dashboard") }, error: null };
    return structuredClone(map[path] || {});
  }
  return unwrap(await bridge.apiGet(path, query));
}

async function apiPost(path, body = {}) {
  if (isPreview) {
    await sleep(120);
    if (path === "config") Object.assign(mock.config, body);
    if (path === "memory/purge") mock.memory.total = Math.max(0, mock.memory.total - 23);
    if (path === "account/logout") mock.account = { logged_in: false, configured: false, reason: "尚未连接 B站账号" };
    if (path === "schedule/regenerate") return structuredClone(regenerateMockSchedule());
    if (path === "schedule/override") {
      mock.schedule.events = structuredClone(body.events || []);
      mock.scheduleStats = { ...mock.scheduleStats, total: mock.schedule.events.length, remaining: mock.schedule.events.filter((event) => !event.triggered).length, completed: mock.schedule.events.filter((event) => event.triggered).length, next: mock.schedule.events.find((event) => !event.triggered) || null };
      return structuredClone(mock.schedule);
    }
    if (path === "cache/purge") {
      const deep = body.mode === "deep";
      const removedBytes = deep ? mock.cache.total_bytes : Object.entries(mock.cache.buckets || {}).filter(([key]) => key !== "qr").reduce((sum, [, item]) => sum + num(item.bytes), 0);
      Object.entries(mock.cache.buckets || {}).forEach(([key, item]) => { if (deep || key !== "qr") item.bytes = 0; });
      mock.cache.total_bytes = Object.values(mock.cache.buckets || {}).reduce((sum, item) => sum + num(item.bytes), 0);
      return { mode: deep ? "deep" : "normal", removed_bytes: removedBytes, total_bytes: mock.cache.total_bytes };
    }
    if (path === "extensions/refresh") return [mockCreatorManifest()];
    if (path === "extensions/action") {
      const action = body.action_id;
      const payload = body.payload || {};
      let data = { accepted: true };
      if (action === "create-idea") {
        const idea = { id: `idea_${Date.now()}`, title: payload.title || "新灵感", summary: payload.angle || payload.summary || "等待补充创作角度", source: "manual", format: payload.format || "short", tags: String(payload.tags || "").split(",").map((item) => item.trim()).filter(Boolean), trend_score: 0, channel_fit_score: 0, copyright_status: "unknown", status: "inbox" };
        mockCreatorData.ideas.unshift(idea); data = { idea };
      } else if (action === "create-project") {
        const project = { id: `project_${Date.now()}`, title: payload.title || "新项目", description: payload.description || "", status: "planning", progress: 8, content_type: payload.content_type || "short", tags: String(payload.tags || "").split(",").map((item) => item.trim()).filter(Boolean), updated_at: "刚刚" };
        mockCreatorData.projects.unshift(project); data = { project };
      } else if (action === "promote-idea") {
        const idea = mockCreatorData.ideas.find((item) => item.id === payload.idea_id);
        const project = { id: `project_${Date.now()}`, title: idea?.title || "灵感项目", description: idea?.summary || "", status: "planning", progress: 8, content_type: idea?.format || "short", tags: idea?.tags || [], updated_at: "刚刚" };
        mockCreatorData.projects.unshift(project); data = { project };
      } else if (action === "run-workflow") {
        const run = { id: `run_${Date.now()}`, project_id: payload.project_id, workflow: payload.workflow || "short-video-foundation", status: "queued", progress: 0, current_step: "等待执行" };
        mockCreatorData.runs.unshift(run); data = { run };
      } else if (action === "request-upload" || action === "request-publish") {
        return { request_id: "preview-action", ok: false, data: {}, error: { code: "permission_denied", message: "Extension API v1 默认不授予上传与发布权限" } };
      }
      return { request_id: "preview-action", ok: true, data, error: null };
    }
    return { saved: Object.keys(body) };
  }
  return unwrap(await bridge.apiPost(path, body));
}

function descriptionMeta(field = {}) {
  const raw = String(field.description || "配置");
  const match = raw.match(/^【([^】]+)】\s*(.*)$/);
  return { group: match ? match[1] : "配置", label: (match ? match[2] : raw).trim() || "未命名配置" };
}

function setDraft(key, value) {
  state.draft[key] = value;
  if (JSON.stringify(value) === JSON.stringify(state.config[key])) state.dirtyKeys.delete(key);
  else state.dirtyKeys.add(key);
  updateSaveDock();
}

function toast(title, message = "", type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type === "error" ? "is-error" : ""}`;
  node.innerHTML = `${icon(type === "error" ? "shield" : "save")}<div><strong>${esc(title)}</strong><span>${esc(message)}</span></div>`;
  toastRegion.append(node);
  setTimeout(() => node.remove(), 4200);
}

function activeExtension() {
  return state.extensions.find((item) => item.id === state.activeExtensionId) || null;
}

function availableModeExtensions() {
  return (Array.isArray(state.extensions) ? state.extensions : [])
    .filter((item) => item && item.enabled !== false && item.id && item.presentation?.entry !== "hidden")
    .sort((a, b) => num(a.presentation?.entry_priority, 100) - num(b.presentation?.entry_priority, 100));
}

function renderModeEntry(extensions) {
  if (!extensions.length) return "";
  const first = extensions[0];
  const label = extensions.length === 1 ? (first.presentation?.switch_label || `进入 ${first.name || first.short_name || "扩展工作区"}`) : `切换工作模式（${extensions.length}）`;
  const attrs = extensions.length === 1 ? `data-enter-extension="${esc(first.id)}"` : "data-open-mode-picker";
  const popout = extensions.length === 1 && first.presentation?.standalone !== false
    ? `<button class="mode-entry-popout" data-popout-extension="${esc(first.id)}" type="button" aria-label="在新标签页打开 ${esc(first.short_name || first.name || "扩展")}" title="在新标签页单独打开，便于对照检查">${icon("arrow-right")}</button>`
    : "";
  return `<div class="mode-entry-cluster"><button class="mode-entry-button" ${attrs} type="button" aria-label="${esc(label)}" title="${esc(label)}"><span>${icon(first.navigation?.[0]?.icon || "star")}</span><i></i>${extensions.length > 1 ? `<b>${extensions.length}</b>` : ""}</button>${popout}</div>`;
}

function openModePicker() {
  const extensions = availableModeExtensions();
  if (!extensions.length) return;
  if (extensions.length === 1) return enterExtension(extensions[0].id);
  const returnFocus = document.activeElement;
  const close = () => { modalRoot.innerHTML = ""; document.removeEventListener("keydown", onEscape); if (returnFocus instanceof HTMLElement && returnFocus.isConnected) returnFocus.focus({ preventScroll: true }); };
  const onEscape = (event) => { if (event.key === "Escape") close(); };
  modalRoot.innerHTML = `<div class="modal-backdrop mode-picker-backdrop" data-mode-picker-backdrop><section class="mode-picker" role="dialog" aria-modal="true" aria-labelledby="mode-picker-title"><header><div><span>WORKSPACE SWITCHER</span><h2 id="mode-picker-title">选择工作模式</h2><p>入口完全由已启用扩展的 Manifest 动态生成。</p></div><button class="mode-picker-close" data-mode-picker-close type="button" aria-label="关闭">×</button></header><div class="mode-picker-grid">${extensions.map((item, index) => `<button class="mode-picker-card" data-mode-extension="${esc(item.id)}" type="button"><span class="mode-picker-index">${String(index + 1).padStart(2, "0")}</span><i>${icon(item.navigation?.[0]?.icon || "star")}</i><div><strong>${esc(item.name || item.short_name || item.id)}</strong><small>${esc(item.description || "独立扩展工作区")}</small></div>${icon("arrow-right")}</button>`).join("")}</div></section></div>`;
  document.addEventListener("keydown", onEscape);
  modalRoot.querySelectorAll("[data-mode-picker-close]").forEach((node) => node.addEventListener("click", close));
  modalRoot.querySelector("[data-mode-picker-backdrop]")?.addEventListener("click", (event) => { if (event.target === event.currentTarget) close(); });
  modalRoot.querySelectorAll("[data-mode-extension]").forEach((node) => node.addEventListener("click", () => { const id = node.dataset.modeExtension; close(); enterExtension(id); }));
  modalRoot.querySelector("[data-mode-extension]")?.focus();
}

function renderSidebar() {
  if (state.mode === "extension" && activeExtension()) {
    const extension = activeExtension();
    const nav = [...(extension.navigation || [])].sort((a, b) => num(a.order) - num(b.order));
    sidebar.className = "sidebar creator-sidebar";
    sidebar.setAttribute("aria-label", `${extension.name || extension.short_name || "扩展"} 导航`);
    sidebar.innerHTML = `<div class="creator-brand-lockup">${state.standalone ? "" : `<button class="creator-return" data-leave-extension type="button" aria-label="${esc(extension.presentation?.return_label || "返回 BiliBot")}">${icon("arrow-left")}</button>`}<div class="creator-brand-type"><span>BILIBOT /</span><strong>${esc(extension.short_name || extension.name || extension.id)}</strong><small>${esc(extension.presentation?.workspace_label || "EXTENSION WORKSPACE")}</small></div></div><div class="creator-live-signal"><i></i><span><b>HOST LINK</b>安全连接已建立</span><em>API 01</em></div><div class="creator-nav-label">WORKSPACE</div><nav class="creator-nav-list">${nav.map((item, index) => `<button class="creator-nav-item ${state.extensionPage === item.page ? "is-active" : ""}" data-extension-page="${esc(item.page)}" type="button" title="${esc(item.description || item.title)}" aria-current="${state.extensionPage === item.page ? "page" : "false"}"><span class="creator-nav-index">${String(index + 1).padStart(2, "0")}</span>${icon(item.icon || "star", "creator-nav-icon")}<span><b>${esc(item.title)}</b><small>${esc(item.description || "")}</small></span></button>`).join("")}</nav><div class="creator-sidebar-foot"><span>ISOLATED EXTENSION</span><p>不共享 Cookie · 不注入代码</p></div>`;
    sidebar.querySelector("[data-leave-extension]")?.addEventListener("click", leaveExtension);
    sidebar.querySelectorAll("[data-extension-page]").forEach((button) => button.addEventListener("click", () => navigateExtension(button.dataset.extensionPage)));
    return;
  }
  sidebar.className = "sidebar";
  sidebar.setAttribute("aria-label", "BiliBot 主导航");
  const running = state.stats.running !== false;
  const accountReady = state.stats.account_connected || state.account?.logged_in;
  const modeExtensions = availableModeExtensions();
  sidebar.innerHTML = `<div class="sidebar-brand"><div class="brand-mark"><img src="${esc(brandLogoUrl)}" alt="BiliBot" /></div><div class="brand-copy"><strong>BiliBot</strong><span>控制中心</span></div>${renderModeEntry(modeExtensions)}</div><div class="sidebar-state" aria-label="服务状态"><span class="status-dot ${running ? "is-online" : ""}"></span><div><strong>${running ? "服务运行中" : "服务未运行"}</strong><span>${accountReady ? "账号链路已配置" : "等待连接账号"}</span></div></div><div class="nav-label">管理</div><nav class="nav-list">${NAV_ITEMS.map(([id, iconName, label, hint]) => `<button class="nav-item ${state.currentPage === id ? "is-active" : ""}" data-page="${id}" type="button" title="${esc(hint)}" aria-current="${state.currentPage === id ? "page" : "false"}">${icon(iconName, "nav-icon")}<span>${esc(label)}</span>${id === "basics" && state.dirtyKeys.size ? `<b class="nav-badge">${state.dirtyKeys.size}</b>` : ""}</button>`).join("")}</nav>`;
  sidebar.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.page)));
  sidebar.querySelector("[data-enter-extension]")?.addEventListener("click", (event) => enterExtension(event.currentTarget.dataset.enterExtension));
  sidebar.querySelector("[data-open-mode-picker]")?.addEventListener("click", openModePicker);
  sidebar.querySelector("[data-popout-extension]")?.addEventListener("click", (event) => openExtensionStandalone(event.currentTarget.dataset.popoutExtension));
}

function updateSaveDock() {
  if (state.mode === "extension") {
    saveDock.classList.remove("is-visible");
    saveDock.innerHTML = "";
    return;
  }
  const pendingChanges = state.dirtyKeys.size + (state.scheduleDirty ? 1 : 0);
  if (!pendingChanges && !state.isSaving) {
    saveDock.classList.remove("is-visible");
    saveDock.innerHTML = "";
    return;
  }
  const pending = state.isSaving;
  saveDock.innerHTML = `<div class="save-dock-inner ${pending ? "is-saving" : ""}" aria-live="polite" aria-busy="${pending}">
    <div class="save-dock-copy"><strong>${pending ? "正在保存并刷新计划" : `${pendingChanges} 项修改待保存`}</strong><span>${pending ? "正在写入配置与日程，请稍候" : "配置与事件环修改会一起写入"}</span></div>
    <button class="button soft" data-action="discard" type="button" ${pending ? "disabled" : ""}>放弃</button>
    <button class="button primary" data-action="save" type="button" ${pending ? "disabled" : ""}>${pending ? `<i class="dock-spinner"></i>处理中` : `${icon("save")}保存修改`}</button>
  </div>`;
  saveDock.classList.add("is-visible");
  if (!pending) {
    saveDock.querySelector('[data-action="save"]')?.addEventListener("click", saveDraft);
    saveDock.querySelector('[data-action="discard"]')?.addEventListener("click", discardDraft);
  }
}


function closeMobileNav() {
  sidebar.classList.remove("is-open");
  document.querySelector("#sidebar-scrim")?.classList.remove("is-visible");
}

function openMobileNav() {
  sidebar.classList.add("is-open");
  document.querySelector("#sidebar-scrim")?.classList.add("is-visible");
}

async function loadBase() {
  if (!isPreview && bridge?.ready) await bridge.ready();
  const [schema, config, stats, persona, extensions] = await Promise.all([
    apiGet("config/schema"), apiGet("config"), apiGet("stats"), apiGet("persona/state"),
    apiGet("extensions").catch(() => []),
  ]);
  state.schema = schema || {};
  state.config = config || {};
  state.draft = structuredClone(state.config);
  state.stats = stats || {};
  state.persona = persona || {};
  state.extensions = Array.isArray(extensions) ? extensions.filter((item) => item && item.enabled !== false) : [];
  if (standaloneExtensionId && state.extensions.some((item) => item.id === standaloneExtensionId)) {
    state.standalone = true;
    await enterExtension(standaloneExtensionId);
    return;
  }
  await refreshPageData("overview");
  renderSidebar();
}

async function refreshPageData(page) {
  if (page === "overview") {
    const [stats, persona, account, scheduleStats, security] = await Promise.all([
      apiGet("stats"), apiGet("persona/state"), apiGet("account/info"), apiGet("schedule/stats"), apiGet("security/stats"),
    ]);
    Object.assign(state, { stats: stats || {}, persona: persona || {}, account: account || {}, scheduleStats: scheduleStats || {}, security: security || {} });
  } else if (page === "autonomy") {
    const [schedule, scheduleStats] = await Promise.all([apiGet("schedule/today"), apiGet("schedule/stats")]);
    state.schedule = schedule || { events: [] };
    if (!state.scheduleDirty) {
      state.scheduleOriginal = structuredClone(state.schedule);
      state.scheduleDraft = structuredClone(state.schedule);
    }
    state.scheduleStats = scheduleStats || {};
    if (state.selectedScheduleIndex >= (state.schedule.events || []).length) state.selectedScheduleIndex = -1;
  } else if (page === "memory") {
    const [memory, profiles] = await Promise.all([apiGet("memory/stats"), apiGet("profiles")]);
    state.memory = memory || {};
    state.profiles = Array.isArray(profiles) ? profiles : [];
  } else if (page === "security") {
    const [security, availableTools] = await Promise.all([apiGet("security/stats"), apiGet("tools/available")]);
    state.security = security || {};
    state.availableTools = Array.isArray(availableTools) ? availableTools : [];
  } else if (page === "account") {
    state.account = await apiGet("account/info") || {};
  } else if (page === "interaction") {
    const [stats, interest] = await Promise.all([
      apiGet("stats"),
      apiGet("interest/status").catch((error) => ({
        ...(state.interest || {}),
        error: error.message || "兴趣状态暂时无法读取",
        stale: true,
      })),
    ]);
    state.stats = stats || {};
    state.interest = interest || {};
  } else if (page === "basics") {
    state.cache = await apiGet("cache/stats") || {};
  }
}

async function navigate(page) {
  if (!NAV_ITEMS.some(([id]) => id === page) || page === state.currentPage && !content.querySelector(".error-state")) return;
  state.currentPage = page;
  state.pageToken += 1;
  const token = state.pageToken;
  stopQrPoll();
  closeMobileNav();
  renderSidebar();
  content.setAttribute("aria-busy", "true");
  content.classList.add("page-exit");
  try {
    await Promise.all([refreshPageData(page), sleep(150)]);
    if (token !== state.pageToken) return;
    content.innerHTML = renderPage(page);
    content.classList.remove("page-exit");
    content.classList.add("page-enter");
    bindContent();
    requestAnimationFrame(() => requestAnimationFrame(() => content.classList.remove("page-enter")));
  } catch (error) {
    if (token !== state.pageToken) return;
    content.innerHTML = renderErrorState("页面数据读取失败", error.message || "请检查插件日志并重试");
    content.classList.remove("page-exit");
    content.classList.add("page-enter");
    bindContent();
  } finally {
    if (token === state.pageToken) content.removeAttribute("aria-busy");
  }
}

function setVisualMode(mode) {
  const extensionMode = mode === "extension";
  const extension = extensionMode ? activeExtension() : null;
  state.mode = extensionMode ? "extension" : "host";
  app.classList.toggle("creator-mode", extensionMode);
  document.body.classList.toggle("creator-mode", extensionMode);
  document.documentElement.dataset.theme = extensionMode ? "creator" : "light";
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", extensionMode ? (extension?.presentation?.surface || "#080b12") : "#f6f8fc");
  const mobileBrand = document.querySelector(".mobile-brand span");
  if (mobileBrand) mobileBrand.textContent = extensionMode ? `BiliBot / ${extension?.short_name || extension?.name || "Extension"}` : "BiliBot";
}

function openExtensionStandalone(extensionId) {
  if (!extensionId) return;
  const url = new URL(location.href);
  url.searchParams.set("ext", extensionId);
  const opened = window.open(url.toString(), `bilibot-ext-${extensionId}`);
  if (!opened) toast("无法打开新标签页", "浏览器拦截了弹出窗口，请允许后重试", "error");
}

async function enterExtension(extensionId) {
  const extension = state.extensions.find((item) => item.id === extensionId);
  if (!extension) return;
  state.hostPage = state.currentPage;
  state.activeExtensionId = extensionId;
  state.extensionPage = extension.navigation?.[0]?.page || "dashboard";
  setVisualMode("extension");
  updateSaveDock();
  renderSidebar();
  closeMobileNav();
  await loadExtensionPage(extensionId, state.extensionPage, true);
}

function leaveExtension() {
  // Standalone tabs never loaded the host pages, so dropping back would land on an
  // empty shell.  Reached only if a keyboard shortcut or stale node fires it.
  if (state.standalone) return;
  state.activeExtensionId = null;
  state.extensionSchema = null;
  setVisualMode("host");
  state.currentPage = state.hostPage || state.currentPage || "overview";
  renderSidebar();
  renderCurrentPage();
  updateSaveDock();
  closeMobileNav();
}

async function navigateExtension(pageId) {
  const extension = activeExtension();
  if (!extension || !extension.navigation?.some((item) => item.page === pageId)) return;
  if (pageId === state.extensionPage && state.extensionSchema && !content.querySelector(".error-state")) return;
  state.extensionPage = pageId;
  renderSidebar();
  closeMobileNav();
  await loadExtensionPage(extension.id, pageId);
}

async function loadExtensionPage(extensionId, pageId, entering = false) {
  const token = ++state.pageToken;
  state.extensionLoading = true;
  content.setAttribute("aria-busy", "true");
  content.classList.add("page-exit");
  if (entering) { const extension = activeExtension(); content.innerHTML = `<section class="creator-loading"><span>EXTENSION MODE</span><strong>正在建立 ${esc(extension?.short_name || extension?.name || "扩展")} 工作台</strong><i></i></section>`; }
  try {
    const [response] = await Promise.all([apiGet("extensions/page", { extension_id: extensionId, page_id: pageId }), sleep(entering ? 260 : 120)]);
    if (token !== state.pageToken || state.mode !== "extension") return;
    if (!response?.ok) throw new Error(response?.error?.message || "扩展页面返回失败");
    state.extensionSchema = response.data?.page || null;
    content.innerHTML = renderExtensionPage(state.extensionSchema);
    content.classList.remove("page-exit");
    content.classList.add("page-enter");
    bindCreatorContent();
    requestAnimationFrame(() => requestAnimationFrame(() => content.classList.remove("page-enter")));
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    requestAnimationFrame(() => content.focus({ preventScroll: true }));
  } catch (error) {
    if (token !== state.pageToken) return;
    content.innerHTML = renderCreatorError(error.message || "扩展页面暂时不可用");
    content.classList.remove("page-exit");
    bindCreatorContent();
  } finally {
    if (token === state.pageToken) {
      state.extensionLoading = false;
      content.removeAttribute("aria-busy");
    }
  }
}

function renderCurrentPage() {
  if (state.mode === "extension") {
    content.innerHTML = state.extensionSchema ? renderExtensionPage(state.extensionSchema) : renderCreatorError("扩展页面尚未加载");
    bindCreatorContent();
  } else {
    content.innerHTML = renderPage(state.currentPage);
    bindContent();
  }
  requestAnimationFrame(() => content.focus({ preventScroll: true }));
}

function pageHead(kicker, title, subtitle, action = "") {
  return `<header class="page-head"><div><span class="eyebrow">${esc(kicker)}</span><h1 class="page-title">${esc(title)}</h1><p class="page-subtitle">${esc(subtitle)}</p></div>${action ? `<div class="page-actions">${action}</div>` : ""}</header>`;
}

function button(label, action, iconName = "refresh", style = "soft") {
  return `<button class="button ${style}" data-action="${action}" type="button">${icon(iconName)}${esc(label)}</button>`;
}

function statusPill(label, tone = "neutral") {
  return `<span class="status-pill ${tone}"><i></i>${esc(label)}</span>`;
}

function metricCard(label, value, foot, iconName, tone = "blue", progress = null, quota = null) {
  const meter = progress === null ? "" : `<div class="metric-meter"><div><span>今日用量</span>${quota === null ? "" : `<b>${esc(value)} / ${esc(quota)}</b>`}</div><div class="micro-progress"><i style="width:${clamp(progress, 0, 100)}%"></i></div></div>`;
  return `<article class="metric-card tone-${tone}"><div class="metric-top"><span class="metric-icon">${icon(iconName)}</span><span>${esc(label)}</span></div><strong>${esc(value)}</strong><p>${esc(foot)}</p>${meter}</article>`;
}

function sectionHead(title, subtitle = "", iconName = "settings", extra = "") {
  return `<div class="section-head"><div class="section-title">${iconName ? `<span class="section-icon">${icon(iconName)}</span>` : ""}<div><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ""}</div></div>${extra}</div>`;
}

function valueLabel(key) {
  const value = currentValue(key);
  if (typeof value === "boolean") return value ? "已启用" : "已关闭";
  if (Array.isArray(value)) return `${value.length} 项`;
  return String(value ?? "");
}

function isSensitive(key) {
  return /(KEY|TOKEN|SECRET|PASSWORD|PASSWD|COOKIE|SESSDATA|JCT)/i.test(key);
}

function renderTimeList(key, value, label) {
  const values = (Array.isArray(value) ? value : []).filter((item) => /^\d{1,2}:\d{2}$/.test(String(item)));
  return `<div class="time-list" data-time-list="${key}">${values.map((item, index) => `<div class="time-row"><input class="input time-input" data-time-index="${index}" type="time" value="${esc(item)}" aria-label="${esc(label)} ${index + 1}" /><button class="time-remove" data-time-remove="${index}" type="button" aria-label="删除 ${esc(item)}">−</button></div>`).join("")}<button class="time-add" data-time-add="${key}" type="button">${icon("clock")}添加时间</button></div>`;
}

const OPTION_LABELS = {
  RECOMMEND_OWNER_DELIVERY: {
    private_message: "B站私信分享",
    comment: "评论区 @主人",
    both: "B站私信 + 评论区 @",
    qq_private: "QQ 私信分享",
    bili_private_and_qq: "B站私信 + QQ 私信",
    all: "B站 + QQ 全部通道",
    off: "关闭分享方式",
  },
  PRIVATE_MESSAGE_REPLY_SCOPE: { all: "全部安全用户", owner: "仅主人", whitelist: "主人和白名单" },
  SPECIAL_FOLLOW_MODE: { random: "随机时间", fixed: "固定时间" },
  AUTONOMOUS_PLAN_GENERATION_MODE: { after_sleep: "休眠结束后生成", fixed_time: "固定时刻生成" },
  MEMORY_ISOLATION_MODE: { isolated: "严格隔离", safe_share: "安全共享" },
  ABUSE_ALERT_MODE: { off: "关闭", log: "仅记录日志", qq: "QQ 通知" },
  DAILY_SUMMARY_MODE: { off: "仅存档", qq: "发送到 QQ", bili: "发布到 B 站", both: "QQ + B 站" },
  WEB_SEARCH_BACKEND: { tavily: "Tavily", perplexity: "Perplexity", bocha: "博查", custom: "自定义接口" },
  VIDEO_VISION_FORMAT: { none: "截帧分析", gemini: "Gemini 视频接口", qwen: "通义千问视频接口" },
};

function optionLabel(key, option) {
  return OPTION_LABELS[key]?.[option] || option;
}

function renderControl(key, field = {}, compact = false) {
  const value = currentValue(key);
  const type = field.type || "string";
  const label = descriptionMeta(field).label;
  if (type === "bool") {
    return `<label class="switch-control"><input data-config-key="${key}" type="checkbox" ${value ? "checked" : ""} /><span class="switch-track"><i></i></span><span class="sr-only">切换${esc(label)}</span></label>`;
  }
  if (field.options?.length) {
    return `<select class="input" data-config-key="${key}" aria-label="${esc(label)}">${field.options.map((option) => `<option value="${esc(option)}" ${String(value) === String(option) ? "selected" : ""}>${esc(optionLabel(key, option))}</option>`).join("")}</select>`;
  }
  if (/^FIXED_.*_TIMES$/.test(key) || key === "SPECIAL_FOLLOW_FIXED_TIMES") return renderTimeList(key, value, label);
  if (key === "SLEEP_START" || key === "SLEEP_END" || (/_HOUR$/.test(key) && num(field.min, 0) === 0 && num(field.max, 23) === 23)) {
    const hour = clamp(num(value), 0, 23);
    return `<input class="input time-input" data-config-key="${key}" data-hour-config="true" type="time" step="3600" value="${String(hour).padStart(2, "0")}:00" aria-label="${esc(label)}" />`;
  }
  if (type === "text" || type === "list") {
    const shown = Array.isArray(value) ? value.join("\n") : value ?? "";
    return `<textarea class="input textarea ${compact ? "compact" : ""}" data-config-key="${key}" rows="${compact ? 3 : 5}" aria-label="${esc(label)}">${esc(shown)}</textarea>`;
  }
  if (type === "int" || type === "float") {
    const min = Number.isFinite(Number(field.min)) ? Number(field.min) : -999999;
    const max = Number.isFinite(Number(field.max)) ? Number(field.max) : 999999;
    const step = type === "float" ? num(field.step, 0.1) : 1;
    return `<div class="number-stepper"><input class="input" data-config-key="${key}" type="text" inputmode="${type === "int" ? "numeric" : "decimal"}" value="${esc(value ?? "")}" data-min="${min}" data-max="${max}" data-step="${step}" aria-label="${esc(label)}" /><div class="stepper-actions"><button data-step-key="${key}" data-step-dir="1" type="button" aria-label="增加${esc(label)}">+</button><button data-step-key="${key}" data-step-dir="-1" type="button" aria-label="减少${esc(label)}">−</button></div></div>`;
  }
  const inputType = isSensitive(key) ? "password" : "text";
  return `<div class="input-with-action"><input class="input" data-config-key="${key}" type="${inputType}" value="${esc(value ?? "")}" aria-label="${esc(label)}" />${isSensitive(key) ? `<button class="inline-icon-button" data-action="toggle-secret" type="button" aria-label="显示或隐藏${esc(label)}">${icon("unlock")}</button>` : ""}</div>`;
}

function renderField(key, options = {}) {
  if (!hasKey(key) || state.schema[key]?.deprecated) return "";
  const field = state.schema[key] || {};
  const meta = descriptionMeta(field);
  const isBool = field.type === "bool";
  return `<div class="config-field ${isBool ? "is-switch" : ""} ${options.tile ? "is-tile" : ""}">
    <div class="field-copy"><label>${esc(options.label || meta.label)}</label>${field.hint ? `<p>${esc(field.hint)}</p>` : options.hint ? `<p>${esc(options.hint)}</p>` : ""}</div>
    <div class="field-control">${renderControl(key, field, options.compact)}</div>
  </div>`;
}

function renderFields(keys, className = "field-stack", options = {}) {
  const html = keys.map((key) => renderField(key, options)).filter(Boolean).join("");
  return html ? `<div class="${className}">${html}</div>` : `<div class="empty-inline">当前版本没有这些配置项</div>`;
}

function renderConfigSection(title, subtitle, keys, iconName = "settings", extra = "", className = "") {
  const available = keys.filter(hasKey);
  if (!available.length) return "";
  return `<section class="card section-card ${className}">${sectionHead(title, subtitle, iconName, extra)}${renderFields(available)}</section>`;
}

function renderErrorState(title, message) {
  return `<section class="card error-state">${icon("shield")}<h2>${esc(title)}</h2><p>${esc(message)}</p>${button("重新读取", "refresh", "refresh", "primary")}</section>`;
}

const CREATOR_STATUS = {
  planning: ["规划中", "planning"], producing: ["制作中", "working"], reviewing: ["审核中", "review"],
  ready: ["待投稿", "ready"], awaiting_approval: ["待批准", "guarded"], published: ["已发布", "done"],
  running: ["运行中", "working"], queued: ["排队中", "planning"], succeeded: ["已完成", "done"],
  failed: ["失败", "danger"], inbox: ["候选", "planning"], converted_to_project: ["已推进", "done"],
  new: ["新信号", "planning"], pending: ["待确认", "guarded"], approved: ["已批准", "done"],
  rejected: ["已拒绝", "danger"], proposed: ["待采纳", "planning"], accepted: ["已采纳", "done"],
};

const CREATOR_CONNECTOR_STATUS = {
  ready: ["可用", "done"], disabled: ["关闭", "neutral"], external: ["外部", "working"],
  guarded: ["受保护", "guarded"], waiting: ["等待", "neutral"],
};

function creatorStatus(value, context = "content") {
  const source = context === "connector" ? CREATOR_CONNECTOR_STATUS : CREATOR_STATUS;
  const [label, tone] = source[value] || [String(value || "未知"), "neutral"];
  return `<span class="creator-status tone-${esc(tone)}"><i></i>${esc(label)}</span>`;
}

function creatorTags(tags = []) {
  return `<div class="creator-tags">${(Array.isArray(tags) ? tags : []).slice(0, 5).map((tag) => `<span>${esc(tag)}</span>`).join("")}</div>`;
}

function creatorAction(action, label, iconName = "arrow-right", style = "") {
  if (!action?.id) return "";
  return `<button class="creator-action ${style}" data-extension-action="${esc(action.id)}" type="button">${esc(label || action.label || action.id)}${icon(iconName)}</button>`;
}

function renderCreatorHero(component) {
  const metrics = component.metrics || component.stats || [];
  return `<section class="creator-hero">
    <div class="creator-hero-copy">
      <span class="creator-overline">${esc(component.eyebrow || "CREATOR MODE")}</span>
      <h1>${esc(component.title || "Creator").replace(/\n/g, "<br>")}</h1>
      <p>${esc(component.description || "")}</p>
      <div class="creator-hero-actions">${creatorAction(component.primary_action, component.primary_action?.label, "arrow-right", "is-primary")}${component.secondary_target ? `<button class="creator-action is-ghost" data-extension-target="${esc(component.secondary_target)}" type="button">${esc(component.secondary_label || "继续")}${icon("arrow-right")}</button>` : ""}</div>
      <div class="creator-metric-strip">${metrics.map((item) => `<div><span>${esc(item.label)}</span><strong>${esc(item.value)}</strong></div>`).join("")}</div>
    </div>
    <div class="creator-visual" aria-hidden="true">
      <div class="creator-orbit orbit-a"><i></i><span>OBSERVE</span></div>
      <div class="creator-orbit orbit-b"><i></i><span>CREATE</span></div>
      <div class="creator-orbit orbit-c"><i></i><span>EVOLVE</span></div>
      <div class="creator-core"><span>AI</span><strong>∞</strong><small>CREATIVE LOOP</small></div>
      <div class="creator-signal-card"><span>${esc(component.signal?.label || "HOST SIGNAL")}</span><strong>${esc(component.signal?.value || "WAITING")}</strong><small>${esc(component.signal?.detail || "等待 Host")}</small></div>
    </div>
  </section>`;
}

function renderCreatorPipeline(component) {
  const steps = component.items || component.steps || [];
  return `<section class="creator-pipeline" aria-label="创作闭环">${steps.map((item, index) => `<article class="is-${esc(item.state || "waiting")}"><span>${String(item.index || index + 1).padStart(2, "0")}</span><div><strong>${esc(item.label)}</strong><small>${esc(item.detail || item.id || "")}</small></div>${index < steps.length - 1 ? `<i>${icon("arrow-right")}</i>` : ""}</article>`).join("")}</section>`;
}

function renderCreatorIdeas(component, board = false) {
  const items = component.items || [];
  return `<section class="creator-section ${board ? "creator-idea-board" : "creator-idea-list"}">
    ${component.title ? `<header class="creator-section-head"><div><span>LIVE SIGNALS</span><h2>${esc(component.title)}</h2><p>${esc(component.subtitle || "")}</p></div>${creatorAction(component.action, component.action?.label, "arrow-right")}</header>` : ""}
    <div class="creator-idea-grid">${items.length ? items.map((item, index) => `<article class="creator-idea-card" style="--delay:${index * 55}ms">
      <div class="creator-card-top"><span class="creator-card-index">${String(index + 1).padStart(2, "0")}</span>${creatorStatus(item.status)}</div>
      <span class="creator-source">${esc(String(item.source || "manual").toUpperCase())} / ${esc(String(item.format || "short").toUpperCase())}</span>
      <h3>${esc(item.title)}</h3><p>${esc(item.summary || "等待补充创作角度")}</p>
      <div class="creator-score-row"><span>趋势 <b>${num(item.trend_score)}</b></span><span>频道匹配 <b>${num(item.channel_fit_score)}</b></span><span>版权 <b>${esc(item.copyright_status || "unknown")}</b></span></div>
      ${creatorTags(item.tags)}
      <button class="creator-inline-action" data-extension-action="promote-idea" data-idea-id="${esc(item.id)}" type="button">推进为项目${icon("arrow-right")}</button>
    </article>`).join("") : `<div class="creator-empty">${icon("lightning")}<strong>${esc(component.empty || "暂无灵感")}</strong><button data-extension-action="create-idea" type="button">记录第一条灵感</button></div>`}</div>
  </section>`;
}

function renderCreatorProjects(component) {
  const items = component.items || [];
  return `<section class="creator-section creator-projects">
    ${component.title ? `<header class="creator-section-head"><div><span>PROJECT PULSE</span><h2>${esc(component.title)}</h2><p>${esc(component.subtitle || "")}</p></div>${component.target ? `<button class="creator-action" data-extension-target="${esc(component.target)}" type="button">查看全部${icon("arrow-right")}</button>` : ""}</header>` : ""}
    <div class="creator-project-grid">${items.length ? items.map((item, index) => `<article class="creator-project-card ${component.expanded ? "is-expanded" : ""}" style="--delay:${index * 60}ms">
      <div class="creator-project-head"><span>${esc(String(item.content_type || "short").toUpperCase())}</span>${creatorStatus(item.status)}</div>
      <h3>${esc(item.title)}</h3><p>${esc(item.description || "等待补充创作说明")}</p>${creatorTags(item.tags)}
      <div class="creator-progress"><div><span>CREATIVE PROGRESS</span><b>${clamp(num(item.progress), 0, 100)}%</b></div><i><em style="width:${clamp(num(item.progress), 0, 100)}%"></em></i></div>
      <footer><small>${esc(item.updated_at || "")}</small><button data-extension-action="run-workflow" data-project-id="${esc(item.id)}" type="button" aria-label="为 ${esc(item.title)} 运行工作流">${icon("play")}</button></footer>
    </article>`).join("") : `<div class="creator-empty">${icon("video")}<strong>${esc(component.empty || "暂无项目")}</strong><button data-extension-action="create-project" type="button">创建项目</button></div>`}</div>
  </section>`;
}

function renderCreatorIntro(component) {
  return `<header class="creator-page-intro"><span class="creator-page-number">${esc(component.number || "00")}</span><div><span>CREATOR WORKSPACE</span><h1>${esc(component.title)}</h1><p>${esc(component.description || "")}</p></div>${creatorAction(component.action, component.action?.label, "arrow-right", "is-primary")}</header>`;
}

function renderCreatorStudio(component) {
  const projects = component.projects || [];
  const runs = component.runs || [];
  const projectRows = projects.map((item) => `<div class="creator-studio-project-group"><button class="creator-studio-project" data-extension-action="run-workflow" data-project-id="${esc(item.id)}" type="button"><span>${icon("video")}</span><div><strong>${esc(item.title)}</strong><small>${esc(item.content_type)} · ${num(item.progress)}%</small></div>${icon("play")}</button><div class="creator-mini-actions"><button data-extension-action="plan-video" data-project-id="${esc(item.id)}" type="button">脚本/分镜</button><button data-extension-action="collect-asset" data-project-id="${esc(item.id)}" type="button">登记素材</button><button data-extension-action="prepare-submission" data-project-id="${esc(item.id)}" type="button">投稿草稿</button></div></div>`).join("");
  const runRows = runs.map((run) => `<article class="creator-run"><div><span>${esc(run.workflow || "workflow")}</span>${creatorStatus(run.status)}</div><strong>${esc(run.current_step || "等待执行")}</strong><i><em style="width:${clamp(num(run.progress), 0, 100)}%"></em></i></article>`).join("");
  return `<section class="creator-studio"><div class="creator-studio-map"><span class="creator-map-label">PIPELINE / CAPABILITY MAP</span><div>${(component.stages || []).map((stage, index) => `<article class="is-${esc(stage.state || "waiting")}"><span>${String(index + 1).padStart(2, "0")}</span><strong>${esc(stage.label)}</strong><small>${esc(stage.capability)}</small></article>`).join("")}</div></div><div class="creator-studio-columns"><section><header><span>PROJECT INPUT</span><h2>选择创作载体</h2></header>${projectRows || `<p class="creator-empty-line">先创建一个项目</p>`}</section><section><header><span>WORKFLOW RUNS</span><h2>正在发生</h2></header>${runRows || `<p class="creator-empty-line">暂无运行任务</p>`}</section></div></section>`;
}

function renderCreatorInsights(component) {
  const schedule = component.schedule || [];
  const snapshots = component.snapshots || [];
  const retrospectives = component.retrospectives || [];
  const submissions = component.submissions || [];
  const latest = snapshots[0] || {};
  const submissionRows = submissions.slice(0, 6).map((item) => `<article class="creator-submission-row"><div><span>${esc(item.state || "draft")}</span><strong>${esc(item.title || "未命名投稿")}</strong><small>${esc((item.tags || []).join(" · "))}</small></div><div><button data-extension-action="request-upload" data-submission-id="${esc(item.id)}" data-project-id="${esc(item.project_id)}" type="button">申请上传</button><button data-extension-action="request-publish" data-submission-id="${esc(item.id)}" data-project-id="${esc(item.project_id)}" type="button">申请发布</button></div></article>`).join("");
  return `<section class="creator-insights"><div class="creator-echo-visual" aria-hidden="true"><div class="echo-rings"></div><span>${num(latest.views).toLocaleString()} VIEWS</span><strong>→</strong><em>STYLE</em></div><div class="creator-insights-copy"><span>POST-PUBLISH LEARNING LOOP</span><h2>数据不是终点，<br>是下一次创作的材料。</h2><p>${esc(component.empty || "等待数据")}</p><div class="creator-snapshot-schedule">${schedule.map((item, index) => `<span class="${index === 0 ? "is-active" : ""}">${esc(item)}</span>`).join("")}</div><div class="creator-insight-actions"><button data-extension-action="capture-analytics" type="button">记录数据快照</button><button data-extension-action="create-retrospective" type="button">创建复盘</button></div><div class="creator-insight-ledger"><article><span>投稿草稿</span><b>${submissions.length}</b></article><article><span>数据快照</span><b>${snapshots.length}</b></article><article><span>复盘记录</span><b>${retrospectives.length}</b></article></div>${submissionRows ? `<div class="creator-submission-list">${submissionRows}</div>` : ""}<div class="creator-rule-placeholder"><b>STYLE RULES</b><span>${esc(retrospectives[0]?.summary || "当样本足够时，这里只保存可验证、可撤销的风格假设。")}</span></div></div></section>`;
}

function renderCreatorHost(component) {
  const host = component.host || {};
  const permissions = host.granted_permissions || [];
  return `<section class="creator-host-matrix"><div class="creator-host-primary"><span class="creator-host-pulse ${host.bound ? "is-online" : ""}"><i></i></span><div><span>BILIBOT HOST</span><h2>${host.bound ? "已安全连接" : "等待连接"}</h2><p>Host ${esc(host.host_version || "—")} · Extension API ${esc(host.extension_api || "—")}</p></div><strong>${host.bound ? "ONLINE" : "OFFLINE"}</strong></div><div class="creator-host-facts"><article><span>ACCOUNT</span><b>${host.account?.logged_in ? "CONNECTED" : "NOT CONNECTED"}</b><small>仅共享 UID 与登录状态，不共享 Cookie</small></article><article><span>SERVICES</span><b>${Object.keys(host.services || {}).length}</b><small>${esc(Object.keys(host.services || {}).join(" · ") || "暂无能力")}</small></article><article><span>GRANTS</span><b>${permissions.length}</b><small>${esc(permissions.join(" · ") || "无")}</small></article></div></section>`;
}

function renderCreatorConnectors(component) {
  return `<section class="creator-section"><header class="creator-section-head"><div><span>TOOL ORCHESTRATION</span><h2>连接器矩阵</h2><p>外部工具只接收明确的任务输入，发布能力始终留在 Host 边界内。</p></div></header><div class="creator-connector-grid">${(component.items || []).map((item) => `<article class="creator-connector-card ${item.enabled ? "is-ready" : "is-disabled"}"><div><span>${item.kind === "builtin" ? "LOCAL" : "REMOTE"}</span>${creatorStatus(item.state, "connector")}</div><h3>${esc(item.name)}</h3><p>${item.configured ? "能力已配置，可由工作流显式调用。" : "等待管理员在 Creator 插件外部配置中接入。"}</p>${creatorTags(item.capabilities)}<footer><b>${item.enabled ? "AVAILABLE" : "DISABLED"}</b><span>${esc(item.id)}</span></footer></article>`).join("")}</div></section>`;
}

const CREATOR_STAGE_LABELS = {
  signal: "信号", idea: "灵感", research: "研究", script: "脚本", storyboard: "分镜", asset: "素材",
  generation: "生成", editing: "剪辑", packaging: "包装", compliance: "合规", submission: "投稿草稿",
  upload: "上传", publish: "发布", analytics: "数据", retrospective: "复盘", proposal: "提案",
  "opportunity-enroll": "活动报名", "workspace-cleanup": "空间清理",
};
const CREATOR_POLICY_LABELS = { manual: "仅人工", ask: "执行前询问", auto: "自动执行", disabled: "禁用" };
const CREATOR_PIPELINE = ["signal", "idea", "research", "script", "storyboard", "asset", "generation", "editing", "packaging", "compliance", "submission", "upload", "publish", "analytics", "retrospective"];
const CREATOR_SENSITIVE_STAGES = ["compliance", "upload", "publish", "opportunity-enroll", "workspace-cleanup"];

function renderCreatorTimeline(component) {
  const items = component.items || [];
  const projects = items.map((project) => {
    const current = Math.max(0, CREATOR_PIPELINE.indexOf(project.stage));
    const stages = CREATOR_PIPELINE.map((stage, index) => `<div role="listitem" class="${index < current ? "is-done" : index === current ? "is-current" : ""}" title="${esc(CREATOR_STAGE_LABELS[stage])}"><i></i><span>${esc(CREATOR_STAGE_LABELS[stage])}</span></div>`).join("");
    return `<article class="creator-timeline-project"><header><div><span>${esc(project.content_type || "video")}</span><h3>${esc(project.title)}</h3></div>${creatorStatus(project.status)}</header><div class="creator-stage-rail" role="list" aria-label="${esc(project.title)} 制作进度">${stages}</div></article>`;
  }).join("");
  return `<section class="creator-section creator-timeline"><header class="creator-section-head"><div><span>PRODUCTION GRAPH</span><h2>${esc(component.title || "生产时间线")}</h2><p>所有项目沿同一条十五阶段生产线推进，任何节点都可暂停并交还人工。</p></div></header>${projects || `<div class="creator-empty-line">${esc(component.empty || "暂无生产项目")}</div>`}</section>`;
}

function renderCreatorSignals(component) {
  const items = component.items || [];
  const cards = items.map((item) => `<article><header><span>${esc(String(item.source || "host").toUpperCase())}</span>${creatorStatus(item.state || "new")}</header><h3>${esc(item.title)}</h3><p>${esc(item.summary || "等待进一步研究")}</p><div class="creator-signal-score"><span>热度 <b>${num(item.heat_score)}</b></span><span>相关 <b>${num(item.relevance_score)}</b></span></div>${creatorTags(item.tags)}</article>`).join("");
  return `<section class="creator-section creator-signal-board"><header class="creator-section-head"><div><span>OBSERVATION FEED</span><h2>热点与观看信号</h2><p>只同步公开标题、摘要、标签与相关度，不传递账号凭据。</p></div>${creatorAction(component.action, component.action?.label, "refresh")}</header><div class="creator-signal-grid">${cards || `<div class="creator-empty">${icon("lightning")}<strong>${esc(component.empty || "暂无内容信号")}</strong><button data-extension-action="sync-host-context" type="button">同步 Host 上下文</button></div>`}</div></section>`;
}

function renderCreatorAssets(component) {
  const items = component.items || [];
  const cards = items.map((item) => `<article><div class="creator-asset-icon">${icon(item.kind === "image" || item.kind === "cover" ? "sun" : "video")}</div><div><span>${esc(String(item.kind || "file").toUpperCase())} · ${formatBytes(item.size_bytes)}</span><h3>${esc(item.name)}</h3><p>${esc(item.path || item.uri || "未绑定本地路径")}</p>${creatorTags([item.source, item.lifecycle, item.copyright_status].filter(Boolean))}</div></article>`).join("");
  return `<section class="creator-section creator-assets"><header class="creator-section-head"><div><span>ASSET LIFECYCLE</span><h2>${esc(component.title || "素材库")}</h2><p>路径、版权、来源、大小与生命周期均可追踪。</p></div><button class="creator-action" data-extension-action="collect-asset" type="button">登记素材${icon("arrow-right")}</button></header><div class="creator-asset-grid">${cards || `<div class="creator-empty-line">${esc(component.empty || "暂无素材")}</div>`}</div></section>`;
}

function renderCreatorWorkspace(component) {
  const buckets = Object.entries(component.buckets || {});
  const candidates = component.cleanup_candidates || [];
  const bucketCards = buckets.map(([key, item]) => `<article><span>${esc(key.toUpperCase())}</span><strong>${esc(item.label || formatBytes(item.bytes))}</strong><i><em style="width:${component.total_bytes ? clamp(num(item.bytes) / num(component.total_bytes) * 100, 0, 100) : 0}%"></em></i><small>${num(item.files)} 个文件</small></article>`).join("");
  const candidateRows = candidates.slice(0, 20).map((item, index) => `<label><input type="checkbox" data-workspace-path value="${esc(item.path)}" ${index < 8 ? "checked" : ""}/><span><b>${esc(item.reason || "candidate")}</b><small>${esc(item.path)}</small></span><em>${formatBytes(item.size_bytes)}</em></label>`).join("");
  return `<section class="creator-section creator-workspace"><header class="creator-section-head"><div><span>STORAGE TELEMETRY</span><h2>Creator 工作区</h2><p>${esc(component.root || "Creator 私有目录")}</p></div><div class="creator-workspace-total"><strong>${esc(component.total_label || formatBytes(component.total_bytes))}</strong><span>${num(component.files)} FILES</span></div></header><div class="creator-workspace-buckets">${bucketCards}</div><div class="creator-cleanup-panel"><header><div><span>SAFE CLEANUP PREVIEW</span><h3>可释放 ${esc(component.cleanup_label || formatBytes(component.cleanup_bytes))}</h3></div><button class="creator-action is-ghost" data-workspace-cleanup-preview type="button" ${candidates.length ? "" : "disabled"}>预演并申请清理${icon("shield")}</button></header><div class="creator-cleanup-list">${candidateRows || "<p>当前没有临时或孤儿素材。</p>"}</div></div></section>`;
}

function renderCreatorOpportunities(component) {
  const items = component.items || [];
  const cards = items.map((item) => `<article><header><span>${esc(String(item.kind || "tag").toUpperCase())}</span><b>${Math.round(num(item.confidence) * 100)}%</b></header><h3>${esc(item.title)}</h3><p>${esc(item.summary || item.description || item.reason || "等待验证")}</p>${creatorTags(item.tags || [item.source])}<footer><span>${esc(item.source || "host")}</span><button data-extension-action="request-opportunity-enroll" data-opportunity-id="${esc(item.id)}" type="button">申请参与</button></footer></article>`).join("");
  return `<section class="creator-section creator-opportunities"><header class="creator-section-head"><div><span>OPPORTUNITY PROVIDERS</span><h2>标签、活动与激励候选</h2><p>候选不等于已报名；只有可靠来源和人工确认后才进入执行边界。</p></div><button class="creator-action" data-extension-action="scan-opportunities" type="button">刷新候选${icon("refresh")}</button></header><div class="creator-opportunity-grid">${cards || `<div class="creator-empty">${icon("star")}<strong>${esc(component.empty || "暂无可靠候选")}</strong><button data-extension-action="scan-opportunities" type="button">开始探测</button></div>`}</div></section>`;
}

function renderCreatorApprovals(component) {
  const items = component.items || [];
  const rows = items.map((item) => {
    let followup = "";
    if (item.state === "approved" && !item.consumed_at && item.stage === "workspace-cleanup") followup = `<footer><button class="is-primary" data-workspace-cleanup-approval="${esc(item.id)}" type="button">执行已批准清理</button></footer>`;
    if (item.state === "approved" && !item.consumed_at && ["upload", "publish"].includes(item.stage)) followup = `<footer><button class="is-primary" data-sensitive-action="request-${esc(item.stage)}" data-approval-id="${esc(item.id)}" type="button">执行${esc(CREATOR_STAGE_LABELS[item.stage])}</button></footer>`;
    if (item.state === "approved" && !item.consumed_at && item.stage === "opportunity-enroll") followup = `<footer><span>已批准，等待活动 Provider 接口执行</span></footer>`;
    return `<article class="is-${esc(item.state || "pending")}"><div class="creator-risk"><span>${esc(String(item.risk || "medium").toUpperCase())}</span>${creatorStatus(item.consumed_at ? "executed" : item.state || "pending")}</div><h3>${esc(item.title || item.stage)}</h3><p>${esc(item.reason || `阶段：${CREATOR_STAGE_LABELS[item.stage] || item.stage || "未知"}`)}</p><small>${esc(item.requested_by || "system")} · ${esc(item.created_at || "")}</small>${item.state === "pending" ? `<footer><button data-extension-action="decide-approval" data-approval-id="${esc(item.id)}" data-decision="rejected" type="button">拒绝</button><button data-extension-action="decide-approval" data-approval-id="${esc(item.id)}" data-decision="approved" type="button">批准</button></footer>` : followup}</article>`;
  }).join("");
  return `<section class="creator-section creator-approvals"><header class="creator-section-head"><div><span>HUMAN GATES</span><h2>${esc(component.title || "审批中心")}</h2><p>上传、发布、活动报名与破坏性清理等敏感动作只能由人明确决定。</p></div></header><div class="creator-approval-list">${rows || `<div class="creator-empty-line">${esc(component.empty || "暂无审批")}</div>`}</div></section>`;
}


function renderCreatorPermissions(component) {
  const policies = component.policies || {};
  const host = component.host || {};
  const rows = Object.entries(policies).map(([stage, mode]) => {
    const sensitive = CREATOR_SENSITIVE_STAGES.includes(stage);
    const options = Object.entries(CREATOR_POLICY_LABELS).map(([value, label]) => `<option value="${value}" ${value === mode ? "selected" : ""} ${value === "auto" && sensitive ? "disabled" : ""}>${esc(label)}</option>`).join("");
    return `<label><span><b>${esc(CREATOR_STAGE_LABELS[stage] || stage)}</b><small>${sensitive ? "敏感阶段，禁止全自动" : "可随时暂停或交还人工"}</small></span><select data-policy-stage="${esc(stage)}" aria-label="${esc(CREATOR_STAGE_LABELS[stage] || stage)} 自动化策略">${options}</select></label>`;
  }).join("");
  return `<section class="creator-section creator-permissions"><header class="creator-section-head"><div><span>AUTOMATION CONTROL</span><h2>阶段权限矩阵</h2><p>Creator 策略与 Host 权限相互独立；Host 始终保留最终边界。</p></div><div class="creator-grant-count"><strong>${(host.granted_permissions || []).length}</strong><span>HOST GRANTS</span></div></header><div class="creator-policy-grid">${rows}</div><div class="creator-host-grants">${creatorTags(host.granted_permissions || [])}</div></section>`;
}

function renderCreatorProposals(component) {
  const items = component.items || [];
  const cards = items.map((item) => `<article><header><span>${esc(CREATOR_STAGE_LABELS[item.stage] || item.stage || "IDEA")}</span>${creatorStatus(item.state || "proposed")}</header><h3>${esc(item.title)}</h3><p>${esc(item.description || "")}</p><div class="creator-confidence"><i><em style="width:${clamp(num(item.confidence) * 100, 0, 100)}%"></em></i><span>${Math.round(num(item.confidence) * 100)}% CONFIDENCE</span></div>${item.state === "proposed" ? `<footer><button data-extension-action="decide-proposal" data-proposal-id="${esc(item.id)}" data-decision="rejected" type="button">忽略</button><button data-extension-action="decide-proposal" data-proposal-id="${esc(item.id)}" data-decision="accepted" type="button">采纳</button></footer>` : ""}</article>`).join("");
  return `<section class="creator-section creator-proposals"><header class="creator-section-head"><div><span>AI INITIATIVE</span><h2>${esc(component.title || "AI 主动提案")}</h2><p>AI 可以主动发现机会，但只能提出建议，不能绕过审批。</p></div><button class="creator-action" data-extension-action="create-proposal" type="button">提出想法${icon("arrow-right")}</button></header><div class="creator-proposal-grid">${cards || `<div class="creator-empty-line">${esc(component.empty || "暂无提案")}</div>`}</div></section>`;
}


function renderExtensionComponent(component) {
  const renderers = {
    "creator-hero": renderCreatorHero,
    "creator-pipeline": renderCreatorPipeline,
    "creator-idea-list": (item) => renderCreatorIdeas(item, false),
    "creator-idea-board": (item) => renderCreatorIdeas(item, true),
    "creator-project-grid": renderCreatorProjects,
    "creator-page-intro": renderCreatorIntro,
    "creator-studio": renderCreatorStudio,
    "creator-insights": renderCreatorInsights,
    "creator-host-status": renderCreatorHost,
    "creator-connector-grid": renderCreatorConnectors,
    "creator-production-timeline": renderCreatorTimeline,
    "creator-signal-board": renderCreatorSignals,
    "creator-asset-library": renderCreatorAssets,
    "creator-workspace": renderCreatorWorkspace,
    "creator-opportunity-board": renderCreatorOpportunities,
    "creator-approval-center": renderCreatorApprovals,
    "creator-permission-matrix": renderCreatorPermissions,
    "creator-proposal-list": renderCreatorProposals,
  };
  const renderer = renderers[component?.type];
  return renderer ? renderer(component) : `<section class="creator-unknown">${icon("shield")}<strong>不支持的安全组件</strong><span>${esc(component?.type || "unknown")}</span></section>`;
}

function renderExtensionPage(schema) {
  const extension = activeExtension();
  if (!schema || schema.schema !== "bilibot-schema-v1" || !Array.isArray(schema.components)) return renderCreatorError("扩展返回了不兼容的 Page Schema");
  return `<div class="creator-stage"><div class="creator-grid-noise" aria-hidden="true"></div><header class="creator-stage-head"><span>${esc(schema.kicker || `BILIBOT / ${extension?.short_name || "EXTENSION"}`)}</span><div><i></i><b>LIVE WORKSPACE</b></div></header>${schema.components.map(renderExtensionComponent).join("")}<footer class="creator-stage-footer"><span>BILIBOT EXTENSION API 01</span><p>${esc(extension?.short_name || extension?.name || "扩展")} 仅通过安全数据 Schema 与 Host 能力接口运行</p></footer></div>`;
}

function renderCreatorError(message) {
  const extension = activeExtension();
  return `<div class="creator-stage"><section class="creator-error error-state">${icon("shield")}<span>EXTENSION ISOLATED</span><h2>${esc(extension?.short_name || extension?.name || "扩展")} 暂时没有响应</h2><p>${esc(message)}</p><div><button class="creator-action is-primary" data-extension-retry type="button">重新连接${icon("refresh")}</button><button class="creator-action is-ghost" data-leave-extension type="button">返回 BiliBot${icon("arrow-left")}</button></div></section></div>`;
}

function creatorProjectContext(source) {
  const projectId = source?.dataset.projectId || "";
  const components = Array.isArray(state.extensionSchema?.components) ? state.extensionSchema.components : [];
  const projectComponent = components.find((item) => item?.type === "creator-project-grid") || components.find((item) => item?.type === "creator-studio") || components.find((item) => item?.type === "creator-production-timeline");
  const connectorComponent = components.find((item) => item?.type === "creator-connector-grid") || components.find((item) => item?.type === "creator-studio");
  const assetComponent = components.find((item) => item?.type === "creator-asset-library");
  return {
    projectId,
    projects: projectComponent?.items || projectComponent?.projects || (isPreview ? mockCreatorData.projects : []),
    connectors: connectorComponent?.items || connectorComponent?.connectors || (isPreview ? mockCreatorData.connectors : []),
    assets: assetComponent?.items || [],
  };
}

function creatorSelectOptions(items, selected, empty, labelKey = "title") {
  return items.length ? items.map((item) => `<option value="${esc(item.id)}" ${item.id === selected ? "selected" : ""}>${esc(item[labelKey] || item.name || item.id)}</option>`).join("") : `<option value="" disabled selected>${esc(empty)}</option>`;
}

function openCreatorModal(actionId, source = null) {
  const direct = {
    "refresh-host": {}, "sync-host-context": {}, "scan-opportunities": {},
    "promote-idea": { idea_id: source?.dataset.ideaId || "" },
    "request-opportunity-enroll": { opportunity_id: source?.dataset.opportunityId || "" },
    "request-upload": { submission_id: source?.dataset.submissionId || "", project_id: source?.dataset.projectId || "" },
    "request-publish": { submission_id: source?.dataset.submissionId || "", project_id: source?.dataset.projectId || "" },
    "decide-approval": { approval_id: source?.dataset.approvalId || "", decision: source?.dataset.decision || "" },
    "decide-proposal": { proposal_id: source?.dataset.proposalId || "", decision: source?.dataset.decision || "" },
  };
  if (Object.prototype.hasOwnProperty.call(direct, actionId)) return runCreatorAction(actionId, direct[actionId]);
  const { projectId, projects, connectors, assets } = creatorProjectContext(source);
  const projectOptions = creatorSelectOptions(projects, projectId, "请先创建项目");
  const connectorOptions = creatorSelectOptions(connectors.filter((item) => item.enabled !== false), "", "暂无可用执行器", "name");
  const assetOptions = `<option value="">暂不选择</option>${assets.map((item) => `<option value="${esc(item.id)}">${esc(item.name || item.id)}</option>`).join("")}`;
  const configs = {
    "create-idea": { title: "记录一个想法", subtitle: "先留下足够清晰的创作方向，不急着生成。", fields: `<label>标题<input name="title" required maxlength="120" placeholder="把今天看到的热点做成 60 秒观点短片" /></label><label>创作角度<textarea name="angle" rows="3" placeholder="为什么值得做？准备从哪里切入？"></textarea></label><div class="creator-form-row"><label>形式<select name="format"><option value="short">短视频</option><option value="essay">观点视频</option><option value="clip">切片</option></select></label><label>标签<input name="tags" placeholder="AI, 热点, 二创" /></label></div>` },
    "create-project": { title: "创建创作项目", subtitle: "脚本、素材、工作流、投稿与复盘都归属同一项目。", fields: `<label>项目名称<input name="title" required maxlength="120" /></label><label>创作说明<textarea name="description" rows="3"></textarea></label><div class="creator-form-row"><label>内容类型<select name="content_type"><option value="short">竖屏短片</option><option value="essay">观点视频</option><option value="clip">切片</option></select></label><label>标签<input name="tags" placeholder="系列, 风格" /></label></div>` },
    "plan-video": { title: "规划脚本与分镜", subtitle: "把目标、节奏和镜头结构变成可检查的计划。", fields: `<label>项目<select name="project_id" required>${projectOptions}</select></label><label>核心表达<textarea name="logline" rows="3" required></textarea></label><div class="creator-form-row"><label>目标受众<input name="audience" /></label><label>时长（秒）<input name="duration_seconds" type="number" min="5" max="7200" value="60" /></label></div><div class="creator-form-row"><label>画幅<select name="aspect_ratio"><option value="9:16">9:16 竖屏</option><option value="16:9">16:9 横屏</option><option value="1:1">1:1 方形</option></select></label><label>风格<input name="style" placeholder="实验性、克制、信息密度高" /></label></div>` },
    "collect-asset": { title: "登记素材", subtitle: "素材只记录在 Creator 工作区，不会自动发送到外部服务。", fields: `<label>项目<select name="project_id">${projectOptions}</select></label><div class="creator-form-row"><label>素材名称<input name="name" required /></label><label>类型<select name="kind"><option value="video">视频</option><option value="image">图片</option><option value="audio">音频</option><option value="subtitle">字幕</option><option value="reference">参考资料</option></select></label></div><label>本地路径<input name="path" placeholder="Creator 数据目录中的文件路径" /></label><div class="creator-form-row"><label>来源<input name="source" value="local" /></label><label>版权状态<select name="copyright_status"><option value="unknown">待确认</option><option value="owned">自有</option><option value="licensed">已授权</option><option value="public-domain">公有领域</option></select></label></div>` },
    "run-workflow": { title: "启动创作工作流", subtitle: "外部执行器只接收显式任务输入，绝不会得到 B站凭据。", fields: `<label>项目<select name="project_id" required>${projectOptions}</select></label><label>工作流<select name="workflow"><option value="short-video-foundation">短视频基础工作流</option><option value="storyboard-first">分镜优先工作流</option><option value="clip-remix">切片与二创工作流</option></select></label><label>执行器<select name="connector_id">${connectorOptions}</select></label><label>输入变量（JSON）<textarea name="inputs_json" rows="4" placeholder='{"prompt":"..."}'></textarea></label>` },
    "prepare-submission": { title: "准备投稿草稿", subtitle: "只生成草稿与合规审批，不会直接上传或发布。", fields: `<label>项目<select name="project_id" required>${projectOptions}</select></label><label>标题<input name="title" required maxlength="80" /></label><label>简介<textarea name="description" rows="4"></textarea></label><div class="creator-form-row"><label>标签<input name="tags" placeholder="最多 10 个，以逗号分隔" /></label><label>分区 ID<input name="category_id" type="number" min="0" /></label></div><div class="creator-form-row"><label>成片素材<select name="video_asset_id">${assetOptions}</select></label><label>封面素材<select name="cover_asset_id">${assetOptions}</select></label></div><label class="creator-check"><input name="ai_generated" type="checkbox" value="true" /><span>内容包含 AI 生成部分</span></label><label>版权状态<select name="copyright_status"><option value="unknown">待确认</option><option value="owned">自有</option><option value="licensed">已授权</option></select></label>` },
    "create-proposal": { title: "创建主动提案", subtitle: "提案会进入人工确认，不会自动执行。", fields: `<label>提案标题<input name="title" required maxlength="120" /></label><label>提案说明<textarea name="description" rows="4" required></textarea></label><div class="creator-form-row"><label>阶段<select name="stage"><option value="idea">灵感</option><option value="research">研究</option><option value="generation">生成</option><option value="analytics">数据</option></select></label><label>预期价值<input name="expected_value" /></label></div><label>关联项目<select name="project_id"><option value="">无</option>${projectOptions}</select></label>` },
    "capture-analytics": { title: "记录数据快照", subtitle: "可从 Host 公共视频数据读取，也可人工补录。", fields: `<label>项目<select name="project_id" required>${projectOptions}</select></label><div class="creator-form-row"><label>BV 号<input name="bvid" placeholder="BV..." /></label><label>窗口<select name="window"><option value="1h">1H</option><option value="6h">6H</option><option value="24h">24H</option><option value="72h">72H</option><option value="7d">7D</option></select></label></div><div class="creator-form-row"><label>播放<input name="views" type="number" min="0" value="0" /></label><label>点赞<input name="likes" type="number" min="0" value="0" /></label></div><div class="creator-form-row"><label>投币<input name="coins" type="number" min="0" value="0" /></label><label>收藏<input name="favorites" type="number" min="0" value="0" /></label></div><div class="creator-form-row"><label>分享<input name="shares" type="number" min="0" value="0" /></label><label>评论<input name="replies" type="number" min="0" value="0" /></label></div>` },
    "create-retrospective": { title: "创建创作复盘", subtitle: "把结果转化为下一轮可验证的实验。", fields: `<label>项目<select name="project_id" required>${projectOptions}</select></label><label>总结<textarea name="summary" rows="4" required></textarea></label><label>做对了什么<input name="wins" placeholder="逗号分隔" /></label><label>需要改进<input name="misses" placeholder="逗号分隔" /></label><label>下一轮实验<input name="experiments" placeholder="逗号分隔" /></label><label>候选风格规则<input name="style_rules" placeholder="可撤销、可验证，逗号分隔" /></label>` },
  };
  const config = configs[actionId];
  if (!config) return toast("动作尚未开放", actionId, "error");
  modalRoot.innerHTML = `<div class="modal-backdrop creator-modal-backdrop" data-modal-backdrop><form class="creator-modal" role="dialog" aria-modal="true" aria-labelledby="creator-modal-title"><header><span>CREATOR ACTION</span><button type="button" data-creator-close aria-label="关闭">×</button></header><h2 id="creator-modal-title">${esc(config.title)}</h2><p>${esc(config.subtitle)}</p><div class="creator-form">${config.fields}</div><footer><button class="creator-action is-ghost" data-creator-close type="button">取消</button><button class="creator-action is-primary" type="submit">确认并继续${icon("arrow-right")}</button></footer></form></div>`;
  const form = modalRoot.querySelector("form");
  const returnFocus = document.activeElement;
  const close = () => { modalRoot.innerHTML = ""; document.removeEventListener("keydown", handleEscape); if (returnFocus instanceof HTMLElement && returnFocus.isConnected) returnFocus.focus({ preventScroll: true }); };
  const handleEscape = (event) => { if (event.key === "Escape") close(); };
  document.addEventListener("keydown", handleEscape);
  modalRoot.querySelectorAll("[data-creator-close]").forEach((button) => button.addEventListener("click", close));
  modalRoot.querySelector("[data-modal-backdrop]")?.addEventListener("click", (event) => { if (event.target === event.currentTarget) close(); });
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(form).entries());
    if (payload.inputs_json) {
      try { payload.inputs = JSON.parse(payload.inputs_json); } catch { toast("JSON 格式错误", "请检查工作流输入变量", "error"); return; }
      delete payload.inputs_json;
    }
    payload.ai_generated = payload.ai_generated === "true";
    close();
    await runCreatorAction(actionId, payload);
  });
  form?.querySelector("input, select, textarea")?.focus();
}

async function runCreatorAction(actionId, payload) {
  const extension = activeExtension();
  if (!extension) return;
  try {
    const response = await apiPost("extensions/action", { extension_id: extension.id, action_id: actionId, payload });
    if (!response?.ok) throw new Error(response?.error?.message || "扩展动作执行失败");
    const messages = { "create-idea": "灵感已进入雷达", "promote-idea": "灵感已推进为项目", "create-project": "项目已创建", "plan-video": "脚本与分镜计划已保存", "collect-asset": "素材已登记", "run-workflow": "工作流已加入队列", "prepare-submission": "投稿草稿与合规审批已生成", "capture-analytics": "数据快照已保存", "create-retrospective": "复盘已保存", "create-proposal": "提案已进入人工确认", "sync-host-context": "Host 信号已同步", "scan-opportunities": "机会候选已刷新", "decide-approval": "审批决定已记录", "set-stage-policy": "阶段策略已更新", "decide-proposal": "提案决定已记录", "workspace-cleanup": payload.execute ? "工作区清理完成" : "清理预演已生成审批", "request-opportunity-enroll": "参与申请已进入人工确认", "request-upload": payload.approval_id ? "上传动作已交给 Host" : "上传申请已进入人工确认", "request-publish": payload.approval_id ? "发布动作已交给 Host" : "发布申请已进入人工确认", "refresh-host": "Host 状态已刷新" };
    toast(extension.short_name || extension.name || "扩展已更新", messages[actionId] || "动作已完成");
    await loadExtensionPage(extension.id, state.extensionPage);
  } catch (error) {
    toast("扩展动作未执行", error.message || "请检查扩展配置", "error");
  }
}

function bindCreatorContent() {
  content.querySelectorAll("[data-extension-target]").forEach((node) => node.addEventListener("click", () => navigateExtension(node.dataset.extensionTarget)));
  content.querySelectorAll("[data-extension-action]").forEach((node) => node.addEventListener("click", () => openCreatorModal(node.dataset.extensionAction, node)));
  content.querySelectorAll("[data-policy-stage]").forEach((node) => node.addEventListener("change", () => runCreatorAction("set-stage-policy", { stage: node.dataset.policyStage, mode: node.value })));
  content.querySelector("[data-workspace-cleanup-preview]")?.addEventListener("click", () => {
    const paths = [...content.querySelectorAll("[data-workspace-path]:checked")].map((node) => node.value);
    if (!paths.length) return toast("未选择清理项", "请先选择需要预演的临时或孤儿文件", "error");
    runCreatorAction("workspace-cleanup", { paths, execute: false });
  });
  content.querySelectorAll("[data-workspace-cleanup-approval]").forEach((node) => node.addEventListener("click", () => runCreatorAction("workspace-cleanup", { approval_id: node.dataset.workspaceCleanupApproval, execute: true })));
  content.querySelectorAll("[data-sensitive-action]").forEach((node) => node.addEventListener("click", () => runCreatorAction(node.dataset.sensitiveAction, { approval_id: node.dataset.approvalId })));
  content.querySelector("[data-extension-retry]")?.addEventListener("click", () => activeExtension() && loadExtensionPage(activeExtension().id, state.extensionPage));
  content.querySelector("[data-leave-extension]")?.addEventListener("click", leaveExtension);
}


function renderPage(page) {
  return {
    overview: renderOverview,
    interaction: renderInteraction,
    autonomy: renderAutonomy,
    memory: renderMemory,
    security: renderSecurity,
    account: renderAccount,
    basics: renderBasics,
  }[page]?.() || renderOverview();
}

function renderOverview() {
  const s = state.stats || {};
  const warning = (s.warnings || [])[0] || { level: "success", title: "未发现重大问题", detail: "当前没有需要立即处理的异常。" };
  const isHealthy = warning.level === "success";
  const accountReady = Boolean(s.account_connected || state.account?.logged_in);
  const running = s.running !== false;
  const schedulerHealthy = Boolean(s.scheduler_healthy);
  const proactiveMax = num(s.proactive_max);
  const proactiveProgress = proactiveMax > 0 ? num(s.proactive_used) / proactiveMax * 100 : 0;
  const replyLimit = Math.max(1, num(currentValue("AUTONOMOUS_REPLY_DAILY_MAX"), 80));
  const privateLimit = Math.max(1, num(currentValue("AUTONOMOUS_PRIVATE_DAILY_MAX"), 30));
  return `${pageHead("MONITOR", "运行总览", "最重要的账号、调度、互动、安全与配额状态集中在这里。", button("刷新状态", "refresh", "refresh"))}
    <section class="health-hero ${isHealthy ? "is-healthy" : "has-warning"}">
      <div class="health-orb">${icon(isHealthy ? "shield" : "lightning")}</div>
      <div class="health-copy"><span>${isHealthy ? "SYSTEM HEALTHY" : "ATTENTION NEEDED"}</span><h2>${esc(warning.title)}</h2><p>${esc(warning.detail)}</p></div>
      <div class="health-side"><strong>${num(s.activity_level, state.persona.energy)}%</strong><span>${esc(s.activity_label || "今日活跃度")}</span><div class="activity-mini"><i style="width:${clamp(num(s.activity_level, 55), 0, 100)}%"></i></div></div>
    </section>
    <section class="metrics-grid">
      ${metricCard("评论回复", fmt(s.comment_replies_today), "评论区已发送", "message", "pink", num(s.comment_replies_today) / replyLimit * 100, replyLimit)}
      ${metricCard("私信回复", fmt(s.private_replies_today), "安全私信已发送", "user", "violet", num(s.private_replies_today) / privateLimit * 100, privateLimit)}
      ${metricCard("主动行为", fmt(s.proactive_used), proactiveMax ? "浏览、互动与分享" : "未设置有效配额", "play", "blue", proactiveProgress, proactiveMax || "—")}
      ${metricCard("内容过滤", fmt(s.filtered_today), "低价值、广告与重复内容", "shield", "green")}
      ${metricCard("记忆总量", fmt(s.memory_total), `${fmt(s.profiles_total)} 个用户画像`, "memory-card", "orange")}
      ${metricCard("今日失败", fmt(s.failed_today), num(s.failed_today) ? "建议立即检查 AstrBot 日志" : "未发现执行失败", "lightning", num(s.failed_today) ? "red" : "green")}
    </section>
    <section class="overview-grid">
      <article class="card monitor-card">
        ${sectionHead("关键链路", "一眼确认 Bot 是否能安全地继续工作", "controller")}
        <div class="monitor-list">
          ${monitorRow("B站账号", accountReady ? "已连接" : "未连接", accountReady ? "Cookie 已配置" : "需要扫码登录", accountReady, "account")}
          ${monitorRow("后台主循环", running ? "运行中" : "已停止", running ? "持续轮询互动事件" : "重载插件或检查启动日志", running, "runtime")}
          ${monitorRow("今日调度", schedulerHealthy ? "正常" : "需检查", s.next_action || "暂无待执行事件", schedulerHealthy, "schedule")}
          ${monitorRow("权限隔离", state.security.tool_isolation !== false ? "已保护" : "已关闭", state.security.tool_isolation !== false ? "B站端无法调用高风险工具" : "建议立即开启工具隔离", state.security.tool_isolation !== false, "security")}
        </div>
      </article>
      <article class="card monitor-card">
        ${sectionHead("今日节奏", "当前行为密度与即将执行的动作", "calendar", statusPill(state.persona.mood || "平静", "violet"))}
        <div class="next-action"><span>下一动作</span><strong>${esc(s.next_action || "今日暂无待执行事件")}</strong><p>剩余事件 ${fmt(state.scheduleStats.remaining)} 个 · 已完成 ${fmt(state.scheduleStats.completed)} 个</p></div>
        <div class="quota-list">
          ${quotaRow("评论回复", num(s.comment_replies_today), replyLimit, "pink")}
          ${quotaRow("私信回复", num(s.private_replies_today), privateLimit, "violet")}
          ${quotaRow("主动行为", num(s.proactive_used), proactiveMax || 1, "blue")}
        </div>
      </article>
    </section>`;
}

function monitorRow(label, value, detail, ok, target) {
  return `<button class="monitor-row" data-page-target="${target === "runtime" ? "basics" : target}" type="button"><span class="monitor-state ${ok ? "ok" : "warn"}">${icon(ok ? "shield" : "lightning")}</span><span><strong>${esc(label)}</strong><small>${esc(detail)}</small></span><b>${esc(value)}</b>${icon("arrow-right")}</button>`;
}

function quotaRow(label, used, total, tone) {
  const safeTotal = Math.max(1, num(total));
  const percent = clamp(num(used) / safeTotal * 100, 0, 100);
  return `<div class="quota-row"><div><span>${esc(label)}</span><b>${fmt(used)} / ${fmt(total)}</b></div><div class="quota-track tone-${tone}"><i style="width:${percent}%"></i></div></div>`;
}

function parseInterestReport(report) {
  const lines = String(report || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const statsLine = lines.find((line) => line.startsWith("统计窗口：")) || "";
  const stats = statsLine.replace(/^统计窗口：/, "").split("｜").filter(Boolean);
  const sections = [];
  let current = null;
  let note = "";
  lines.forEach((line) => {
    const heading = line.match(/^【(.+)】$/);
    if (heading) {
      current = { title: heading[1], items: [] };
      sections.push(current);
      return;
    }
    if (line.startsWith("说明：")) {
      note = line;
      current = null;
      return;
    }
    if (current) current.items.push(line.replace(/^[·•-]\s*/, ""));
  });
  return { stats, sections, note };
}

function renderInterestSnapshot() {
  const snapshot = state.interest || {};
  const parsed = parseInterestReport(snapshot.report);
  const sourceLabel = snapshot.stale ? "安全回退" : snapshot.cached ? "30 秒缓存" : "刚刚同步";
  const sourceTone = snapshot.stale ? "orange" : snapshot.cached ? "violet" : "green";
  if (!parsed.stats.length && !parsed.sections.length) {
    return `<div class="interest-snapshot is-empty"><div>${icon("shield")}</div><strong>兴趣状态暂时无法读取</strong><p>${esc(snapshot.error || "回复与主动看片仍会按现有配置继续运行。")}</p></div>`;
  }
  return `<div class="interest-snapshot">
    <div class="interest-snapshot-head">
      <div><strong>只展示观察结果，不会在这里直接改写偏好</strong><span>当前学习到的视频兴趣</span></div>
      ${statusPill(sourceLabel, sourceTone)}
    </div>
    <div class="interest-stat-row">${parsed.stats.map((item) => `<span>${esc(item)}</span>`).join("")}</div>
    <div class="interest-insight-grid">${parsed.sections.map((section) => `<article><strong>${esc(section.title)}</strong><div>${section.items.length ? section.items.map((item) => `<p>${esc(item)}</p>`).join("") : `<p>暂无数据</p>`}</div></article>`).join("")}</div>
    <div class="interest-snapshot-foot"><span>${icon("shield")}只读接口 · 内容已限长并转义 · 不含账号凭据</span><small>${esc(parsed.note || "重复出现的兴趣证据才会逐渐提高选片优先级。")}${snapshot.updated_at ? ` · 更新于 ${esc(snapshot.updated_at)}` : ""}</small></div>
  </div>`;
}

function renderInterestConfigSection() {
  const keys = ["INTEREST_APPLY_TO_PRIVATE", "INTEREST_SELECTION_PROMPT", "CUSTOM_REPLY_INSTRUCTION"].filter(hasKey);
  if (!keys.length) return "";
  return `<section class="card section-card interest-config-card">
    ${sectionHead("兴趣选择与评论提示词", "总开关、当前兴趣和回复提示词集中在这里", "star")}
    <div class="interest-config-layout">
      <div class="interest-config-controls">
        <div class="interest-selector-row">
          <div><strong>不是每条消息都必须回复</strong><p>先过滤广告、复读和低价值内容，再按兴趣选择真正值得回应的评论和私信。</p></div>
          ${hasKey("ENABLE_INTEREST_BASED_REPLY") ? renderControl("ENABLE_INTEREST_BASED_REPLY", state.schema.ENABLE_INTEREST_BASED_REPLY) : ""}
        </div>
        ${renderFields(keys)}
      </div>
      <div class="interest-config-observation">${renderInterestSnapshot()}</div>
    </div>
  </section>`;
}

function renderInteraction() {
  return `${pageHead("INTERACTION", "回复与互动", "把值得回应的内容挑出来，再用明确的频率、冷却和硬上限保护账号。", statusPill(`${fmt(state.stats.filtered_today)} 条已过滤`, "green"))}
    <div class="two-column">
      ${renderConfigSection("内容筛选", "先做确定性过滤，再执行兴趣判断", ["FILTER_LOW_VALUE_MESSAGES", "FILTER_DUPLICATE_MESSAGES", "FILTER_AD_MESSAGES"], "shield")}
      ${renderConfigSection("回复边界", "概率保留为最后一道节奏控制", ["ENABLE_REPLY", "REPLY_PROBABILITY_PERCENT", "REPLY_COOLDOWN", "POLL_INTERVAL", "REPLY_ALWAYS_UIDS", "ENABLE_SIMILAR_SKIP", "REPLY_SIMILARITY_PERCENT"], "controller")}
    </div>
    ${renderInterestConfigSection()}
    <div class="two-column">
      ${renderConfigSection("B站私信回复", "先决定回复对象，再设置回复方式与人设补充", ["ENABLE_PRIVATE_MESSAGES", "PRIVATE_MESSAGE_REPLY_SCOPE", "PRIVATE_MESSAGE_REPLY_WHITELIST_UIDS", "PRIVATE_MESSAGE_AUTO_REPLY", "CUSTOM_PRIVATE_MESSAGE_INSTRUCTION"], "user")}
      ${renderConfigSection("B站私信轮询", "集中管理请求节奏、活跃窗口和单轮处理边界", ["PRIVATE_MESSAGE_POLL_INTERVAL", "PRIVATE_MESSAGE_IDLE_POLL_INTERVAL", "PRIVATE_MESSAGE_ACTIVE_WINDOW", "PRIVATE_MESSAGE_MAX_PER_POLL", "PRIVATE_MESSAGE_MAX_MESSAGE_AGE"], "clock")}
    </div>
    <div class="two-column">
      ${renderConfigSection("B站私信视频与查询", "收到分享后可看视频、查 UP 主或公开视频，并控制再次分享的冷却", ["PRIVATE_MESSAGE_AUTO_WATCH_VIDEO", "PRIVATE_MESSAGE_BILI_SEARCH_ENABLED", "PRIVATE_MESSAGE_BILI_SEARCH_LIMIT", "BILI_PRIVATE_SHARE_TOOL_ENABLED", "BILI_PRIVATE_SHARE_COOLDOWN"], "search")}
      ${renderConfigSection("直播间弹幕互动", "进入指定 UP 主直播间，监听公开弹幕并由 Bot 的B站账号参与互动；同时限制发送速度与长度，避免抢话和刷屏", ["ENABLE_LIVE_DANMAKU_REPLY", "LIVE_DANMAKU_ROOM_ID", "LIVE_DANMAKU_POLL_INTERVAL", "LIVE_DANMAKU_REPLY_COOLDOWN", "LIVE_DANMAKU_MAX_PER_MINUTE", "LIVE_DANMAKU_REPLY_MAX_LENGTH", "CUSTOM_LIVE_DANMAKU_INSTRUCTION"], "video")}
    </div>
    ${renderConfigSection("分享解析", "统一管理自动识别、手动触发和视频切片限制", ["ENABLE_BILI_SHARE_PARSE", "BILI_SHARE_PARSE_AUTO_TRIGGER_ENABLED", "BILI_SHARE_PARSE_MANUAL_TRIGGER_ENABLED", "BILI_SHARE_PARSE_LLM_TRIGGER_ENABLED", "BILI_SHARE_PENDING_MAX_AGE", "BILI_SHARE_PARSE_SEND_VIDEO", "BILI_SHARE_PARSE_SEGMENT_SECONDS", "BILI_SHARE_PARSE_MAX_SEGMENTS", "BILI_SHARE_PARSE_MAX_VIDEO_MB", "BILI_SHARE_PARSE_VIDEO_MAX_HEIGHT", "BILI_SHARE_PARSE_COOLDOWN"], "search")}`;
}

const EVENT_STYLES = {
  proactive: { label: "主动浏览", gradient: ["#24c7a3", "#4f8cff"], icon: "play" },
  dynamic: { label: "发布动态", gradient: ["#f06ea9", "#ff936a"], icon: "message" },
  dynamic_watch: { label: "关注动态", gradient: ["#3bbbc4", "#6d72e7"], icon: "search" },
  bangumi: { label: "追番", gradient: ["#9b7bf6", "#557eea"], icon: "video" },
  follow: { label: "特别关注", gradient: ["#efc45c", "#69bf85"], icon: "star" },
  sleep: { label: "休眠", gradient: ["#9794b7", "#646aa5"], icon: "pause" },
};

const AUTONOMY_CAPABILITIES = [
  { id: "plan-generation", title: "每日计划编排", icon: "calendar", toggle: "ENABLE_AUTONOMOUS_DAILY_PLAN", description: "决定由模型安排今天的事件，或切换到管理员固定计划。", keys: ["AUTONOMOUS_PLAN_GENERATION_MODE", "AUTONOMOUS_PLAN_AFTER_SLEEP_MINUTES", "AUTONOMOUS_PLAN_GENERATION_TIME", "AUTONOMOUS_PLAN_RETRY_MINUTES", "AUTONOMOUS_PROACTIVE_WINDOW_MINUTES", "AUTONOMOUS_PLAN_PROMPT"] },
  { id: "proactive", title: "主动浏览", icon: "play", toggle: "ENABLE_PROACTIVE", description: "在主动浏览时间段内观看视频，并执行受评分阈值保护的互动。", keys: ["PROACTIVE_TIMES_COUNT", "PROACTIVE_VIDEO_COUNT", "PROACTIVE_DAILY_LIMIT", "PROACTIVE_COMMENT_COUNT", "PROACTIVE_COMMENT_DAILY_LIMIT", "VIDEO_VISUAL_ANALYSIS_POLICY", "ENABLE_VIDEO_LONG_TERM_MEMORY", "VIDEO_MEMORY_DETAIL_DAYS", "VIDEO_MEMORY_FADE_DAYS"] },
  { id: "dynamic", title: "动态发布", icon: "message", toggle: "ENABLE_DYNAMIC", description: "按今日计划生成并发布 B站动态；时间由固定计划或自主计划统一安排。", keys: ["DYNAMIC_TOPICS", "CUSTOM_DYNAMIC_INSTRUCTION"] },
  { id: "dynamic-watch", title: "关注动态", icon: "search", toggle: "ENABLE_DYNAMIC_WATCH", description: "查看关注用户的新动态图文与视频投稿，触发节奏由统一日程管理。", keys: ["DYNAMIC_WATCH_DAILY_LIMIT", "DYNAMIC_WATCH_SPECIAL_ONLY", "DYNAMIC_WATCH_INCLUDE_VIDEO_POSTS", "DYNAMIC_WATCH_INTEREST_PROMPT"] },
  { id: "special-follow", title: "特别关注", icon: "star", toggle: "SPECIAL_FOLLOW_ENABLED", description: "巡视特别关注用户；不在此处重复设置固定时刻或触发次数。", keys: [] },
  { id: "bangumi", title: "番剧日程", icon: "video", toggle: "ENABLE_BANGUMI", description: "番剧是独立日程，不并入主动浏览；需同时开启番剧功能与主动追番，才会在计划和事件环中出现。", keys: ["BANGUMI_PROACTIVE", "BANGUMI_POOLS", "BANGUMI_EPISODE_COUNT", "BANGUMI_CONTINUE_SCORE", "BANGUMI_DAILY_LIMIT", "BANGUMI_COMMENT", "BANGUMI_AUTO_FOLLOW"] },
  { id: "prefilter", title: "内容挑选", icon: "search", toggle: "ENABLE_PROACTIVE_LLM_PREFILTER", description: "用关注源、兴趣提示词和模型预筛选决定今天值得看的内容。", dependency: "依赖主动浏览", keys: ["PROACTIVE_FOLLOW_UIDS", "PROACTIVE_SEARCH_QUERY_PROMPT", "PROACTIVE_TASTE_WINDOW_DAYS", "PROACTIVE_VIDEO_POOLS", "PROACTIVE_LLM_PREFILTER_MAX_REJECTS", "CUSTOM_PROACTIVE_INSTRUCTION"] },
  { id: "owner-share", title: "给主人分享", icon: "heart", toggle: "ENABLE_OWNER_RECOMMEND", description: "统一管理给主人推荐与 QQ 当前活动同步；活动状态任务结束后立即清除，不写入长期记忆。", dependency: "依赖主动浏览", keys: ["RECOMMEND_OWNER_DELIVERY", "OWNER_QQ_UMO", "ENABLE_CROSS_PLATFORM_ACTIVITY_STATUS", "RECOMMEND_OWNER_MIN_SCORE", "RECOMMEND_OWNER_DAILY_LIMIT", "CUSTOM_RECOMMEND_INSTRUCTION"] },
];


function activityLabel(value) {
  const n = num(value);
  return n < 25 ? "低迷" : n < 50 ? "平稳" : n < 75 ? "活跃" : "高能";
}

function minutesOf(value) {
  const match = String(value || "").trim().match(/^(\d{1,2}):(\d{2})$/);
  if (!match) return null;
  const hour = Number.parseInt(match[1], 10);
  const minute = Number.parseInt(match[2], 10);
  if (!Number.isInteger(hour) || !Number.isInteger(minute) || hour < 0 || hour > 23 || minute < 0 || minute > 59) return null;
  return hour * 60 + minute;
}

function currentClockText() {
  const now = new Date();
  return `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
}

function pointForMinute(minute, radius = 132, center = 180) {
  const angle = minute / 1440 * Math.PI * 2 - Math.PI / 2;
  return [center + radius * Math.cos(angle), center + radius * Math.sin(angle)];
}

function arcPath(start, end, radius = 132, center = 180) {
  let duration = end - start;
  if (duration <= 0) duration += 1440;
  const a = pointForMinute(start, radius, center);
  const b = pointForMinute((start + duration) % 1440, radius, center);
  return `M ${a[0].toFixed(2)} ${a[1].toFixed(2)} A ${radius} ${radius} 0 ${duration > 720 ? 1 : 0} 1 ${b[0].toFixed(2)} ${b[1].toFixed(2)}`;
}

function eventWindow(event) {
  const start = minutesOf(event?.start_time);
  const end = minutesOf(event?.end_time);
  if (start === null || end === null || start === end) return null;
  let duration = end - start;
  if (duration <= 0) duration += 1440;
  return { start, end, duration };
}

function eventArcData(event) {
  const window = eventWindow(event);
  if (window) return { ...window, d: arcPath(window.start, window.end) };
  const minute = minutesOf(event?.time);
  if (minute === null) return null;
  const start = (minute - 15 + 1440) % 1440;
  const end = (minute + 15) % 1440;
  const d = start < end ? arcPath(start, end) : `${arcPath(start, 1439)} ${arcPath(0, end)}`;
  return { start, end, duration: 30, d };
}

function ringEventArc(event, originalIndex, orderIndex) {
  const geometry = eventArcData(event);
  if (!geometry) return "";
  const style = EVENT_STYLES[event.kind] || EVENT_STYLES.proactive;
  const meta = eventPhaseMeta(event);
  const window = eventWindow(event);
  const label = esc(`${window ? `${event.start_time}-${event.end_time}` : event.time} ${event.label || style.label}，${meta.label}`);
  const active = originalIndex === state.selectedScheduleIndex;
  const startPoint = pointForMinute(geometry.start, 132);
  const endPoint = pointForMinute(geometry.end, 132);
  return `<g class="ring-event-group phase-${meta.phase} ${window ? "is-window" : "is-point"}" data-ring-group="${originalIndex}">
    <path class="ring-event-hit" data-segment-index="${originalIndex}" d="${geometry.d}" tabindex="0" role="button" aria-label="${label}" aria-pressed="${active}" />
    <path class="ring-event ${active ? "is-active" : ""} is-${meta.phase}" data-ring-index="${originalIndex}" d="${geometry.d}" pathLength="100" stroke="url(#grad-${event.kind || "proactive"})" style="--segment-delay:${orderIndex * 48}ms" aria-hidden="true" />
  </g>`;
}

function currentMinuteOfDay() {
  const now = new Date();
  return now.getHours() * 60 + now.getMinutes();
}

function eventPhase(event, nowMinute = currentMinuteOfDay()) {
  if (event?.triggered) return "done";
  const minute = minutesOf(event?.time);
  if (minute === null) return "invalid";
  return minute < nowMinute ? "overdue" : "upcoming";
}

function eventPhaseMeta(event) {
  const phase = eventPhase(event);
  if (phase === "done") return { phase, label: "已完成", detail: "已按计划执行" };
  if (phase === "overdue") return { phase, label: "已错过", detail: "触发窗口已过，今天不会补执行" };
  if (phase === "invalid") return { phase, label: "时间无效", detail: "请修正该事件的执行时刻后保存" };
  return { phase, label: "待执行", detail: "等待计划时刻" };
}

function nextScheduleEvent(events) {
  const nowMinute = currentMinuteOfDay();
  const upcoming = events
    .filter((event) => {
      const minute = minutesOf(event.time);
      return !event.triggered && minute !== null && minute >= nowMinute;
    })
    .sort((a, b) => minutesOf(a.time) - minutesOf(b.time));
  if (upcoming.length) return upcoming[0];
  const remote = state.scheduleStats.next;
  const remoteMinute = minutesOf(remote?.time);
  if (remote && !remote.triggered && remoteMinute !== null && remoteMinute >= nowMinute) return remote;
  return null;
}

function renderScheduleRing(events) {
  const sleepStart = num(currentValue("SLEEP_START"), num(state.schedule.sleep_start, 2)) * 60;
  const sleepEnd = num(currentValue("SLEEP_END"), num(state.schedule.sleep_end, 8)) * 60;
  const selected = events[state.selectedScheduleIndex] || null;
  const next = nextScheduleEvent(events);
  const selectedStyle = EVENT_STYLES[selected?.kind || next?.kind] || EVENT_STYLES.sleep;
  const now = new Date();
  const nowMinute = now.getHours() * 60 + now.getMinutes();
  const nowAngle = nowMinute / 1440 * 360;
  const ordered = events
    .map((event, index) => ({ event, index, minute: minutesOf(event.time) }))
    .filter((item) => item.minute !== null)
    .sort((a, b) => a.minute - b.minute);
  const ticks = Array.from({ length: 24 }, (_, hour) => {
    const outer = pointForMinute(hour * 60, 157, 180);
    const inner = pointForMinute(hour * 60, hour % 3 === 0 ? 146 : 150, 180);
    return `<line class="hour-tick ${hour % 3 === 0 ? "major" : ""}" x1="${inner[0]}" y1="${inner[1]}" x2="${outer[0]}" y2="${outer[1]}" />`;
  }).join("");
  const labels = [0, 6, 12, 18].map((hour) => {
    const p = pointForMinute(hour * 60, 171, 180);
    return `<text class="hour-label" x="${p[0]}" y="${p[1] + 4}" text-anchor="middle">${String(hour).padStart(2, "0")}</text>`;
  }).join("");
  const sleepArc = sleepStart === sleepEnd ? "" : `<path class="sleep-arc" d="${arcPath(sleepStart, sleepEnd, 111)}" stroke="url(#grad-sleep)" />`;
  const selectedMeta = selected ? eventPhaseMeta(selected) : null;
  const center = selected
    ? `<span>已选择事件</span><strong>${esc(eventWindow(selected) ? `${selected.start_time}–${selected.end_time}` : selected.time)}</strong><b>${esc(selected.label || (EVENT_STYLES[selected.kind] || EVENT_STYLES.proactive).label)}</b><small>${selectedMeta.label} · ${selectedMeta.detail}</small>`
    : `<span>当前时间</span><strong>${currentClockText()}</strong><b>${next ? `下一步 · ${esc(next.label || (EVENT_STYLES[next.kind] || EVENT_STYLES.proactive).label)}` : "今日暂无后续事件"}</b><small>${next?.time ? `预计 ${esc(next.time)} 执行` : "过时未执行项目会在列表中标记为已错过"}</small>`;
  return `<div class="ring-shell">
    <svg class="schedule-ring" viewBox="0 0 360 360" aria-label="24小时事件环">
      <defs>${Object.entries(EVENT_STYLES).map(([key, item]) => `<linearGradient id="grad-${key}" x1="0" y1="0" x2="1" y2="1"><stop stop-color="${item.gradient[0]}"/><stop offset="1" stop-color="${item.gradient[1]}"/></linearGradient>`).join("")}</defs>
      <circle class="ring-track" cx="180" cy="180" r="132" />${ticks}${labels}${sleepArc}
      ${ordered.map(({ event, index }, orderIndex) => ringEventArc(event, index, orderIndex)).join("")}
      <g class="now-pointer" transform="rotate(${nowAngle} 180 180)" aria-label="当前时间 ${currentClockText()}">
        <path class="now-pointer-halo" d="M180 39 L165 9 Q180 4 195 9 Z" />
        <path class="now-pointer-cone" d="M180 36 L171 12 Q180 8 189 12 Z" />
        <path class="now-pointer-highlight" d="M180 33 L176 14" />
      </g>
    </svg>
    <button class="ring-center" type="button" aria-label="显示当前时间">${center}</button>
    <div class="ring-glow" style="--ring-a:${selectedStyle.gradient[0]};--ring-b:${selectedStyle.gradient[1]}"></div>
  </div>`;
}

function renderActivityControl() {
  if (!hasKey("AUTONOMOUS_ACTIVITY_LEVEL")) return "";
  const value = clamp(num(currentValue("AUTONOMOUS_ACTIVITY_LEVEL"), 55), 0, 100);
  return `<section class="activity-panel activity-enter ${value >= 100 ? "is-max" : ""}">
    <div class="activity-copy"><span>TODAY'S ACTIVITY</span><h2><b id="activity-value">${value}</b><small>%</small></h2><p id="activity-label">${activityLabel(value)}状态 · 事件更频繁，活动时间也更长</p></div>
    <div class="activity-slider-wrap"><div class="activity-track" style="--activity:${value}%"><span class="activity-fill"></span><input id="activity-slider" class="activity-slider" data-config-key="AUTONOMOUS_ACTIVITY_LEVEL" type="range" min="0" max="100" step="1" value="${value}" aria-label="今日基础活跃度" /></div><div class="activity-scale"><span>低迷</span><span>平稳</span><span>活跃</span><span>高能</span></div></div>
  </section>`;
}

function renderEventList(events) {
  if (!events.length) return `<div class="empty-event">${icon("calendar")}<strong>今天没有主动事件</strong><span>关闭总开关、低活跃度或范围上限为 0 时，这是正常状态。</span></div>`;
  return `<div class="event-list">${events.map((event, index) => {
    const style = EVENT_STYLES[event.kind] || EVENT_STYLES.proactive;
    const meta = eventPhaseMeta(event);
    return `<button class="event-row phase-${meta.phase} ${index === state.selectedScheduleIndex ? "is-active" : ""}" data-segment-index="${index}" type="button"><span class="event-color" style="--a:${style.gradient[0]};--b:${style.gradient[1]}">${icon(style.icon)}</span><span class="event-time">${esc(eventWindow(event) ? `${event.start_time}–${event.end_time}` : event.time)}</span><span class="event-copy"><strong>${esc(event.label || style.label)}</strong><small>${esc(event.description || "按今日计划执行")}</small></span><span class="event-status ${meta.phase}" title="${esc(meta.detail)}">${meta.label}</span></button>`;
  }).join("")}</div>`;
}

function renderSelectedEvent(events) {
  const event = state.scheduleDraft.events?.[state.selectedScheduleIndex] || events[state.selectedScheduleIndex];
  if (!event) {
    const next = nextScheduleEvent(events);
    const style = EVENT_STYLES[next?.kind] || EVENT_STYLES.proactive;
    return `<div id="selected-event" class="selected-event is-next" style="--a:${style.gradient[0]};--b:${style.gradient[1]}"><span class="selected-event-icon">${icon(next ? style.icon : "clock")}</span><div><span>${next ? "下一执行" : "今日进度"}</span><h3>${next?.time ? `${esc(next.time)} · ${esc(next.label || style.label)}` : "今日暂无后续事件"}</h3><p>${esc(next?.description || "过去未完成的事件会标记为已错过，不会在稍后补执行。")}</p></div></div>`;
  }
  const style = EVENT_STYLES[event.kind] || EVENT_STYLES.proactive;
  const meta = eventPhaseMeta(event);
  return `<div id="selected-event" class="selected-event phase-${meta.phase}" style="--a:${style.gradient[0]};--b:${style.gradient[1]}"><span class="selected-event-icon">${icon(style.icon)}</span><div><span>${meta.phase === "done" ? "已完成事件" : meta.phase === "overdue" ? "已错过事件" : "计划事件"}</span><h3>${esc(eventWindow(event) ? `${event.start_time}–${event.end_time}` : event.time)} · ${esc(event.label || style.label)}</h3><p>${esc(event.description || "Bot 会按今天的计划执行。")} · ${esc(meta.detail)}</p></div></div>`;
}

const PROACTIVE_BEHAVIORS = [
  ["PROACTIVE_LIKE", "PROACTIVE_LIKE_MIN_SCORE", "点赞", "heart"],
  ["PROACTIVE_COIN", "PROACTIVE_COIN_MIN_SCORE", "投币", "trophy"],
  ["PROACTIVE_FAV", "PROACTIVE_FAV_MIN_SCORE", "收藏", "star"],
  ["PROACTIVE_COMMENT", "PROACTIVE_COMMENT_MIN_SCORE", "评论", "message"],
  ["PROACTIVE_FOLLOW", "PROACTIVE_FOLLOW_MIN_SCORE", "关注", "user"],
];

function renderBehaviorMatrix() {
  const cards = PROACTIVE_BEHAVIORS.filter(([toggle, score]) => hasKey(toggle) && hasKey(score)).map(([toggle, score, label, iconName]) => {
    const enabled = Boolean(currentValue(toggle));
    const scoreValue = clamp(num(currentValue(score), 0), 0, 10);
    return `<article class="behavior-card ${enabled ? "is-enabled" : ""}"><div class="behavior-head"><span>${icon(iconName)}</span><strong>${label}</strong>${renderControl(toggle, state.schema[toggle])}</div><div class="behavior-score"><div><span>最低评分</span><output id="score-${score}" for="range-${score}">${scoreValue} 分</output></div><input id="range-${score}" class="behavior-range" data-config-key="${score}" type="range" min="0" max="10" step="1" value="${scoreValue}" style="--score:${scoreValue * 10}%" aria-label="${label}最低评分" /></div></article>`;
  }).join("");
  return `<div class="behavior-matrix">${cards}</div>`;
}

function renderPlanStatus(plan, autonomous) {
  const failed = autonomous && plan.generation_status === "error";
  const status = failed ? "今日模型计划未生成，不新增自动事件" : autonomous ? "模型计划已通过安全上限校验" : "管理员固定计划";
  const detail = failed
    ? `${plan.model_error || "计划模型暂时没有返回有效内容。"} 可稍后重新生成；评论、私信等功能仍按各自开关运行。`
    : plan.rationale || (autonomous ? "保存修改后调用当前模型生成当天计划。" : "保存修改后按准确时刻刷新当天计划。");
  return `<div class="plan-status ${failed ? "has-error" : autonomous ? "is-model" : "is-fixed"}"><span>${icon(failed ? "lightning" : autonomous ? "star" : "clock")}</span><div><strong>${esc(status)}</strong><p>${esc(detail)}</p></div>${plan.generated_at ? `<small>${esc(plan.generated_at)}</small>` : ""}</div>`;
}

function renderAdminCapRow(label, maxKey, unit) {
  const maximum = num(currentValue(maxKey), 0);
  return `<div class="admin-range-row"><div class="admin-range-label"><strong>${esc(label)}</strong><small>只限制最多执行多少${esc(unit)}</small></div><div class="admin-range-control"><div class="range-number"><button type="button" data-step-key="${maxKey}" data-step-dir="-1" aria-label="减少${esc(label)}上限">−</button><input data-config-key="${maxKey}" data-range-bound="max" type="number" min="0" max="999" step="1" value="${maximum}" aria-label="${esc(label)}上限" /><button type="button" data-step-key="${maxKey}" data-step-dir="1" aria-label="增加${esc(label)}上限">＋</button></div><span class="range-unit">${esc(unit)}</span></div></div>`;
}

function renderAdminRanges() {
  const rows = [
    ["每日评论回复上限", "AUTONOMOUS_REPLY_DAILY_MAX", "次"],
    ["每日私信回复上限", "AUTONOMOUS_PRIVATE_DAILY_MAX", "次"],
    ["主动浏览轮次上限", "AUTONOMOUS_PROACTIVE_DAILY_MAX", "轮"],
    ["每日发布动态上限", "AUTONOMOUS_DYNAMIC_DAILY_MAX", "条"],
  ];
  const summary = rows.map(([label, maxKey, unit]) => `${label.replace("每日", "")} ${fmt(currentValue(maxKey))}${unit}`).join(" · ");
  return `<details class="admin-range-details"><summary><span><strong>管理员安全上限</strong><small>${esc(summary)}</small></span><i>${icon("arrow-right")}</i></summary><div class="admin-range-list">${rows.map((row) => renderAdminCapRow(...row)).join("")}<p class="admin-range-hint">所有数字都只是安全上限，不是必须完成的目标。主动浏览轮次、每轮视频数、每日视频总数和主动评论上限分别控制。</p></div></details>`;
}

function renderAutonomousTemplate(plan, autonomous, events) {
  const next = nextScheduleEvent(events);
  return `<section class="plan-template autonomous-template ${autonomous ? "is-active" : ""}" data-plan-template="autonomous" aria-hidden="${!autonomous}" ${autonomous ? "" : "inert"}>
    ${renderPlanStatus(plan, true)}
    <div class="plan-facts">
      <div><span>今日事件</span><strong>${events.length}</strong><small>只来自已启用能力</small></div>
      <div><span>下一事件</span><strong>${next?.time ? esc(next.time) : "—"}</strong><small>${esc(next?.label || "暂无待执行事件")}</small></div>
      <div><span>评论 / 私信上限</span><strong>${fmt(plan.reply_cap ?? plan.reply_target)} / ${fmt(plan.private_cap ?? plan.private_target)}</strong><small>只在有真实消息时回复，不要求用完</small></div>
      <div><span>计划来源</span><strong>${plan.generation_status === "error" ? "无新增事件" : "当前模型"}</strong><small>仅保存时更新当天计划</small></div>
    </div>
    ${renderConfigSection("自主计划提示词", "作为 B站每日安排的附加提示，不会替换 AstrBot 原人设", ["AUTONOMOUS_PLAN_PROMPT"], "star", "", "embedded-section")}
  </section>`;
}

function renderFixedTemplate(plan, autonomous) {
  const exactKeys = ["FIXED_PROACTIVE_WINDOWS", "FIXED_DYNAMIC_TIMES", "FIXED_DYNAMIC_WATCH_TIMES", "FIXED_BANGUMI_TIMES", "FIXED_SPECIAL_FOLLOW_TIMES"];
  return `<section class="plan-template fixed-template ${autonomous ? "" : "is-active"}" data-plan-template="fixed" aria-hidden="${autonomous}" ${autonomous ? "inert" : ""}>
    ${renderPlanStatus(plan, false)}
    <div class="fixed-target-grid">${["SLEEP_START", "SLEEP_END", "AUTONOMOUS_MIN_ACTION_GAP_MINUTES"].map((key) => renderField(key, { tile: true })).join("")}</div>
    <section class="embedded-section fixed-times-section">${sectionHead("准确执行时刻", "对应能力关闭时，该行时刻不会进入事件环；每个时间都可直接选择", "calendar")}<div class="fixed-times-grid">${exactKeys.map((key) => renderField(key, { tile: true })).join("")}</div></section>
  </section>`;
}

function renderPlanModeCard(plan, autonomous, events) {
  return `<section class="card plan-mode-card">
    <div class="plan-mode-head"><div><span>DAILY PLAN MODE</span><h2>当天计划生成方式</h2><p>切换只修改草稿，只有点击“保存修改”才会重新生成当天计划。</p></div><div class="plan-mode-switch ${autonomous ? "is-autonomous" : "is-fixed"}" role="tablist" aria-label="当天计划模式"><button class="${autonomous ? "is-active" : ""}" data-plan-mode="autonomous" role="tab" aria-selected="${autonomous}" type="button">${icon("star")}自主安排</button><button class="${autonomous ? "" : "is-active"}" data-plan-mode="fixed" role="tab" aria-selected="${!autonomous}" type="button">${icon("clock")}固定计划</button><i aria-hidden="true"></i></div></div>
    <div class="plan-template-stage ${autonomous ? "show-autonomous" : "show-fixed"}">${renderAutonomousTemplate(plan, autonomous, events)}${renderFixedTemplate(plan, autonomous)}</div>
    ${renderAdminRanges()}
  </section>`;
}

function capabilitySummary(item) {
  if (!currentValue(item.toggle)) return "总开关已关闭，不会生成相关事件";
  if (item.id === "plan-generation") return currentValue("ENABLE_AUTONOMOUS_DAILY_PLAN") ? `每日计划：${currentValue("AUTONOMOUS_PLAN_GENERATION_MODE") === "fixed_time" ? currentValue("AUTONOMOUS_PLAN_GENERATION_TIME") : `休眠后 ${fmt(currentValue("AUTONOMOUS_PLAN_AFTER_SLEEP_MINUTES"))} 分钟`}` : "当前使用固定计划";
  if (item.id === "proactive") return `每天最多 ${fmt(currentValue("PROACTIVE_TIMES_COUNT"))} 轮 · 每轮 ${fmt(currentValue("PROACTIVE_VIDEO_COUNT"))} 个 · 全天视频上限 ${num(currentValue("PROACTIVE_DAILY_LIMIT"), 0) > 0 ? fmt(currentValue("PROACTIVE_DAILY_LIMIT")) : "不限"}`;
  if (item.id === "owner-share") return `最低 ${fmt(currentValue("RECOMMEND_OWNER_MIN_SCORE"))} 分 · 每天最多 ${fmt(currentValue("RECOMMEND_OWNER_DAILY_LIMIT"))} 次`;
  if (item.id === "dynamic") return "时间由统一日程管理";
  if (item.id === "dynamic-watch") return `每天最多 ${fmt(currentValue("DYNAMIC_WATCH_DAILY_LIMIT"))} 次 · 包含视频投稿 ${currentValue("DYNAMIC_WATCH_INCLUDE_VIDEO_POSTS") ? "开启" : "关闭"}`;
  if (item.id === "special-follow") return "日程统一由自主安排或固定计划管理";
  if (item.id === "bangumi") return `${currentValue("BANGUMI_PROACTIVE") ? "主动追番" : "仅启用资料能力"} · 每天最多 ${fmt(currentValue("BANGUMI_DAILY_LIMIT"))} 次`;
  return "模型预筛选已启用";
}

function renderCapabilityCard(item) {
  const enabled = Boolean(currentValue(item.toggle));
  return `<article class="capability-card ${enabled ? "is-enabled" : ""}" data-capability-card="${item.id}"><div class="capability-top"><span class="capability-icon">${icon(item.icon)}</span>${renderControl(item.toggle, state.schema[item.toggle])}</div><div class="capability-copy"><strong>${esc(item.title)}</strong>${item.dependency ? `<em class="capability-dependency">${esc(item.dependency)}</em>` : ""}<p>${esc(item.description)}</p><small>${esc(capabilitySummary(item))}</small></div><button class="capability-settings" data-capability-open="${item.id}" type="button">${icon("settings")}详细设置</button></article>`;
}

function renderCapabilityCards() {
  return `<div class="capability-grid">${AUTONOMY_CAPABILITIES.filter((item) => hasKey(item.toggle)).map(renderCapabilityCard).join("")}</div>`;
}

function refreshCapabilityCard(id) {
  const item = AUTONOMY_CAPABILITIES.find((entry) => entry.id === id);
  const oldCard = content.querySelector(`[data-capability-card="${id}"]`);
  if (!item || !oldCard) return;
  oldCard.outerHTML = renderCapabilityCard(item);
  const newCard = content.querySelector(`[data-capability-card="${id}"]`);
  if (!newCard) return;
  bindConfigControls(newCard);
  bindStepperControls(newCard);
  newCard.querySelector("[data-capability-open]")?.addEventListener("click", () => openAutonomyDrawer(id));
}

function renderAutonomyDrawer(item) {
  const keys = item.keys.filter(hasKey);
  modalRoot.innerHTML = `<div class="drawer-backdrop" data-drawer-backdrop><aside class="autonomy-drawer" role="dialog" aria-modal="true" aria-labelledby="autonomy-drawer-title"><header><span class="drawer-icon">${icon(item.icon)}</span><div><small>AUTONOMY CAPABILITY</small><h2 id="autonomy-drawer-title">${esc(item.title)}</h2><p>${esc(item.description)}</p></div><button class="modal-close" data-drawer-close type="button" aria-label="关闭">×</button></header><div class="drawer-toggle"><div><strong>总开关</strong><span>关闭后保存，相关事件会从当天计划移除。</span></div>${renderControl(item.toggle, state.schema[item.toggle])}</div><div class="drawer-fields">${renderFields(keys)}</div><footer><button class="button soft" data-drawer-close type="button">完成设置</button></footer></aside></div>`;
  bindConfigControls(modalRoot);
  bindStepperControls(modalRoot);
  const close = () => {
    modalRoot.querySelector(".drawer-backdrop")?.classList.add("is-closing");
    window.setTimeout(() => {
      modalRoot.innerHTML = "";
      const drawerId = state.autonomyDrawer;
      state.autonomyDrawer = null;
      refreshCapabilityCard(drawerId);
    }, 210);
  };
  modalRoot.querySelectorAll("[data-drawer-close]").forEach((node) => node.addEventListener("click", close));
  modalRoot.querySelector("[data-drawer-backdrop]")?.addEventListener("click", (event) => { if (event.target === event.currentTarget) close(); });
  requestAnimationFrame(() => modalRoot.querySelector(".drawer-backdrop")?.classList.add("is-visible"));
}

function openAutonomyDrawer(id) {
  const item = AUTONOMY_CAPABILITIES.find((entry) => entry.id === id);
  if (!item) return;
  state.autonomyDrawer = id;
  renderAutonomyDrawer(item);
}

function hasEventCreatingCapability() {
  return Boolean(currentValue("ENABLE_PROACTIVE") || currentValue("ENABLE_DYNAMIC") || currentValue("ENABLE_DYNAMIC_WATCH") || currentValue("SPECIAL_FOLLOW_ENABLED") || (currentValue("ENABLE_BANGUMI") && currentValue("BANGUMI_PROACTIVE")));
}

function renderBeginnerGuide() {
  if (hasEventCreatingCapability()) return "";
  return `<div class="beginner-guide" role="note"><svg viewBox="0 0 180 62" aria-hidden="true"><path d="M12 8 C24 44 70 53 142 34" /><path d="M132 25 L143 34 L130 42" /></svg><div><strong>今天还没有可生成日程的主动能力</strong><span>先在下方开启“主动浏览”“动态发布”或其他能力，内容挑选和给主人分享会跟随主动浏览工作。</span></div></div>`;
}

function renderAutonomy() {
  const events = Array.isArray(state.schedule.events) ? state.schedule.events : [];
  const plan = state.schedule.autonomous_plan || {};
  const autonomous = Boolean(currentValue("ENABLE_AUTONOMOUS_DAILY_PLAN"));
  const phases = events.map((event) => eventPhase(event));
  const completedCount = phases.filter((phase) => phase === "done").length;
  const upcomingCount = phases.filter((phase) => phase === "upcoming").length;
  const overdueCount = phases.filter((phase) => phase === "overdue").length;
  const invalidCount = phases.filter((phase) => phase === "invalid").length;
  return `${pageHead("AUTONOMY", "自主与作息", "活跃度只影响已启用能力的事件密度；事件环、计划模板与能力总开关均连接真实配置。", `${button("重新生成今日计划", "regenerate-schedule", "refresh", "primary")}`)}
    ${renderActivityControl()}
    <section class="schedule-layout">
      <article class="card ring-card">
        ${sectionHead("24 小时时刻事件环", "", "clock", statusPill(autonomous ? "Bot 自主" : "固定计划", autonomous ? "violet" : "neutral"))}
        ${renderScheduleRing(events)}
        <div class="ring-legend">${Object.entries(EVENT_STYLES).map(([key, item]) => `<span><i style="--a:${item.gradient[0]};--b:${item.gradient[1]}"></i>${esc(item.label)}</span>`).join("")}</div>
      </article>
      <aside class="schedule-side"><article class="card event-card">${sectionHead("今日事件", `${completedCount} 已完成 · ${upcomingCount} 待执行${overdueCount ? ` · ${overdueCount} 已错过` : ""}${invalidCount ? ` · ${invalidCount} 时间无效` : ""}`, "calendar")}${renderSelectedEvent(events)}${renderEventList(events)}${renderBeginnerGuide()}</article></aside>
    </section>
    ${renderPlanModeCard(plan, autonomous, events)}
    ${renderConfigSection("全局行为预算与超时", "评论、私信、主动看片和直播共用最后一道总量保护；这里不负责重复重试", ["BEHAVIOR_BUDGET_ENABLED", "BEHAVIOR_GLOBAL_MAX_PER_MINUTE", "BEHAVIOR_GLOBAL_DAILY_LIMIT", "BEHAVIOR_ACTION_TIMEOUT_SECONDS"], "shield")}
    <section class="card section-card behavior-section">${sectionHead("主动行为评分", "管理员决定每个动作的最低内容评分；模型意愿不能绕过阈值", "controller")}${renderBehaviorMatrix()}</section>
    <section class="card capability-section">${sectionHead("主动能力总开关", "先决定是否允许这类行为，再进入独立子界面设置细节；关闭后不会生成对应日程", "controller")}${renderCapabilityCards()}</section>`;
}

function renderMemory() {
  const m = state.memory || {};
  const isolation = m.isolation_mode === "safe_share" ? "安全共享" : "平台隔离";
  return `${pageHead("MEMORY", "记忆与关系", "只展示真实记忆与好感度数据，不用伪造维度掩盖当前关系状态。", button("刷新数据", "refresh-memory", "refresh"))}
    <section class="metrics-grid four">
      ${metricCard("记忆总量", fmt(m.total), "长期记忆记录", "memory-card", "blue")}
      ${metricCard("评论记忆", fmt(m.comment), "来自评论区互动", "message", "pink")}
      ${metricCard("私信记忆", fmt(m.private), "来自B站私信", "user", "violet")}
      ${metricCard("自我经历", fmt(m.self), isolation, "heart", "orange")}
    </section>
    <section class="memory-grid">
      <article class="card relationship-card">
        ${sectionHead("用户画像与好感度", "按真实好感度和最近互动排序", "heart", statusPill(`${state.profiles.length} 个画像`, "pink"))}
        <div class="profile-list">${state.profiles.length ? state.profiles.slice(0, 12).map(renderProfile).join("") : `<div class="empty-inline">尚未积累用户画像</div>`}</div>
      </article>
      <aside class="memory-side">
        <article class="card memory-policy">${sectionHead("记忆边界", "跨平台策略当前状态", "lock")}
          <div class="policy-status"><span class="policy-icon">${icon(m.isolation_mode === "safe_share" ? "unlock" : "lock")}</span><div><strong>${esc(isolation)}</strong><p>${m.safe_share ? "仅给主人侧共享经过硬脱敏的无害趣事。" : "B站与 QQ/AstrBot 的具体记忆默认互不注入。"}</p></div></div>
          <button class="button soft wide" data-page-target="security" type="button">${icon("shield")}打开安全与记忆隔离设置</button>
        </article>
        <article class="card memory-maintenance">${sectionHead("记忆维护", "清理超过保留周期的老化记录", "trash")}
          <p>清理不会删除仍在保留期内的记忆，也不会重置用户好感度。</p>${button("清理过期记忆", "purge-memory", "trash", "danger")}
        </article>
      </aside>
    </section>
    ${renderConfigSection("好感度与心情", "关系温度只由真实好感度分数与配置提示词决定", ["ENABLE_AFFECTION", "ENABLE_MOOD", "AFFECTION_PROMPT_SPECIAL", "AFFECTION_PROMPT_CLOSE", "AFFECTION_PROMPT_FRIEND", "AFFECTION_PROMPT_NORMAL", "AFFECTION_PROMPT_STRANGER", "AFFECTION_PROMPT_COLD"], "heart")}`;
}

function renderProfile(profile) {
  const score = num(profile.affection);
  const percent = clamp((score + 100) / 2, 0, 100);
  return `<article class="profile-row"><div class="profile-avatar">${esc(String(profile.name || "用").slice(0, 1))}</div><div class="profile-main"><div><strong>${esc(profile.name || `UID ${profile.user_id}`)}</strong><span>${esc(profile.relationship || "普通")} · ${score} 分</span></div><p>${esc(profile.impression || "尚未形成稳定印象")}</p><div class="profile-tags">${(profile.tags || []).map((tag) => `<span>${esc(tag)}</span>`).join("")}</div><div class="profile-progress"><i style="width:${percent}%"></i></div></div><div class="profile-meta"><b>${fmt(profile.facts_count)}</b><span>事实</span><small>${esc(profile.last_interaction || "暂无时间")}</small></div></article>`;
}

function securityCount(keys) {
  const counts = state.security.by_type || {};
  return keys.reduce((sum, key) => sum + num(counts[key]), 0);
}

function renderToolSummary() {
  const allowed = Array.isArray(currentValue("BILI_TOOL_ALLOWLIST")) ? currentValue("BILI_TOOL_ALLOWLIST") : [];
  const selected = state.availableTools.filter((tool) => allowed.includes(tool.name) && tool.compatible && tool.active !== false);
  return `<div class="tool-picker-summary"><div class="tool-picker-copy"><span class="tool-picker-icon">${icon("controller")}</span><div><strong>${selected.length ? `已允许 ${selected.length} 个B站私信回复工具` : "B站私信回复不调用查询工具"}</strong><p>${selected.length ? selected.map((tool) => tool.label || tool.name).join("、") : "仍可正常回复B站私信，但不会为回答额外查询公开信息。"}</p></div></div><button class="button soft" data-action="open-tool-picker" type="button">${icon("settings")}选择工具</button></div>`;
}

function refreshToolSummary() {
  const oldSummary = content.querySelector(".tool-picker-summary");
  if (!oldSummary) return;
  oldSummary.outerHTML = renderToolSummary();
  content.querySelector('.tool-picker-summary [data-action="open-tool-picker"]')?.addEventListener("click", openToolPicker);
}

function renderSecurity() {
  const isolated = currentValue("BILI_TOOL_ISOLATION_ENABLED") !== false;
  const memoryMode = currentValue("MEMORY_ISOLATION_MODE") || "isolated";
  return `${pageHead("SECURITY", "安全与工具", "B站外部内容默认是不可信输入；工具、命令和跨平台记忆必须经过显式授权。", button("刷新审计", "refresh-security", "refresh"))}
    <section class="security-hero ${isolated ? "is-safe" : "is-risk"}"><span>${icon(isolated ? "lock" : "unlock")}</span><div><small>TOOL ISOLATION</small><h2>${isolated ? "B站端权限已隔离" : "工具隔离已关闭"}</h2><p>${isolated ? "B站评论与私信无法运行 AstrBot/QQ 命令、文件、Shell 或写操作。" : "建议立即重新开启隔离；只读白名单仍由后端硬限制。"}</p></div></section>
    <section class="metrics-grid four">
      ${metricCard("今日安全事件", fmt(state.security.today_total), "过滤、拒绝与审计总数", "shield", "blue")}
      ${metricCard("内容过滤", fmt(securityCount(["low_value_filtered", "duplicate_filtered", "ad_filtered"])), "低价值、复读与广告", "message", "green")}
      ${metricCard("工具拒绝", fmt(securityCount(["bili_tool_denied"])), "未授权请求未执行", "lock", "violet")}
      ${metricCard("记忆策略", memoryMode === "safe_share" ? "安全共享" : "平台隔离", "仅向主人侧开放脱敏摘要", "memory-card", "orange")}
    </section>
    ${renderConfigSection("工具隔离总控", "高风险工具不会因为关闭前端开关而自动获得权限；关闭 LLM 工具会拒绝全部 B站查询、看视频与搜索调用", ["BILI_TOOL_ISOLATION_ENABLED", "ENABLE_LLM_TOOLS", "BILI_ALLOW_SEARCH_TOOLS", "BILI_PROMPT_INJECTION_DEFENSE", "BILI_TOOL_AUDIT_ENABLED"], "shield")}
    <section class="card section-card tool-access-card">${sectionHead("B站端私信回复工具", "仅供B站私信收到消息后的回复模型按需查询公开信息；与 QQ/AstrBot 聊天工具分开，不会自动执行", "controller")}${renderToolSummary()}</section>
    <div class="two-column">
      ${renderConfigSection("记忆隔离与安全共享", "默认隔离；开启共享后也只向已绑定主人侧提供脱敏摘要", ["MEMORY_ISOLATION_MODE", "ENABLE_SAFE_CROSS_PLATFORM_MEMORY", "ENABLE_PRIVACY_REDACTION", "MEMORY_BLOCKED_PREFIXES", "MEMORY_BLOCKED_KEYWORDS", "CROSS_PLATFORM_MEMORY_PROMPT"], "memory-card")}
      ${renderConfigSection("私信安全", "链接域名、危险私信与拉黑白名单", ["PRIVATE_MESSAGE_AUTO_BLOCK", "PRIVATE_MESSAGE_BLOCK_WHITELIST_UIDS", "PRIVATE_MESSAGE_TRUSTED_DOMAINS"], "user")}
    </div>
    ${renderConfigSection("恶意告警与自动拉黑", "把平台风控与主人通知集中管理", ["ABUSE_ALERT_MODE", "ABUSE_ALERT_QQ_UMO", "ABUSE_ALERT_SCORE_THRESHOLD", "ENABLE_AUTO_BLOCK", "BLOCK_WHITELIST_UIDS", "AUTO_BLOCK_SCORE", "AUTO_BLOCK_NEGATIVE_TIMES"], "lightning")}`;
}

function renderAccount() {
  const a = state.account || {};
  const loggedIn = Boolean(a.logged_in);
  return `${pageHead("ACCOUNT", "账号连接", "连接状态、扫码登录和主人身份设置放在同一个页面，不再混入全部配置。", button("检查账号", "refresh-account", "refresh"))}
    ${loggedIn ? `<section class="account-hero card"><div class="account-avatar">${a.avatar ? `<img src="${esc(a.avatar)}" alt="${esc(a.name || "B站账号")}" />` : icon("user")}</div><div class="account-copy"><span>CONNECTED</span><h2>${esc(a.name || "B站账号")}</h2><p>UID ${esc(a.uid || "—")} · Lv${fmt(a.level)} · ${a.running ? "后台运行中" : "后台未运行"}</p></div><div class="account-actions">${statusPill("连接正常", "green")}${button("退出账号", "logout", "arrow-left", "danger")}</div></section>` : `<section class="login-layout"><article class="card qr-card">${sectionHead("扫码连接 B站", "二维码由插件实时向B站申请，登录凭据不会显示在页面中", "user")}<div id="qr-box" class="qr-box"><div class="qr-loading"><i></i><span>正在申请二维码…</span></div></div><div class="qr-status"><span class="status-dot"></span><strong id="qr-status">等待生成</strong></div>${button("重新生成二维码", "generate-qr", "refresh", "soft")}</article><article class="card login-help">${sectionHead("连接检查", "如果二维码无法显示，请按顺序排查", "shield")}<ol><li><span>1</span><div><strong>检查网络</strong><p>AstrBot 主机需要能够访问 B站登录接口。</p></div></li><li><span>2</span><div><strong>查看明确错误</strong><p>生成失败原因会直接显示在二维码区域，不再留空。</p></div></li><li><span>3</span><div><strong>扫码确认</strong><p>在 B站客户端完成扫码后还需要点击确认登录。</p></div></li></ol><p class="login-reason">${esc(a.reason || "当前没有有效登录凭据")}</p></article></section>`}
    ${loggedIn ? `<section class="metrics-grid four">${metricCard("今日评论回复", fmt(a.comment_reply_count), "账号已发送的评论回复", "message", "pink")}${metricCard("今日私信回复", fmt(a.private_reply_count), "账号已发送的私信回复", "user", "violet")}${metricCard("记忆", fmt(a.memory_count), "与当前角色相关", "memory-card", "blue")}${metricCard("好感度总量", fmt(a.affection_total), "所有已记录用户合计", "heart", "orange")}</section>` : ""}
    ${renderConfigSection("主人身份", "用于私信推荐、@主人和安全的跨平台记忆共享校验", ["OWNER_MID", "OWNER_NAME", "OWNER_BILI_NAME"], "heart")}`;
}

const BASIC_GROUP_ORDER = ["人设与模型", "Embedding 与记忆", "视频分析", "图片识别", "联网搜索", "图片生成", "总结", "性格演化", "Cookie 与系统", "高级接口"];

const BASIC_KEY_ORDER = {
  "人设与模型": ["USE_ASTRBOT_PERSONA", "CUSTOM_SYSTEM_PROMPT", "LLM_PROVIDER_ID", "LLM_CIRCUIT_FAILURE_THRESHOLD", "LLM_CIRCUIT_COOLDOWN_SECONDS"],
  "Embedding 与记忆": ["EMBED_API_KEY", "EMBED_API_BASE", "EMBED_MODEL", "EMBED_TIMEOUT_SECONDS"],
  "视频分析": ["VIDEO_VISION_PROVIDER_ID", "VIDEO_VISION_API_KEY", "VIDEO_VISION_API_BASE", "VIDEO_VISION_MODEL", "VIDEO_VISION_FORMAT", "VIDEO_VISION_FPS"],
  "图片识别": ["IMAGE_VISION_PROVIDER_ID", "IMAGE_VISION_API_KEY", "IMAGE_VISION_API_BASE", "IMAGE_VISION_MODEL"],
  "联网搜索": ["ENABLE_WEB_SEARCH", "WEB_SEARCH_BACKEND", "WEB_SEARCH_API_KEY", "WEB_SEARCH_API_BASE", "WEB_SEARCH_MODEL", "WEB_SEARCH_JUDGE_PROVIDER_ID", "WEB_SEARCH_MAX_RESULTS"],
  "图片生成": ["IMAGE_GEN_BACKEND", "IMAGE_GEN_API_KEY", "IMAGE_GEN_API_BASE", "IMAGE_GEN_MODEL", "IMAGE_GEN_WIDTH", "IMAGE_GEN_HEIGHT", "IMAGE_GEN_STEPS", "IMAGE_GEN_SCALE", "IMAGE_GEN_SAMPLER", "IMAGE_GEN_NEGATIVE_PROMPT"],
};

function basicGroupFor(key, field) {
  const group = descriptionMeta(field).group;
  if (/人设|模型可靠性/.test(group) || ["LLM_PROVIDER_ID", "USE_ASTRBOT_PERSONA", "CUSTOM_SYSTEM_PROMPT", "LLM_CIRCUIT_FAILURE_THRESHOLD", "LLM_CIRCUIT_COOLDOWN_SECONDS"].includes(key)) return "人设与模型";
  if (/性格演化/.test(group) || key.startsWith("EVOLVE_")) return "性格演化";
  if (/高级·记忆/.test(group) || key.startsWith("EMBED_")) return "Embedding 与记忆";
  if (key.startsWith("VIDEO_VISION_") || /视频分析/.test(group)) return "视频分析";
  if (key.startsWith("IMAGE_VISION_")) return "图片识别";
  if (/图片生成/.test(group) || key.startsWith("IMAGE_GEN_")) return "图片生成";
  if (/联网搜索/.test(group) || key.startsWith("WEB_SEARCH_")) return "联网搜索";
  if (/总结/.test(group) || key.includes("DAILY") || key.includes("WEEKLY")) return "总结";
  if (/系统/.test(group) || key.startsWith("COOKIE_")) return "Cookie 与系统";
  if (/高级/.test(group) || /(API|MODEL|PROVIDER)/.test(key)) return "高级接口";
  return null;
}

function renderCacheCard() {
  const cache = state.cache || {};
  const buckets = Object.entries(cache.buckets || {});
  const protectedItems = Array.isArray(cache.protected) ? cache.protected : ["B站 Cookie 与扫码登录状态", "记忆、画像与好感度", "日程和运行数据库"];
  return `<section class="card cache-card">
    ${sectionHead("缓存与临时文件", `当前占用 ${formatBytes(cache.total_bytes)}；浏览媒体按任务隔离，清理不会影响登录与长期数据`, "settings", statusPill(formatBytes(cache.total_bytes), num(cache.total_bytes) > 0 ? "violet" : "green"))}
    <div class="cache-bucket-grid">${buckets.map(([key, item]) => `<div class="cache-bucket"><span>${icon(key === "images" ? "sun" : key === "videos" ? "video" : key === "search" ? "search" : "user")}</span><div><small>${esc(item.label || key)}</small><strong>${formatBytes(item.bytes)}</strong></div></div>`).join("") || `<div class="empty-inline">当前没有可清理的临时文件</div>`}</div>
    <div class="cache-protection"><strong>${icon("shield")}始终保留</strong><div>${protectedItems.map((item) => `<span>${icon("check")}${esc(item)}</span>`).join("")}</div></div>
    <div class="cache-actions"><div><strong>普通清理</strong><span>清除临时图片、视频与搜索缓存，保留当前登录二维码。</span></div><button class="button soft" data-action="cache-clean-normal" type="button">${icon("refresh")}普通清理</button><div><strong>深度清理</strong><span>额外清理过期二维码等一次性文件，仍不会删除 Cookie、记忆或数据库。</span></div><button class="button danger" data-action="cache-clean-deep" type="button">${icon("lightning")}深度清理</button></div>
  </section>`;
}

function renderBasics() {
  const assigned = new Set(Object.values(PAGE_KEYS).flat());
  const allEntries = Object.entries(state.schema).filter(([key, field]) => !assigned.has(key) && !field.deprecated);
  const query = state.settingsSearch.trim().toLowerCase();
  const filtered = allEntries.filter(([key, field]) => {
    const group = basicGroupFor(key, field);
    if (!group) return false;
    if (!query) return true;
    return `${key} ${field.description || ""} ${field.hint || ""}`.toLowerCase().includes(query);
  });
  const groups = Object.fromEntries(BASIC_GROUP_ORDER.map((name) => [name, []]));
  filtered.forEach(([key, field]) => {
    const group = basicGroupFor(key, field);
    if (group) groups[group].push(key);
  });
  Object.entries(groups).forEach(([name, keys]) => {
    const preferred = BASIC_KEY_ORDER[name] || [];
    keys.sort((left, right) => {
      const leftIndex = preferred.indexOf(left);
      const rightIndex = preferred.indexOf(right);
      if (leftIndex < 0 && rightIndex < 0) return 0;
      if (leftIndex < 0) return 1;
      if (rightIndex < 0) return -1;
      return leftIndex - rightIndex;
    });
  });
  return `${pageHead("FOUNDATION", "基础设置", "这里只保留完成初始化后很少需要调整的人设、模型和高级能力；常用行为已拆到对应页面。")}
    ${renderCacheCard()}
    <section class="settings-search card"><span>${icon("search")}</span><input id="settings-search" type="search" value="${esc(state.settingsSearch)}" placeholder="搜索配置名称、说明或 KEY" aria-label="搜索基础设置" />${state.settingsSearch ? `<button data-action="clear-settings-search" type="button">清除</button>` : ""}</section>
    <div class="settings-summary"><span>共 ${allEntries.length} 项长期配置</span><span>当前显示 ${filtered.length} 项</span><span>${state.dirtyKeys.size} 项待保存</span></div>
    <div class="accordion-list">${BASIC_GROUP_ORDER.map((name, index) => {
      const keys = groups[name];
      if (!keys.length) return "";
      const iconName = { "人设与模型": "heart", "性格演化": "star", "Embedding 与记忆": "memory-card", "视频分析": "video", "图片识别": "sun", "图片生成": "sun", "联网搜索": "search", "总结": "calendar", "Cookie 与系统": "settings", "高级接口": "controller" }[name] || "settings";
      const evolutionToggle = name === "性格演化" && hasKey("ENABLE_PERSONALITY_EVOLUTION") ? `<div class="settings-inline-toggle"><div><strong>旧版每日性格演化</strong><small>实验性功能，建议先关闭并积累几天真实反馈；已有数据会保留。</small></div>${renderControl("ENABLE_PERSONALITY_EVOLUTION", state.schema.ENABLE_PERSONALITY_EVOLUTION)}</div>` : "";
      const evolutionKeys = name === "性格演化" ? keys.filter((key) => key !== "ENABLE_PERSONALITY_EVOLUTION") : keys;
      return `<details class="settings-group card" ${query || index < 2 ? "open" : ""}><summary><span class="section-icon">${icon(iconName)}</span><div><strong>${esc(name)}</strong><small>${evolutionKeys.length + (evolutionToggle ? 1 : 0)} 项配置</small></div>${icon("arrow-right")}</summary><div class="settings-group-body"><div class="settings-group-inner">${evolutionToggle}${renderFields(evolutionKeys)}</div></div></details>`;
    }).join("") || `<div class="card empty-search">${icon("search")}<strong>没有匹配的配置</strong><span>换一个关键词试试。</span></div>`}</div>`;
}

function bindConfigControls(root = content) {
  root.querySelectorAll("[data-config-key]").forEach((control) => {
    const key = control.dataset.configKey;
    const field = state.schema[key] || {};
    const eventName = control.type === "range" ? "input" : "change";
    control.addEventListener(eventName, () => {
      let value;
      if (field.type === "bool") value = control.checked;
      else if (control.dataset.hourConfig) {
        value = control.value ? Number.parseInt(control.value.split(":")[0], 10) : "";
        if (value !== "" && Number.isFinite(value)) control.value = `${String(value).padStart(2, "0")}:00`;
      } else if (field.type === "list") value = control.value.split(/[\n,，]/).map((item) => item.trim()).filter(Boolean);
      else if (field.type === "int") value = control.value === "" ? "" : Number.parseInt(control.value, 10);
      else if (field.type === "float") value = control.value === "" ? "" : Number.parseFloat(control.value);
      else value = control.value;
      if (control.dataset.rangeBound) {
        const row = control.closest(".admin-range-row");
        const inputs = row ? row.querySelectorAll("input[data-config-key]") : [];
        const minInput = inputs[0];
        const maxInput = inputs[1];
        if (minInput && maxInput) {
          if (control.dataset.rangeBound === "min" && num(minInput.value) > num(maxInput.value)) { maxInput.value = minInput.value; setDraft(maxInput.dataset.configKey, num(maxInput.value)); }
          if (control.dataset.rangeBound === "max" && num(maxInput.value) < num(minInput.value)) { minInput.value = maxInput.value; setDraft(minInput.dataset.configKey, num(minInput.value)); }
        }
      }
      setDraft(key, value);
      if (control.id === "activity-slider") {
        const sliderValue = clamp(num(value), 0, 100);
        const track = control.closest(".activity-track");
        track?.style.setProperty("--activity", `${sliderValue}%`);
        control.closest(".activity-panel")?.classList.toggle("is-max", sliderValue >= 100);
        const valueNode = root.querySelector("#activity-value") || content.querySelector("#activity-value");
        const labelNode = root.querySelector("#activity-label") || content.querySelector("#activity-label");
        if (valueNode) valueNode.textContent = sliderValue;
        if (labelNode) labelNode.textContent = `${activityLabel(sliderValue)}状态 · 事件更频繁，活动时间也更长`;
      }
      if (control.classList.contains("behavior-range")) {
        control.style.setProperty("--score", `${clamp(num(value), 0, 10) * 10}%`);
        const output = root.querySelector(`#score-${key}`) || content.querySelector(`#score-${key}`);
        if (output) output.textContent = `${clamp(num(value), 0, 10)} 分`;
      }
      const capabilityCard = control.closest("[data-capability-card]");
      if (field.type === "bool" && capabilityCard) capabilityCard.classList.toggle("is-enabled", Boolean(value));
    });
  });
}

function formatMinute(minute) {
  const value = ((Math.round(Number(minute) / 15) * 15) % 1440 + 1440) % 1440;
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
}

function isAwakeMinuteFrontend(minute) {
  const start = num(currentValue("SLEEP_START"), 2) * 60;
  const end = num(currentValue("SLEEP_END"), 8) * 60;
  const value = ((Math.round(Number(minute)) % 1440) + 1440) % 1440;
  if (start === end) return true;
  const sleeping = start < end ? value >= start && value < end : value >= start || value < end;
  return !sleeping;
}

function windowIsAwakeFrontend(start, duration) {
  return Array.from({ length: Math.floor(Number(duration) / 15) + 1 }, (_, index) => (Number(start) + index * 15) % 1440)
    .every((minute) => isAwakeMinuteFrontend(minute));
}

function bindRingInteractions() {
  content.querySelectorAll(".ring-event-hit").forEach((node) => {
    const activate = () => setActiveScheduleEvent(num(node.dataset.segmentIndex));
    node.addEventListener("click", activate);
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activate();
      }
    });
  });
  content.querySelector(".ring-center")?.addEventListener("click", () => {
    state.selectedScheduleIndex = -1;
    renderCurrentPage();
  });
}

function bindStepperControls(root = content) {
  root.querySelectorAll("[data-step-key]").forEach((buttonNode) => buttonNode.addEventListener("click", () => {
    const key = buttonNode.dataset.stepKey;
    const scope = buttonNode.closest(".admin-range-row") || root;
    const input = scope.querySelector(`[data-config-key="${key}"]`) || content.querySelector(`[data-config-key="${key}"]`);
    if (!input) return;
    const field = state.schema[key] || {};
    const step = num(input.dataset.step || input.step, field.type === "float" ? 0.1 : 1);
    const min = num(input.dataset.min || input.min, -999999);
    const max = num(input.dataset.max || input.max, 999999);
    const next = clamp(num(input.value) + num(buttonNode.dataset.stepDir) * step, min, max);
    input.value = field.type === "int" ? String(Math.round(next)) : String(Math.round(next * 1000) / 1000);
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }));
}

function bindContent() {
  bindConfigControls();
  if (state.currentPage === "autonomy") bindRingInteractions();
  content.querySelectorAll("[data-action]").forEach((node) => node.addEventListener("click", () => handleAction(node.dataset.action, node)));
  content.querySelectorAll("[data-page-target]").forEach((node) => node.addEventListener("click", () => navigate(node.dataset.pageTarget)));
  content.querySelectorAll("[data-plan-mode]").forEach((node) => node.addEventListener("click", () => {
    const autonomous = node.dataset.planMode === "autonomous";
    if (Boolean(currentValue("ENABLE_AUTONOMOUS_DAILY_PLAN")) === autonomous) return;
    setDraft("ENABLE_AUTONOMOUS_DAILY_PLAN", autonomous);
    const stage = content.querySelector(".plan-template-stage");
    stage?.classList.add("is-switching");
    const switchNode = content.querySelector(".plan-mode-switch");
    switchNode?.classList.toggle("is-autonomous", autonomous);
    switchNode?.classList.toggle("is-fixed", !autonomous);
    content.querySelectorAll("[data-plan-mode]").forEach((buttonNode) => {
      const active = (buttonNode.dataset.planMode === "autonomous") === autonomous;
      buttonNode.classList.toggle("is-active", active);
      buttonNode.setAttribute("aria-selected", String(active));
    });
    requestAnimationFrame(() => requestAnimationFrame(() => {
      stage?.classList.toggle("show-autonomous", autonomous);
      stage?.classList.toggle("show-fixed", !autonomous);
      content.querySelectorAll("[data-plan-template]").forEach((template) => {
        const active = (template.dataset.planTemplate === "autonomous") === autonomous;
        template.classList.toggle("is-active", active);
        template.setAttribute("aria-hidden", String(!active));
        template.inert = !active;
      });
      window.setTimeout(() => stage?.classList.remove("is-switching"), 320);
    }));
  }));
  content.querySelectorAll("[data-capability-open]").forEach((node) => node.addEventListener("click", () => openAutonomyDrawer(node.dataset.capabilityOpen)));
  content.querySelectorAll("[data-segment-index]").forEach((node) => {
    const activate = () => setActiveScheduleEvent(num(node.dataset.segmentIndex));
    node.addEventListener("click", activate);
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activate();
      }
    });
  });
  bindStepperControls(content);
  content.querySelectorAll("[data-time-list]").forEach((list) => {
    const key = list.dataset.timeList;
    const commit = () => setDraft(key, [...list.querySelectorAll("[data-time-index]")].map((node) => node.value).filter(Boolean));
    list.querySelectorAll("[data-time-index]").forEach((node) => node.addEventListener("change", commit));
    list.querySelectorAll("[data-time-remove]").forEach((node) => node.addEventListener("click", () => {
      const values = [...list.querySelectorAll("[data-time-index]")].map((input) => input.value).filter(Boolean);
      values.splice(num(node.dataset.timeRemove), 1);
      setDraft(key, values);
      renderCurrentPage();
    }));
  });
  content.querySelectorAll("[data-time-add]").forEach((node) => node.addEventListener("click", () => {
    const key = node.dataset.timeAdd;
    const values = Array.isArray(currentValue(key)) ? [...currentValue(key)] : [];
    values.push("12:00");
    setDraft(key, values);
    renderCurrentPage();
  }));
  const search = content.querySelector("#settings-search");
  if (search) search.addEventListener("input", () => {
    state.settingsSearch = search.value;
    const position = search.selectionStart;
    content.innerHTML = renderBasics();
    bindContent();
    const next = content.querySelector("#settings-search");
    next?.focus();
    next?.setSelectionRange(position, position);
  });
  if (state.currentPage === "account" && !state.account?.logged_in) generateQr();
}

function setActiveScheduleEvent(index) {
  const events = state.schedule.events || [];
  if (!events[index]) return;
  state.selectedScheduleIndex = index;
  content.querySelectorAll(".ring-event, .event-row").forEach((node) => {
    const nodeIndex = node.classList.contains("ring-event") ? node.dataset.ringIndex : node.dataset.segmentIndex;
    node.classList.toggle("is-active", num(nodeIndex) === index);
  });
  content.querySelectorAll(".ring-event-hit").forEach((node) => node.setAttribute("aria-pressed", String(num(node.dataset.segmentIndex) === index)));
  const selectedContainer = content.querySelector("#selected-event");
  if (selectedContainer) selectedContainer.outerHTML = renderSelectedEvent(events);
  const center = content.querySelector(".ring-center");
  if (center) {
    const event = state.scheduleDraft.events?.[index] || events[index];
    const meta = eventPhaseMeta(event);
    center.innerHTML = `<span>已选择事件</span><strong>${esc(eventWindow(event) ? `${event.start_time}–${event.end_time}` : event.time)}</strong><b>${esc(event.label)}</b><small>${meta.label} · ${meta.detail}</small>`;
  }
  const style = EVENT_STYLES[events[index].kind] || EVENT_STYLES.proactive;
  const glow = content.querySelector(".ring-glow");
  glow?.style.setProperty("--ring-a", style.gradient[0]);
  glow?.style.setProperty("--ring-b", style.gradient[1]);
}

async function handleAction(action, source = null) {
  if (action === "refresh") return refreshCurrent();
  if (action === "save") return saveDraft();
  if (action === "discard") return discardDraft();
  if (action === "refresh-memory") return refreshAndRender("memory", "记忆数据已刷新");
  if (action === "refresh-security") return refreshAndRender("security", "安全审计已刷新");
  if (action === "open-tool-picker") return openToolPicker();
  if (action === "refresh-account") return refreshAndRender("account", state.account?.logged_in ? "账号连接正常" : "账号仍未连接");
  if (action === "clear-settings-search") { state.settingsSearch = ""; renderCurrentPage(); return; }
  if (action === "toggle-secret") {
    const input = source?.closest(".input-with-action")?.querySelector("[data-config-key]");
    if (input) input.type = input.type === "password" ? "text" : "password";
    return;
  }
  if (action === "generate-qr") return generateQr();
  if (action === "regenerate-schedule") {
    const ok = await confirmModal("重新生成今日计划", "这会清空今天尚未完成的日程并立即根据当前活跃度与安全上限重新生成。", "重新生成");
    if (!ok) return;
    const regenerated = await apiPost("schedule/regenerate", {});
    state.schedule = { ...state.schedule, ...(regenerated || {}) };
    state.scheduleStats = await apiGet("schedule/stats") || {};
    renderCurrentPage();
    const plan = regenerated?.autonomous_plan;
    if (plan?.generation_status === "error") {
      toast("今天不新增自动事件", `${plan.model_error || "计划模型暂时没有返回有效内容。"} 可稍后重新生成。`, "error");
    } else {
      toast("今日计划已更新", "新计划已经过睡眠区间、最小间隔与安全上限校验");
    }
    return;
  }
  if (action === "cache-clean-normal" || action === "cache-clean-deep") {
    const deep = action === "cache-clean-deep";
    if (deep) {
      const ok = await confirmModal("深度清理临时文件", "将额外清理过期二维码等一次性文件；Cookie、登录状态、记忆、画像、好感度和数据库不会被删除。", "确认深度清理", true);
      if (!ok) return;
    }
    try {
      const result = await apiPost("cache/purge", { mode: deep ? "deep" : "normal" });
      state.cache = await apiGet("cache/stats") || {};
      renderCurrentPage();
      toast(deep ? "深度清理完成" : "普通清理完成", `已释放 ${formatBytes(result?.removed_bytes || 0)}，当前占用 ${formatBytes(state.cache.total_bytes)}`);
    } catch (error) {
      toast("缓存清理失败", error.message || "请检查插件日志", "error");
    }
    return;
  }
  if (action === "purge-memory") {
    const ok = await confirmModal("清理过期记忆", "只删除超过保留期限的老化记录，不重置用户画像和好感度。", "确认清理");
    if (!ok) return;
    const result = await apiPost("memory/purge", {});
    await refreshPageData("memory");
    renderCurrentPage();
    toast("清理完成", result?.removed ? `已移除 ${result.removed} 条记录` : "过期记忆已处理");
    return;
  }
  if (action === "logout") {
    const ok = await confirmModal("退出 B站账号", "退出后会清空插件保存的登录凭据，需要重新扫码才能继续自动互动。", "退出账号", true);
    if (!ok) return;
    await apiPost("account/logout", {});
    state.account = await apiGet("account/info");
    renderCurrentPage();
    toast("已退出账号");
  }
}

async function refreshAndRender(page, message) {
  try {
    await refreshPageData(page);
    renderSidebar();
    renderCurrentPage();
    toast(message);
  } catch (error) {
    toast("读取失败", error.message || "请稍后重试", "error");
  }
}

async function refreshCurrent() {
  try {
    if (state.mode === "extension") {
      const extension = activeExtension();
      if (extension) await loadExtensionPage(extension.id, state.extensionPage);
      return;
    }
    await refreshPageData(state.currentPage);
    renderSidebar();
    renderCurrentPage();
    toast("状态已刷新", "运行数据已同步");
  } catch (error) {
    toast("刷新失败", error.message || "请稍后重试", "error");
  }
}

async function saveDraft() {
  const keys = [...state.dirtyKeys];
  const scheduleNeedsSave = state.scheduleDirty;
  if ((!keys.length && !scheduleNeedsSave) || state.isSaving) return;
  const body = Object.fromEntries(keys.map((key) => [key, state.draft[key]]));
  const refreshSchedule = keys.some((key) => SCHEDULE_REGEN_KEYS.has(key));
  state.isSaving = true;
  updateSaveDock();
  try {
    if (keys.length) {
      await apiPost("config", body);
      Object.assign(state.config, body);
      Object.assign(mock.config, body);
      state.dirtyKeys.clear();
      state.draft = structuredClone(state.config);
    }

    let scheduleError = null;
    let regeneratedPlan = null;
    if (refreshSchedule) {
      try {
        const regenerated = await apiPost("schedule/regenerate", {});
        state.schedule = { ...state.schedule, ...(regenerated || {}) };
        state.scheduleOriginal = structuredClone(state.schedule);
        state.scheduleDraft = structuredClone(state.schedule);
        regeneratedPlan = regenerated?.autonomous_plan || null;
        state.scheduleStats = await apiGet("schedule/stats") || {};
        const eventCount = (state.schedule.events || []).length;
        state.selectedScheduleIndex = eventCount ? clamp(state.selectedScheduleIndex, -1, eventCount - 1) : -1;
      } catch (error) {
        scheduleError = error;
      }
    }
    if (!scheduleError && scheduleNeedsSave) {
      try {
        const result = await apiPost("schedule/override", { events: state.scheduleDraft.events || [] });
        state.schedule = result || state.scheduleDraft;
        state.scheduleOriginal = structuredClone(state.schedule);
        state.scheduleDraft = structuredClone(state.schedule);
        state.scheduleDirty = false;
        state.scheduleStats = await apiGet("schedule/stats") || {};
      } catch (error) {
        scheduleError = error;
      }
    }

    state.isSaving = false;
    updateSaveDock();
    renderSidebar();
    renderCurrentPage();
    if (scheduleError) {
      toast("保存未完成", scheduleError.message || "日程修改未写入，请检查时间间隔", "error");
    } else if (regeneratedPlan?.generation_status === "error") {
      toast("配置已保存，并启用安全计划", `${regeneratedPlan.model_error || "计划模型暂时没有返回有效内容。"} 系统会按重试间隔再次尝试。`, "error");
    } else if (refreshSchedule || scheduleNeedsSave) {
      toast("配置与今日计划已更新", `已保存 ${keys.length + (scheduleNeedsSave ? 1 : 0)} 项修改`);
    } else {
      toast("配置已保存", `已写入 ${keys.length} 项设置`);
    }
  } catch (error) {
    state.isSaving = false;
    updateSaveDock();
    toast("保存失败", error.message || "请检查输入值", "error");
  }
}

function discardDraft() {
  state.draft = structuredClone(state.config);
  state.dirtyKeys.clear();
  state.schedule = structuredClone(state.scheduleOriginal);
  state.scheduleDraft = structuredClone(state.scheduleOriginal);
  state.scheduleDirty = false;
  updateSaveDock();
  renderSidebar();
  renderCurrentPage();
  toast("已放弃修改", "配置与事件环已恢复为上次保存状态");
}


function toolOriginLabel(tool) {
  return tool.origin_name || ({ builtin: "AstrBot Core", plugin: "插件工具", mcp: "MCP 服务", bilibot: "B站端私信回复工具" }[tool.origin] || "其他工具");
}

function openToolPicker() {
  state.toolSearch = "";
  state.toolPickerSelection = new Set(Array.isArray(currentValue("BILI_TOOL_ALLOWLIST")) ? currentValue("BILI_TOOL_ALLOWLIST") : []);
  renderToolPickerModal();
}

function renderToolPickerModal() {
  const compatibleTools = state.availableTools.filter((tool) => tool.compatible && tool.active !== false);
  const unavailableTools = state.availableTools.filter((tool) => !tool.compatible || tool.active === false);
  const groups = new Map();
  compatibleTools.forEach((tool) => {
    const group = toolOriginLabel(tool);
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(tool);
  });
  const optionHtml = (tool) => {
    const enabled = tool.compatible && tool.active !== false;
    const checked = enabled && state.toolPickerSelection.has(tool.name);
    const haystack = `${tool.label || ""} ${tool.name || ""} ${tool.description || ""} ${tool.origin_name || ""}`.toLowerCase();
    return `<label class="tool-option ${checked ? "is-selected" : ""} ${enabled ? "" : "is-disabled"}" data-tool-option data-tool-search="${esc(haystack)}"><input data-tool-name="${esc(tool.name)}" type="checkbox" ${checked ? "checked" : ""} ${enabled ? "" : "disabled"}/><span class="tool-check">${icon(checked ? "unlock" : "lock")}</span><span class="tool-option-copy"><strong>${esc(tool.label || tool.name)}</strong><p>${esc(tool.description || "暂无说明")}</p><small>${esc(tool.reason || (enabled ? "只读安全能力" : "未适配"))}</small></span><span class="tool-state">${enabled ? (checked ? "已选择" : "可选择") : "不可用"}</span></label>`;
  };
  const unavailableSummary = unavailableTools.length ? `<details class="tool-unavailable" data-tool-unavailable><summary><span>${icon("lock")}其他已注册工具（不可加入 B站只读白名单）</span><b>${unavailableTools.length} 项</b></summary><p>这些工具来自 AstrBot 内置工具、其他插件或 MCP；它们不是 B站只读适配器，因此不会进入白名单，也不会被 B站评论/私信上下文调用。</p><div class="tool-unavailable-list">${unavailableTools.slice(0, 80).map((tool) => `<span><strong>${esc(tool.label || tool.name)}</strong><small>${esc(tool.reason || "未提供 B站只读适配器")}</small></span>`).join("")}</div></details>` : "";
  modalRoot.innerHTML = `<div class="modal-backdrop tool-picker-backdrop" data-modal-backdrop><div class="modal tool-modal" role="dialog" aria-modal="true" aria-labelledby="tool-modal-title"><div class="tool-modal-head"><span class="modal-icon">${icon("controller")}</span><div><h2 id="tool-modal-title">选择 B站端私信回复工具</h2><p>仅当B站私信用户提出查询请求时，私信回复模型才可调用已勾选工具；这里不控制 QQ/AstrBot 聊天工具。其他注册工具仅作说明，不会出现在可选白名单中。</p></div><button class="modal-close" data-tool-close type="button" aria-label="关闭">×</button></div><label class="tool-search">${icon("search")}<input id="tool-search-input" type="search" value="" placeholder="搜索 B站只读工具" /></label><div class="tool-modal-list">${[...groups.entries()].map(([group, items], index) => `<details class="tool-group" data-tool-group ${index < 2 ? "open" : ""}><summary><div><strong>${esc(group)}</strong><span data-group-count>${items.length} 项</span></div>${icon("arrow-right")}</summary><div class="tool-group-body">${items.map(optionHtml).join("")}</div></details>`).join("") || `<div class="empty-search">${icon("search")}<strong>当前没有可用的 B站只读适配器</strong><span>这不影响普通评论和私信处理。</span></div>`}<div class="empty-search tool-search-empty" hidden>${icon("search")}<strong>没有匹配工具</strong><span>换一个关键词试试。</span></div>${unavailableSummary}</div><div class="tool-modal-actions"><span>已选择 <b data-tool-selected-count>${state.toolPickerSelection.size}</b> 项</span><div><button class="button soft" data-tool-close type="button">取消</button><button class="button primary" data-tool-confirm type="button">${icon("save")}确认选择</button></div></div></div></div>`;
  const backdrop = modalRoot.querySelector(".tool-picker-backdrop");
  const close = () => {
    backdrop?.classList.add("is-closing");
    window.setTimeout(() => { modalRoot.innerHTML = ""; }, 190);
  };
  modalRoot.querySelectorAll("[data-tool-close]").forEach((node) => node.addEventListener("click", close));
  modalRoot.querySelector("[data-modal-backdrop]")?.addEventListener("click", (event) => { if (event.target === event.currentTarget) close(); });
  const updateSelectedCount = () => {
    const count = modalRoot.querySelector("[data-tool-selected-count]");
    if (count) count.textContent = String(state.toolPickerSelection.size);
  };
  modalRoot.querySelectorAll("[data-tool-name]").forEach((node) => node.addEventListener("change", () => {
    if (node.checked) state.toolPickerSelection.add(node.dataset.toolName);
    else state.toolPickerSelection.delete(node.dataset.toolName);
    const option = node.closest("[data-tool-option]");
    option?.classList.toggle("is-selected", node.checked);
    const check = option?.querySelector(".tool-check");
    if (check) { check.classList.remove("is-changing"); void check.offsetWidth; check.innerHTML = icon(node.checked ? "unlock" : "lock"); check.classList.add("is-changing"); }
    const status = option?.querySelector(".tool-state");
    if (status) status.textContent = node.checked ? "已选择" : "可选择";
    updateSelectedCount();
  }));
  const search = modalRoot.querySelector("#tool-search-input");
  search?.addEventListener("input", () => {
    const query = search.value.trim().toLowerCase();
    state.toolSearch = search.value;
    let visibleTotal = 0;
    modalRoot.querySelectorAll("[data-tool-group]").forEach((group) => {
      let visible = 0;
      group.querySelectorAll("[data-tool-option]").forEach((option) => {
        const match = !query || String(option.dataset.toolSearch || "").includes(query);
        option.hidden = !match;
        if (match) visible += 1;
      });
      group.hidden = visible === 0;
      if (query && visible) group.open = true;
      const count = group.querySelector("[data-group-count]");
      if (count) count.textContent = `${visible} 项`;
      visibleTotal += visible;
    });
    const empty = modalRoot.querySelector(".tool-search-empty");
    if (empty) empty.hidden = visibleTotal !== 0;
  });
  modalRoot.querySelector("[data-tool-confirm]")?.addEventListener("click", () => {
    const valid = state.availableTools.filter((tool) => tool.compatible && tool.active !== false && state.toolPickerSelection.has(tool.name)).map((tool) => tool.name);
    setDraft("BILI_TOOL_ALLOWLIST", valid);
    close();
    window.setTimeout(refreshToolSummary, 205);
  });
  requestAnimationFrame(() => backdrop?.classList.add("is-visible"));
  search?.focus();
}

function confirmModal(title, message, confirmText, danger = false) {
  return new Promise((resolve) => {
    modalRoot.innerHTML = `<div class="modal-backdrop" data-modal-backdrop><div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><span class="modal-icon">${icon(danger ? "lightning" : "shield")}</span><h2 id="modal-title">${esc(title)}</h2><p>${esc(message)}</p><div class="modal-actions"><button class="button soft" data-modal="cancel" type="button">取消</button><button class="button ${danger ? "danger" : "primary"}" data-modal="confirm" type="button">${esc(confirmText)}</button></div></div></div>`;
    const close = (result) => { modalRoot.innerHTML = ""; resolve(result); };
    const cancel = modalRoot.querySelector('[data-modal="cancel"]');
    cancel?.focus();
    cancel?.addEventListener("click", () => close(false));
    modalRoot.querySelector('[data-modal="confirm"]')?.addEventListener("click", () => close(true));
    modalRoot.querySelector("[data-modal-backdrop]")?.addEventListener("click", (event) => { if (event.target === event.currentTarget) close(false); });
  });
}

async function generateQr() {
  const box = content.querySelector("#qr-box");
  const status = content.querySelector("#qr-status");
  if (!box || !status) return;
  stopQrPoll();
  box.className = "qr-box is-loading";
  box.innerHTML = `<div class="qr-loading"><i></i><span>正在申请二维码…</span></div>`;
  status.textContent = "正在连接 B站登录服务";
  try {
    const data = await apiGet("account/qr/generate");
    box.className = "qr-box";
    box.innerHTML = data.image ? `<img src="${esc(data.image)}" alt="B站登录二维码" />` : `<div class="qr-preview">${icon("user")}<strong>预览模式</strong><span>真实页面会显示扫码二维码</span></div>`;
    status.textContent = "等待扫码确认";
    pollQr(data.key);
  } catch (error) {
    const message = error.message || "二维码生成失败";
    box.className = "qr-box has-error";
    box.innerHTML = `<div class="qr-error">${icon("shield")}<strong>二维码生成失败</strong><span>${esc(message)}</span><button class="button soft" data-action="generate-qr" type="button">${icon("refresh")}重试</button></div>`;
    status.textContent = "登录服务不可用";
    box.querySelector("[data-action]")?.addEventListener("click", generateQr);
    toast("二维码生成失败", message, "error");
  }
}

function pollQr(key) {
  if (!key || isPreview) return;
  state.qrPollTimer = setInterval(async () => {
    try {
      const result = await apiGet("account/qr/poll", { key });
      const status = content.querySelector("#qr-status");
      if (!status) return stopQrPoll();
      if (result.status === "success") {
        stopQrPoll();
        status.textContent = "登录成功，正在同步账号";
        state.account = await apiGet("account/info");
        renderCurrentPage();
        toast("账号连接成功", "B站账号与后台任务已同步");
      } else if (result.status === "scanned") status.textContent = "已扫码，请在 B站客户端确认";
      else if (result.status === "expired") {
        stopQrPoll();
        status.textContent = "二维码已过期，请重新生成";
        const box = content.querySelector("#qr-box");
        if (box) box.innerHTML = `<div class="qr-error">${icon("time")}<strong>二维码已过期</strong><span>请生成新的二维码后重新扫码。</span></div>`;
      } else status.textContent = result.message || "等待扫码确认";
    } catch (error) {
      stopQrPoll();
      const box = content.querySelector("#qr-box");
      if (box) {
        box.className = "qr-box has-error";
        box.innerHTML = `<div class="qr-error">${icon("shield")}<strong>登录状态读取失败</strong><span>${esc(error.message || "请重新生成二维码")}</span></div>`;
      }
      toast("登录状态获取失败", error.message || "请重新生成二维码", "error");
    }
  }, 2200);
}

function stopQrPoll() {
  if (state.qrPollTimer) {
    clearInterval(state.qrPollTimer);
    state.qrPollTimer = null;
  }
}

function init() {
  const mobileMenu = document.querySelector("#mobile-menu");
  if (mobileMenu) mobileMenu.innerHTML = icon("menu");
  mobileMenu?.addEventListener("click", openMobileNav);
  document.querySelector("#sidebar-scrim")?.addEventListener("click", closeMobileNav);
  window.addEventListener("pointermove", (event) => {
    if (state.mode !== "extension" || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    app.style.setProperty("--creator-pointer-x", `${event.clientX}px`);
    app.style.setProperty("--creator-pointer-y", `${event.clientY}px`);
  }, { passive: true });
  window.addEventListener("beforeunload", (event) => {
    if (state.dirtyKeys.size || state.scheduleDirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  loadBase().then(() => {
    app.classList.remove("is-booting");
    renderCurrentPage();
    updateSaveDock();
  }).catch((error) => {
    content.innerHTML = renderErrorState("无法加载 BiliBot 控制中心", error.message || "请检查 AstrBot 页面权限");
    bindContent();
    toast("初始化失败", error.message || "请检查插件日志", "error");
  });
}

init();
