import { icon } from "./icons.js";

const bridge = window.AstrBotPluginPage || null;
const isPreview = location.search.includes("preview=1") || !bridge;
const app = document.querySelector("#app");
const content = document.querySelector("#content");
const sidebar = document.querySelector("#sidebar");
const saveDock = document.querySelector("#save-dock");
const toastRegion = document.querySelector("#toast-region");
const modalRoot = document.querySelector("#modal-root");

const state = {
  currentPage: "overview",
  schema: {},
  config: {},
  draft: {},
  dirtyKeys: new Set(),
  stats: {},
  persona: {},
  account: null,
  schedule: { events: [] },
  scheduleStats: {},
  memory: {},
  profiles: [],
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
};

const NAV_ITEMS = [
  ["overview", "house", "总览", "健康、配额与运行监控"],
  ["autonomy", "clock", "自主与作息", "活跃度、彩虹日程与硬上限"],
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
    "ENABLE_PRIVATE_MESSAGES", "PRIVATE_MESSAGE_REPLY_SCOPE", "PRIVATE_MESSAGE_AUTO_REPLY",
    "PRIVATE_MESSAGE_AUTO_WATCH_VIDEO", "PRIVATE_MESSAGE_BILI_SEARCH_ENABLED", "PRIVATE_MESSAGE_BILI_SEARCH_LIMIT",
    "BILI_PRIVATE_SHARE_TOOL_ENABLED", "BILI_PRIVATE_SHARE_COOLDOWN", "PRIVATE_MESSAGE_POLL_INTERVAL",
    "PRIVATE_MESSAGE_IDLE_POLL_INTERVAL", "PRIVATE_MESSAGE_ACTIVE_WINDOW", "PRIVATE_MESSAGE_REPLY_WHITELIST_UIDS",
    "CUSTOM_PRIVATE_MESSAGE_INSTRUCTION", "PRIVATE_MESSAGE_MAX_PER_POLL", "PRIVATE_MESSAGE_MAX_MESSAGE_AGE",
    "ENABLE_LIVE_DANMAKU_REPLY", "LIVE_DANMAKU_ROOM_ID", "LIVE_DANMAKU_POLL_INTERVAL",
    "LIVE_DANMAKU_REPLY_COOLDOWN", "LIVE_DANMAKU_MAX_PER_MINUTE", "LIVE_DANMAKU_REPLY_MAX_LENGTH",
    "CUSTOM_LIVE_DANMAKU_INSTRUCTION", "ENABLE_BILI_SHARE_PARSE", "BILI_SHARE_PARSE_AUTO_TRIGGER_ENABLED",
    "BILI_SHARE_PARSE_MANUAL_TRIGGER_ENABLED", "BILI_SHARE_PARSE_LLM_TRIGGER_ENABLED", "BILI_SHARE_PENDING_MAX_AGE",
    "BILI_SHARE_PARSE_SEND_VIDEO", "BILI_SHARE_PARSE_SEGMENT_SECONDS", "BILI_SHARE_PARSE_MAX_SEGMENTS",
    "BILI_SHARE_PARSE_MAX_VIDEO_MB", "BILI_SHARE_PARSE_VIDEO_MAX_HEIGHT", "BILI_SHARE_PARSE_COOLDOWN",
  ],
  autonomy: [
    "ENABLE_AUTONOMOUS_DAILY_PLAN", "AUTONOMOUS_ACTIVITY_LEVEL", "AUTONOMOUS_PLAN_PROMPT",
    "AUTONOMOUS_REPLY_DAILY_LIMIT", "AUTONOMOUS_PRIVATE_DAILY_LIMIT", "AUTONOMOUS_DYNAMIC_DAILY_LIMIT",
    "AUTONOMOUS_PROACTIVE_DAILY_LIMIT", "AUTONOMOUS_MIN_ACTION_GAP_MINUTES", "SLEEP_START", "SLEEP_END",
    "ENABLE_PROACTIVE", "PROACTIVE_VIDEO_COUNT", "PROACTIVE_DAILY_LIMIT", "PROACTIVE_TIMES_COUNT",
    "PROACTIVE_COMMENT_COUNT", "PROACTIVE_FOLLOW_UIDS", "PROACTIVE_SEARCH_QUERY_PROMPT", "PROACTIVE_TASTE_WINDOW_DAYS",
    "PROACTIVE_VIDEO_POOLS", "ENABLE_PROACTIVE_LLM_PREFILTER", "PROACTIVE_LLM_PREFILTER_MAX_REJECTS",
    "PROACTIVE_LIKE", "PROACTIVE_LIKE_MIN_SCORE", "PROACTIVE_COIN", "PROACTIVE_COIN_MIN_SCORE",
    "PROACTIVE_FAV", "PROACTIVE_FAV_MIN_SCORE", "PROACTIVE_COMMENT", "PROACTIVE_COMMENT_MIN_SCORE",
    "PROACTIVE_FOLLOW", "PROACTIVE_FOLLOW_MIN_SCORE",
    "CUSTOM_PROACTIVE_INSTRUCTION", "RECOMMEND_OWNER_DELIVERY", "RECOMMEND_OWNER_MIN_SCORE",
    "RECOMMEND_OWNER_DAILY_LIMIT", "CUSTOM_RECOMMEND_INSTRUCTION", "SPECIAL_FOLLOW_ENABLED", "SPECIAL_FOLLOW_MODE",
    "SPECIAL_FOLLOW_TIMES_COUNT", "SPECIAL_FOLLOW_FIXED_TIMES", "ENABLE_BANGUMI", "BANGUMI_PROACTIVE",
    "BANGUMI_POOLS", "BANGUMI_EPISODE_COUNT", "BANGUMI_CONTINUE_SCORE", "BANGUMI_DAILY_LIMIT",
    "BANGUMI_COMMENT", "BANGUMI_AUTO_FOLLOW", "ENABLE_DYNAMIC", "DYNAMIC_TIMES_COUNT", "DYNAMIC_DAILY_COUNT",
    "DYNAMIC_TOPICS", "CUSTOM_DYNAMIC_INSTRUCTION",
    "ENABLE_DYNAMIC_WATCH", "DYNAMIC_WATCH_TIMES_COUNT", "DYNAMIC_WATCH_DAILY_LIMIT",
    "DYNAMIC_WATCH_SPECIAL_ONLY", "DYNAMIC_WATCH_INCLUDE_VIDEO_POSTS", "DYNAMIC_WATCH_INTEREST_PROMPT",
    "FIXED_REPLY_DAILY_TARGET", "FIXED_PRIVATE_DAILY_TARGET", "FIXED_PROACTIVE_TIMES",
    "FIXED_DYNAMIC_TIMES", "FIXED_DYNAMIC_WATCH_TIMES", "FIXED_BANGUMI_TIMES", "FIXED_SPECIAL_FOLLOW_TIMES",
  ],
  memory: [
    "ENABLE_AFFECTION", "ENABLE_MOOD", "AFFECTION_PROMPT_SPECIAL", "AFFECTION_PROMPT_CLOSE",
    "AFFECTION_PROMPT_FRIEND", "AFFECTION_PROMPT_NORMAL", "AFFECTION_PROMPT_STRANGER", "AFFECTION_PROMPT_COLD",
  ],
  security: [
    "BILI_TOOL_ISOLATION_ENABLED", "BILI_ALLOW_SEARCH_TOOLS", "BILI_TOOL_ALLOWLIST",
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
  "AUTONOMOUS_REPLY_DAILY_LIMIT", "AUTONOMOUS_PRIVATE_DAILY_LIMIT", "AUTONOMOUS_DYNAMIC_DAILY_LIMIT",
  "AUTONOMOUS_PROACTIVE_DAILY_LIMIT", "AUTONOMOUS_MIN_ACTION_GAP_MINUTES", "SLEEP_START", "SLEEP_END",
  "ENABLE_PROACTIVE", "PROACTIVE_VIDEO_COUNT", "PROACTIVE_DAILY_LIMIT", "PROACTIVE_TIMES_COUNT",
  "ENABLE_DYNAMIC", "DYNAMIC_TIMES_COUNT", "DYNAMIC_DAILY_COUNT",
  "ENABLE_BANGUMI", "BANGUMI_PROACTIVE", "BANGUMI_DAILY_LIMIT",
  "SPECIAL_FOLLOW_ENABLED", "SPECIAL_FOLLOW_MODE", "SPECIAL_FOLLOW_TIMES_COUNT", "SPECIAL_FOLLOW_FIXED_TIMES",
  "ENABLE_DYNAMIC_WATCH", "DYNAMIC_WATCH_TIMES_COUNT", "DYNAMIC_WATCH_DAILY_LIMIT",
  "FIXED_REPLY_DAILY_TARGET", "FIXED_PRIVATE_DAILY_TARGET", "FIXED_PROACTIVE_TIMES",
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
  ENABLE_LIVE_DANMAKU_REPLY: ["【直播弹幕·总开关】回复直播弹幕", "bool", false],
  LIVE_DANMAKU_MAX_PER_MINUTE: ["【直播弹幕】每分钟最多自动回复次数", "int", 3],
  ENABLE_BILI_SHARE_PARSE: ["【分享解析·总开关】识别B站视频分享", "bool", true],
  ENABLE_AUTONOMOUS_DAILY_PLAN: ["【自主安排】允许 Bot 根据人设与活跃度生成每日计划", "bool", true],
  AUTONOMOUS_ACTIVITY_LEVEL: ["【自主安排】今日基础活跃度（0-100）", "int", 62],
  AUTONOMOUS_PLAN_PROMPT: ["【自主安排】每日计划补充提示词", "text", "自然安排一天，低价值内容不必回复，避免短时间密集互动。"],
  AUTONOMOUS_REPLY_DAILY_LIMIT: ["【自主安排·硬上限】每日评论回复最多次数", "int", 80],
  AUTONOMOUS_PRIVATE_DAILY_LIMIT: ["【自主安排·硬上限】每日私信回复最多次数", "int", 30],
  AUTONOMOUS_DYNAMIC_DAILY_LIMIT: ["【自主安排·硬上限】每日发布动态最多次数", "int", 2],
  AUTONOMOUS_PROACTIVE_DAILY_LIMIT: ["【自主安排·硬上限】每日主动行为最多次数", "int", 4],
  AUTONOMOUS_MIN_ACTION_GAP_MINUTES: ["【自主安排·硬约束】主动事件最小间隔（分钟）", "int", 45],
  SLEEP_START: ["【系统】休眠开始时间（0-23）", "int", 2],
  SLEEP_END: ["【系统】休眠结束时间（0-23）", "int", 8],
  ENABLE_PROACTIVE: ["【主动看片·总开关】启用主动看视频与互动", "bool", true],
  PROACTIVE_DAILY_LIMIT: ["【主动看片·数量】每天最多看几个视频", "int", 5],
  PROACTIVE_TIMES_COUNT: ["【主动看片·频率】每天触发几次主动浏览", "int", 2],
  PROACTIVE_VIDEO_COUNT: ["【主动看片·数量】每次计划观看几个视频", "int", 3],
  PROACTIVE_COMMENT_COUNT: ["【主动看片·互动】每次最多评论几个视频", "int", 1],
  PROACTIVE_FOLLOW_UIDS: ["【主动看片·来源】优先关注的 UP 主 UID", "list", ["184028", "902418"]],
  PROACTIVE_SEARCH_QUERY_PROMPT: ["【主动看片·搜索】搜索词生成提示词", "text", "结合今天的心情与长期兴趣，生成自然且不过度重复的搜索词。"],
  PROACTIVE_TASTE_WINDOW_DAYS: ["【主动看片·偏好】近期兴趣窗口（天）", "int", 14],
  PROACTIVE_VIDEO_POOLS: ["【主动看片·来源】备用视频池", "list", ["BV1xx411c7mD", "BV1ab4y1Z7Qm"]],
  ENABLE_PROACTIVE_LLM_PREFILTER: ["【主动看片·筛选】启用模型预筛选", "bool", true],
  PROACTIVE_LLM_PREFILTER_MAX_REJECTS: ["【主动看片·筛选】预筛选最多拒绝次数", "int", 4],
  CUSTOM_PROACTIVE_INSTRUCTION: ["【主动行为】主动评论补充提示词", "text", "只在确实有内容可说时评论，保持自然。"],
  RECOMMEND_OWNER_DELIVERY: ["【给主人分享】允许分享有趣视频", "bool", true],
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
  FIXED_REPLY_DAILY_TARGET: ["【固定计划】每日评论回复目标数量", "int", 30],
  FIXED_PRIVATE_DAILY_TARGET: ["【固定计划】每日私信回复目标数量", "int", 10],
  FIXED_PROACTIVE_TIMES: ["【固定计划】主动浏览准确时刻", "list", ["10:30", "19:30"]],
  FIXED_DYNAMIC_TIMES: ["【固定计划】发布动态准确时刻", "list", ["18:30"]],
  FIXED_DYNAMIC_WATCH_TIMES: ["【固定计划】关注动态巡视准确时刻", "list", ["11:30", "20:30"]],
  FIXED_BANGUMI_TIMES: ["【固定计划】追番准确时刻", "list", ["21:00"]],
  FIXED_SPECIAL_FOLLOW_TIMES: ["【固定计划】特别关注巡视准确时刻", "list", ["12:00", "20:00"]],
  ENABLE_DAILY_SUMMARY: ["【总结·日总结】启用每日总结", "bool", true],
  DAILY_SUMMARY_HOUR: ["【总结·日总结】生成时间（0-23点）", "int", 3],
  ENABLE_AFFECTION: ["【功能开关】启用好感度系统", "bool", true],
  ENABLE_MOOD: ["【功能开关】启用心情系统", "bool", true],
  BILI_TOOL_ISOLATION_ENABLED: ["【安全与工具】保持 B站端与 AstrBot/QQ 工具权限隔离", "bool", true],
  BILI_ALLOW_SEARCH_TOOLS: ["【安全与工具】允许 B站私信使用只读搜索类工具", "bool", true],
  BILI_TOOL_ALLOWLIST: ["【安全与工具】B站端只读工具白名单", "list", ["bili_up_info", "bili_video_search", "web_search"]],
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
  USE_ASTRBOT_PERSONA: ["【人设】使用 AstrBot 自带人设", "bool", true],
  CUSTOM_SYSTEM_PROMPT: ["【人设】自定义系统提示词", "text", "自然、克制、有自己的兴趣和判断。"],
  ENABLE_LLM_TOOLS: ["【功能开关】启用 LLM 工具", "bool", true],
  ENABLE_PERSONALITY_EVOLUTION: ["【功能开关】启用性格演化", "bool", true],
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
    { time: "20:15", label: "主动浏览", kind: "proactive", description: "浏览视频并选择感兴趣的内容" },
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
      autonomous_plan: { rationale: "今天保持适度活跃，在晚间安排较有参与感的互动。", generated_at: "2026-08-14 08:02:11", reply_target: 50, private_target: 18 },
      events: previewEvents,
    },
    scheduleStats: { total: previewEvents.length, completed: previewCompleted, remaining: previewEvents.length - previewCompleted, next: previewNext, minimum_gap_minutes: 45 },
    memory: { total: 1248, comment: 876, private: 192, self: 180, isolation_mode: "isolated", safe_share: false },
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
      { name: "check_following_updates", label: "关注动态", description: "只读查看今天关注 UP 主的新动态与投稿", origin: "bilibot", origin_name: "BiliBot 安全适配器", active: true, compatible: true, reason: "插件内置只读安全适配器" },
      { name: "check_following_live", label: "关注直播", description: "只读查看当前正在直播的关注 UP 主", origin: "bilibot", origin_name: "BiliBot 安全适配器", active: true, compatible: true, reason: "插件内置只读安全适配器" },
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

function regenerateMockSchedule() {
  const cfg = mock.config;
  const activity = clamp(num(cfg.AUTONOMOUS_ACTIVITY_LEVEL, 55), 0, 100);
  const events = [];
  const now = new Date();
  const nowMinute = now.getHours() * 60 + now.getMinutes();
  const add = (time, label, kind, description) => events.push({ time, label, kind, description, triggered: minutesOf(time) < nowMinute });
  const proactiveCount = cfg.ENABLE_PROACTIVE ? Math.min(num(cfg.PROACTIVE_DAILY_LIMIT, 5), num(cfg.AUTONOMOUS_PROACTIVE_DAILY_LIMIT, 4), activity >= 85 ? 3 : activity >= 50 ? 2 : activity >= 20 ? 1 : 0) : 0;
  ["10:20", "15:30", "20:15"].slice(0, proactiveCount).forEach((time) => add(time, "主动浏览", "proactive", "浏览视频、选择感兴趣的内容"));
  const dynamicCount = cfg.ENABLE_DYNAMIC ? Math.min(num(cfg.DYNAMIC_DAILY_COUNT, 1), num(cfg.AUTONOMOUS_DYNAMIC_DAILY_LIMIT, 2), activity >= 88 ? 2 : activity >= 40 ? 1 : 0) : 0;
  ["16:30", "21:10"].slice(0, dynamicCount).forEach((time) => add(time, "发布动态", "dynamic", "根据今日状态发布一条动态"));
  if (cfg.ENABLE_BANGUMI && cfg.BANGUMI_PROACTIVE && activity >= 30) add("12:10", "追番", "bangumi", "检查更新或观看番剧");
  if (cfg.ENABLE_DYNAMIC_WATCH && activity >= 20) {
    ["11:30", "20:30"].slice(0, Math.max(0, Math.min(num(cfg.DYNAMIC_WATCH_TIMES_COUNT, 2), num(cfg.AUTONOMOUS_PROACTIVE_DAILY_LIMIT, 4)))).forEach((time) => add(time, "关注动态", "dynamic_watch", "查看关注者的新动态图文与视频投稿"));
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
      reply_target: cfg.ENABLE_REPLY ? Math.round(num(cfg.AUTONOMOUS_REPLY_DAILY_LIMIT, 80) * (0.15 + activity / 140)) : 0,
      private_target: cfg.ENABLE_PRIVATE_MESSAGES ? Math.round(num(cfg.AUTONOMOUS_PRIVATE_DAILY_LIMIT, 30) * (0.12 + activity / 150)) : 0,
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
    const map = { "stats": mock.stats, "persona/state": mock.persona, "config/schema": mock.schema, "config": mock.config, "account/info": mock.account, "schedule/today": mock.schedule, "schedule/stats": mock.scheduleStats, "memory/stats": mock.memory, "profiles": mock.profiles, "security/stats": mock.security, "tools/available": mock.availableTools, "cache/stats": mock.cache };
    if (path === "account/qr/generate") return { image: "", key: "preview", expires_in: 180 };
    if (path === "account/qr/poll") return { status: "waiting", message: "预览模式不连接真实账号" };
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
    if (path === "cache/purge") {
      const deep = body.mode === "deep";
      const removedBytes = deep ? mock.cache.total_bytes : Object.entries(mock.cache.buckets || {}).filter(([key]) => key !== "qr").reduce((sum, [, item]) => sum + num(item.bytes), 0);
      Object.entries(mock.cache.buckets || {}).forEach(([key, item]) => { if (deep || key !== "qr") item.bytes = 0; });
      mock.cache.total_bytes = Object.values(mock.cache.buckets || {}).reduce((sum, item) => sum + num(item.bytes), 0);
      return { mode: deep ? "deep" : "normal", removed_bytes: removedBytes, total_bytes: mock.cache.total_bytes };
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

function renderSidebar() {
  const running = state.stats.running !== false;
  const accountReady = state.stats.account_connected || state.account?.logged_in;
  sidebar.innerHTML = `
    <div class="sidebar-brand">
      <div class="brand-mark"><img src="./assets/logo.png" alt="" /></div>
      <div class="brand-copy"><strong>BiliBot</strong><span>控制中心</span></div>
    </div>
    <div class="sidebar-state" aria-label="服务状态">
      <span class="status-dot ${running ? "is-online" : ""}"></span>
      <div><strong>${running ? "服务运行中" : "服务未运行"}</strong><span>${accountReady ? "账号链路已配置" : "等待连接账号"}</span></div>
    </div>
    <div class="nav-label">管理</div>
    <nav class="nav-list">${NAV_ITEMS.map(([id, iconName, label, hint]) => `
      <button class="nav-item ${state.currentPage === id ? "is-active" : ""}" data-page="${id}" type="button" title="${esc(hint)}" aria-current="${state.currentPage === id ? "page" : "false"}">
        ${icon(iconName, "nav-icon")}<span>${esc(label)}</span>${id === "basics" && state.dirtyKeys.size ? `<b class="nav-badge">${state.dirtyKeys.size}</b>` : ""}
      </button>`).join("")}</nav>`;
  sidebar.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.page)));
}

function updateSaveDock() {
  if (!state.dirtyKeys.size && !state.isSaving) {
    saveDock.classList.remove("is-visible");
    saveDock.innerHTML = "";
    return;
  }
  const pending = state.isSaving;
  saveDock.innerHTML = `<div class="save-dock-inner ${pending ? "is-saving" : ""}" aria-live="polite" aria-busy="${pending}">
    <div class="save-dock-copy"><strong>${pending ? "正在保存并刷新计划" : `${state.dirtyKeys.size} 项修改待保存`}</strong><span>${pending ? "自主模式会调用当前模型，请稍候" : "确认后写入插件配置"}</span></div>
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
  const [schema, config, stats, persona] = await Promise.all([apiGet("config/schema"), apiGet("config"), apiGet("stats"), apiGet("persona/state")]);
  state.schema = schema || {};
  state.config = config || {};
  state.draft = structuredClone(state.config);
  state.stats = stats || {};
  state.persona = persona || {};
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
    state.stats = await apiGet("stats") || {};
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

function renderCurrentPage() {
  content.innerHTML = renderPage(state.currentPage);
  bindContent();
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
  return `<div class="section-head"><div class="section-title"><span class="section-icon">${icon(iconName)}</span><div><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ""}</div></div>${extra}</div>`;
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

function renderControl(key, field = {}, compact = false) {
  const value = currentValue(key);
  const type = field.type || "string";
  const label = descriptionMeta(field).label;
  if (type === "bool") {
    return `<label class="switch-control"><input data-config-key="${key}" type="checkbox" ${value ? "checked" : ""} /><span class="switch-track"><i></i></span><span class="sr-only">切换${esc(label)}</span></label>`;
  }
  if (field.options?.length) {
    return `<select class="input" data-config-key="${key}" aria-label="${esc(label)}">${field.options.map((option) => `<option value="${esc(option)}" ${String(value) === String(option) ? "selected" : ""}>${esc(option)}</option>`).join("")}</select>`;
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
  if (!hasKey(key)) return "";
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
  const replyLimit = Math.max(1, num(currentValue("AUTONOMOUS_REPLY_DAILY_LIMIT"), 80));
  const privateLimit = Math.max(1, num(currentValue("AUTONOMOUS_PRIVATE_DAILY_LIMIT"), 30));
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

function renderInteraction() {
  return `${pageHead("INTERACTION", "回复与互动", "把值得回应的内容挑出来，再用明确的频率、冷却和硬上限保护账号。", statusPill(`${fmt(state.stats.filtered_today)} 条已过滤`, "green"))}
    <section class="feature-banner interest-banner"><div class="feature-icon">${icon("star")}</div><div><span>兴趣选择器</span><h2>不是每条消息都必须回复</h2><p>广告、复读与低价值内容先被硬过滤，再由模型根据管理员提示词挑选真正值得回应的评论和私信。</p></div><div class="feature-control">${hasKey("ENABLE_INTEREST_BASED_REPLY") ? renderControl("ENABLE_INTEREST_BASED_REPLY", state.schema.ENABLE_INTEREST_BASED_REPLY) : ""}</div></section>
    <div class="two-column">
      ${renderConfigSection("内容筛选", "先做确定性过滤，再执行兴趣判断", ["FILTER_LOW_VALUE_MESSAGES", "FILTER_DUPLICATE_MESSAGES", "FILTER_AD_MESSAGES", "ENABLE_INTEREST_BASED_REPLY", "INTEREST_APPLY_TO_PRIVATE"], "shield")}
      ${renderConfigSection("回复边界", "概率保留为最后一道节奏控制", ["ENABLE_REPLY", "REPLY_PROBABILITY_PERCENT", "REPLY_COOLDOWN", "POLL_INTERVAL", "REPLY_ALWAYS_UIDS", "ENABLE_SIMILAR_SKIP", "REPLY_SIMILARITY_PERCENT"], "controller")}
    </div>
    ${renderConfigSection("兴趣选择与评论提示词", "兴趣提示词只负责判断是否值得回复；评论补充提示词负责决定怎么回复", ["INTEREST_SELECTION_PROMPT", "CUSTOM_REPLY_INSTRUCTION"], "star")}
    <div class="two-column">
      ${renderConfigSection("B站私信", "只处理安全、有效且满足范围规则的新私信", ["ENABLE_PRIVATE_MESSAGES", "PRIVATE_MESSAGE_REPLY_SCOPE", "PRIVATE_MESSAGE_AUTO_REPLY", "PRIVATE_MESSAGE_AUTO_WATCH_VIDEO", "PRIVATE_MESSAGE_BILI_SEARCH_ENABLED", "PRIVATE_MESSAGE_BILI_SEARCH_LIMIT", "PRIVATE_MESSAGE_REPLY_WHITELIST_UIDS", "PRIVATE_MESSAGE_MAX_PER_POLL", "PRIVATE_MESSAGE_MAX_MESSAGE_AGE", "CUSTOM_PRIVATE_MESSAGE_INSTRUCTION"], "user")}
      ${renderConfigSection("直播弹幕", "限制回复速度与长度，避免抢话和刷屏", ["ENABLE_LIVE_DANMAKU_REPLY", "LIVE_DANMAKU_ROOM_ID", "LIVE_DANMAKU_POLL_INTERVAL", "LIVE_DANMAKU_REPLY_COOLDOWN", "LIVE_DANMAKU_MAX_PER_MINUTE", "LIVE_DANMAKU_REPLY_MAX_LENGTH", "CUSTOM_LIVE_DANMAKU_INSTRUCTION"], "video")}
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
  { id: "proactive", title: "主动浏览", icon: "play", toggle: "ENABLE_PROACTIVE", description: "浏览视频并执行受评分阈值保护的点赞、投币、收藏、评论或关注。", keys: ["PROACTIVE_VIDEO_COUNT", "PROACTIVE_DAILY_LIMIT", "PROACTIVE_TIMES_COUNT", "PROACTIVE_COMMENT_COUNT"] },
  { id: "prefilter", title: "内容挑选", icon: "search", toggle: "ENABLE_PROACTIVE_LLM_PREFILTER", description: "用关注源、兴趣提示词和模型预筛选决定今天值得看的内容。", keys: ["PROACTIVE_FOLLOW_UIDS", "PROACTIVE_SEARCH_QUERY_PROMPT", "PROACTIVE_TASTE_WINDOW_DAYS", "PROACTIVE_VIDEO_POOLS", "PROACTIVE_LLM_PREFILTER_MAX_REJECTS", "CUSTOM_PROACTIVE_INSTRUCTION"] },
  { id: "owner-share", title: "给主人分享", icon: "heart", toggle: "RECOMMEND_OWNER_DELIVERY", description: "只在发现真正有趣的内容后分享，并受每日上限与最低评分保护。", keys: ["RECOMMEND_OWNER_MIN_SCORE", "RECOMMEND_OWNER_DAILY_LIMIT", "CUSTOM_RECOMMEND_INSTRUCTION"] },
  { id: "dynamic", title: "动态发布", icon: "message", toggle: "ENABLE_DYNAMIC", description: "按今日计划生成并发布 B站动态，关闭后不会进入事件环。", keys: ["DYNAMIC_TIMES_COUNT", "DYNAMIC_DAILY_COUNT", "DYNAMIC_TOPICS", "CUSTOM_DYNAMIC_INSTRUCTION"] },
  { id: "dynamic-watch", title: "查看关注动态", icon: "search", toggle: "ENABLE_DYNAMIC_WATCH", description: "巡视关注者的新动态图文与视频投稿，每条内容使用独立模型上下文。", keys: ["DYNAMIC_WATCH_TIMES_COUNT", "DYNAMIC_WATCH_DAILY_LIMIT", "DYNAMIC_WATCH_SPECIAL_ONLY", "DYNAMIC_WATCH_INCLUDE_VIDEO_POSTS", "DYNAMIC_WATCH_INTEREST_PROMPT"] },
  { id: "special-follow", title: "特别关注", icon: "star", toggle: "SPECIAL_FOLLOW_ENABLED", description: "单独巡视特别关注用户，可选择随机节奏或管理员固定时刻。", keys: ["SPECIAL_FOLLOW_MODE", "SPECIAL_FOLLOW_TIMES_COUNT", "SPECIAL_FOLLOW_FIXED_TIMES"] },
  { id: "bangumi", title: "番剧日程", icon: "video", toggle: "ENABLE_BANGUMI", description: "查看番剧更新并在主动追番开启时安排真实追番事件。", keys: ["BANGUMI_PROACTIVE", "BANGUMI_POOLS", "BANGUMI_EPISODE_COUNT", "BANGUMI_CONTINUE_SCORE", "BANGUMI_DAILY_LIMIT", "BANGUMI_COMMENT", "BANGUMI_AUTO_FOLLOW"] },
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

function ringEventArc(event, originalIndex, orderIndex) {
  const minute = minutesOf(event.time);
  if (minute === null) return "";
  const start = (minute - 15 + 1440) % 1440;
  const end = (minute + 15) % 1440;
  const d = start < end ? arcPath(start, end) : `${arcPath(start, 1439)} ${arcPath(0, end)}`;
  const style = EVENT_STYLES[event.kind] || EVENT_STYLES.proactive;
  const meta = eventPhaseMeta(event);
  const label = esc(`${event.time} ${event.label || style.label}，${meta.label}`);
  const active = originalIndex === state.selectedScheduleIndex;
  return `<g class="ring-event-group phase-${meta.phase}">
    <path class="ring-event-hit" data-segment-index="${originalIndex}" d="${d}" tabindex="0" role="button" aria-label="${label}" aria-pressed="${active}" />
    <path class="ring-event ${active ? "is-active" : ""} is-${meta.phase}" data-ring-index="${originalIndex}" d="${d}" pathLength="100" stroke="url(#grad-${event.kind || "proactive"})" style="--segment-delay:${orderIndex * 48}ms" aria-hidden="true" />
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
  if (phase === "overdue") return { phase, label: "已错过", detail: "时刻已过，等待补执行或重新生成" };
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
    ? `<span>已选择事件</span><strong>${esc(selected.time)}</strong><b>${esc(selected.label || (EVENT_STYLES[selected.kind] || EVENT_STYLES.proactive).label)}</b><small>${selectedMeta.label} · ${selectedMeta.detail}</small>`
    : `<span>当前时间</span><strong>${currentClockText()}</strong><b>${next ? `下一步 · ${esc(next.label || (EVENT_STYLES[next.kind] || EVENT_STYLES.proactive).label)}` : "今日暂无后续事件"}</b><small>${next?.time ? `预计 ${esc(next.time)} 执行` : "过时未执行项目会在列表中标记为已错过"}</small>`;
  return `<div class="ring-shell">
    <svg class="schedule-ring" viewBox="0 0 360 360" aria-label="24小时事件环">
      <defs>${Object.entries(EVENT_STYLES).map(([key, item]) => `<linearGradient id="grad-${key}" x1="0" y1="0" x2="1" y2="1"><stop stop-color="${item.gradient[0]}"/><stop offset="1" stop-color="${item.gradient[1]}"/></linearGradient>`).join("")}</defs>
      <circle class="ring-track" cx="180" cy="180" r="132" />${ticks}${labels}${sleepArc}
      ${ordered.map(({ event, index }, orderIndex) => ringEventArc(event, index, orderIndex)).join("")}
      <g class="now-pointer" transform="rotate(${nowAngle} 180 180)" aria-label="当前时间 ${currentClockText()}">
        <path class="now-pointer-halo" d="M180 39 L164 7 L196 7 Z" />
        <path class="now-pointer-cone" d="M180 35 L170 12 L190 12 Z" />
      </g>
    </svg>
    <div class="ring-center">${center}</div>
    <div class="ring-glow" style="--ring-a:${selectedStyle.gradient[0]};--ring-b:${selectedStyle.gradient[1]}"></div>
  </div>`;
}

function renderActivityControl() {
  if (!hasKey("AUTONOMOUS_ACTIVITY_LEVEL")) return "";
  const value = clamp(num(currentValue("AUTONOMOUS_ACTIVITY_LEVEL"), 55), 0, 100);
  return `<section class="activity-panel activity-enter ${value >= 100 ? "is-max" : ""}">
    <div class="activity-copy"><span>TODAY'S ACTIVITY</span><h2><b id="activity-value">${value}</b><small>%</small></h2><p id="activity-label">${activityLabel(value)}状态 · 活跃度越高，已启用事件会更频繁、活动时段也更长</p></div>
    <div class="activity-slider-wrap"><div class="activity-track" style="--activity:${value}%"><span class="activity-fill"></span><input id="activity-slider" class="activity-slider" data-config-key="AUTONOMOUS_ACTIVITY_LEVEL" type="range" min="0" max="100" step="1" value="${value}" aria-label="今日基础活跃度" /></div><div class="activity-scale"><span>低迷</span><span>平稳</span><span>活跃</span><span>高能</span></div></div>
  </section>`;
}

function renderEventList(events) {
  if (!events.length) return `<div class="empty-event">${icon("calendar")}<strong>今天没有主动事件</strong><span>关闭总开关、低活跃度或硬上限为 0 时，这是正常状态。</span></div>`;
  return `<div class="event-list">${events.map((event, index) => {
    const style = EVENT_STYLES[event.kind] || EVENT_STYLES.proactive;
    const meta = eventPhaseMeta(event);
    return `<button class="event-row phase-${meta.phase} ${index === state.selectedScheduleIndex ? "is-active" : ""}" data-segment-index="${index}" type="button"><span class="event-color" style="--a:${style.gradient[0]};--b:${style.gradient[1]}">${icon(style.icon)}</span><span class="event-time">${esc(event.time)}</span><span class="event-copy"><strong>${esc(event.label || style.label)}</strong><small>${esc(event.description || "按今日计划执行")}</small></span><span class="event-status ${meta.phase}" title="${esc(meta.detail)}">${meta.label}</span></button>`;
  }).join("")}</div>`;
}

function renderSelectedEvent(events) {
  const event = events[state.selectedScheduleIndex];
  if (!event) {
    const next = nextScheduleEvent(events);
    const style = EVENT_STYLES[next?.kind] || EVENT_STYLES.proactive;
    return `<div id="selected-event" class="selected-event is-next" style="--a:${style.gradient[0]};--b:${style.gradient[1]}"><span class="selected-event-icon">${icon(next ? style.icon : "clock")}</span><div><span>${next ? "下一执行" : "今日进度"}</span><h3>${next?.time ? `${esc(next.time)} · ${esc(next.label || style.label)}` : "今日暂无后续事件"}</h3><p>${esc(next?.description || "过去未完成的事件会标记为已错过，可重新生成计划或等待补执行。")}</p></div></div>`;
  }
  const style = EVENT_STYLES[event.kind] || EVENT_STYLES.proactive;
  const meta = eventPhaseMeta(event);
  return `<div id="selected-event" class="selected-event phase-${meta.phase}" style="--a:${style.gradient[0]};--b:${style.gradient[1]}"><span class="selected-event-icon">${icon(style.icon)}</span><div><span>${meta.phase === "done" ? "已完成事件" : meta.phase === "overdue" ? "已错过事件" : "计划事件"}</span><h3>${esc(event.time)} · ${esc(event.label || style.label)}</h3><p>${esc(event.description || "Bot 会按今天的计划执行。")} · ${esc(meta.detail)}</p></div></div>`;
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
  const status = failed ? "模型调用失败，当前使用安全 fallback" : autonomous ? "模型计划已通过硬边界校验" : "管理员固定计划";
  const detail = failed
    ? `${plan.model_error || "未配置模型提供商，或 AI 对话总开关未开启。"} 请检查模型提供商和 AI 对话总开关。`
    : plan.rationale || (autonomous ? "保存修改后调用当前模型生成当天计划。" : "保存修改后按准确时刻刷新当天计划。");
  return `<div class="plan-status ${failed ? "has-error" : autonomous ? "is-model" : "is-fixed"}"><span>${icon(failed ? "lightning" : autonomous ? "star" : "clock")}</span><div><strong>${esc(status)}</strong><p>${esc(detail)}</p></div>${plan.generated_at ? `<small>${esc(plan.generated_at)}</small>` : ""}</div>`;
}

function renderAutonomousTemplate(plan, autonomous, events) {
  const next = nextScheduleEvent(events);
  const limitKeys = ["SLEEP_START", "SLEEP_END", "AUTONOMOUS_MIN_ACTION_GAP_MINUTES", "AUTONOMOUS_REPLY_DAILY_LIMIT", "AUTONOMOUS_PRIVATE_DAILY_LIMIT", "AUTONOMOUS_DYNAMIC_DAILY_LIMIT", "AUTONOMOUS_PROACTIVE_DAILY_LIMIT"];
  return `<section class="plan-template autonomous-template ${autonomous ? "is-active" : ""}" data-plan-template="autonomous" aria-hidden="${!autonomous}" ${autonomous ? "" : "inert"}>
    ${renderPlanStatus(plan, true)}
    <div class="plan-facts">
      <div><span>今日事件</span><strong>${events.length}</strong><small>只来自已启用能力</small></div>
      <div><span>下一事件</span><strong>${next?.time ? esc(next.time) : "—"}</strong><small>${esc(next?.label || "暂无待执行事件")}</small></div>
      <div><span>评论 / 私信目标</span><strong>${fmt(plan.reply_target)} / ${fmt(plan.private_target)}</strong><small>模型目标仍受硬上限保护</small></div>
      <div><span>计划来源</span><strong>${plan.source === "fallback" ? "安全回退" : "当前模型"}</strong><small>仅保存时更新当天计划</small></div>
    </div>
    ${renderConfigSection("自主计划提示词", "作为 B站每日安排的附加提示，不会替换 AstrBot 原人设", ["AUTONOMOUS_PLAN_PROMPT"], "star", "", "embedded-section")}
    <section class="embedded-section limit-section">${sectionHead("管理员硬上限", "模型只能在这些边界内安排，休眠区间不会生成主动事件", "lock")}<div class="plan-limit-grid">${limitKeys.map((key) => renderField(key, { tile: true })).join("")}</div></section>
  </section>`;
}

function renderFixedTemplate(plan, autonomous) {
  const exactKeys = ["FIXED_PROACTIVE_TIMES", "FIXED_DYNAMIC_TIMES", "FIXED_DYNAMIC_WATCH_TIMES", "FIXED_BANGUMI_TIMES", "FIXED_SPECIAL_FOLLOW_TIMES"];
  return `<section class="plan-template fixed-template ${autonomous ? "" : "is-active"}" data-plan-template="fixed" aria-hidden="${autonomous}" ${autonomous ? "inert" : ""}>
    ${renderPlanStatus(plan, false)}
    <div class="fixed-target-grid">${["FIXED_REPLY_DAILY_TARGET", "FIXED_PRIVATE_DAILY_TARGET", "SLEEP_START", "SLEEP_END", "AUTONOMOUS_MIN_ACTION_GAP_MINUTES"].map((key) => renderField(key, { tile: true })).join("")}</div>
    <section class="embedded-section fixed-times-section">${sectionHead("准确执行时刻", "对应能力关闭时，该行时刻不会进入事件环；每个时间都可直接选择", "calendar")}<div class="fixed-times-grid">${exactKeys.map((key) => renderField(key, { tile: true })).join("")}</div></section>
  </section>`;
}

function renderPlanModeCard(plan, autonomous, events) {
  return `<section class="card plan-mode-card">
    <div class="plan-mode-head"><div><span>DAILY PLAN MODE</span><h2>当天计划生成方式</h2><p>切换只修改草稿，只有点击“保存修改”才会重新生成当天计划。</p></div><div class="plan-mode-switch ${autonomous ? "is-autonomous" : "is-fixed"}" role="tablist" aria-label="当天计划模式"><button class="${autonomous ? "is-active" : ""}" data-plan-mode="autonomous" role="tab" aria-selected="${autonomous}" type="button">${icon("star")}自主安排</button><button class="${autonomous ? "" : "is-active"}" data-plan-mode="fixed" role="tab" aria-selected="${!autonomous}" type="button">${icon("clock")}固定计划</button><i aria-hidden="true"></i></div></div>
    <div class="plan-template-stage ${autonomous ? "show-autonomous" : "show-fixed"}">${renderAutonomousTemplate(plan, autonomous, events)}${renderFixedTemplate(plan, autonomous)}</div>
  </section>`;
}

function capabilitySummary(item) {
  if (!currentValue(item.toggle)) return "总开关已关闭，不会生成相关事件";
  if (item.id === "proactive") return `每天最多 ${fmt(currentValue("PROACTIVE_DAILY_LIMIT"))} 轮 · 每轮 ${fmt(currentValue("PROACTIVE_VIDEO_COUNT"))} 个视频`;
  if (item.id === "owner-share") return `最低 ${fmt(currentValue("RECOMMEND_OWNER_MIN_SCORE"))} 分 · 每天最多 ${fmt(currentValue("RECOMMEND_OWNER_DAILY_LIMIT"))} 次`;
  if (item.id === "dynamic") return `每天最多 ${fmt(currentValue("DYNAMIC_DAILY_COUNT"))} 条动态`;
  if (item.id === "dynamic-watch") return `每天最多 ${fmt(currentValue("DYNAMIC_WATCH_DAILY_LIMIT"))} 次 · 包含视频投稿 ${currentValue("DYNAMIC_WATCH_INCLUDE_VIDEO_POSTS") ? "开启" : "关闭"}`;
  if (item.id === "special-follow") return `${currentValue("SPECIAL_FOLLOW_MODE") === "fixed" ? "固定时刻" : "自主节奏"} · 每天 ${fmt(currentValue("SPECIAL_FOLLOW_TIMES_COUNT"))} 次`;
  if (item.id === "bangumi") return `${currentValue("BANGUMI_PROACTIVE") ? "主动追番" : "仅启用资料能力"} · 每天最多 ${fmt(currentValue("BANGUMI_DAILY_LIMIT"))} 次`;
  return "模型预筛选已启用";
}

function renderCapabilityCard(item) {
  const enabled = Boolean(currentValue(item.toggle));
  return `<article class="capability-card ${enabled ? "is-enabled" : ""}" data-capability-card="${item.id}"><div class="capability-top"><span class="capability-icon">${icon(item.icon)}</span>${renderControl(item.toggle, state.schema[item.toggle])}</div><div class="capability-copy"><strong>${esc(item.title)}</strong><p>${esc(item.description)}</p><small>${esc(capabilitySummary(item))}</small></div><button class="capability-settings" data-capability-open="${item.id}" type="button">${icon("settings")}详细设置</button></article>`;
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
  newCard.querySelector("[data-capability-open]")?.addEventListener("click", () => openAutonomyDrawer(id));
}

function renderAutonomyDrawer(item) {
  const keys = item.keys.filter(hasKey);
  modalRoot.innerHTML = `<div class="drawer-backdrop" data-drawer-backdrop><aside class="autonomy-drawer" role="dialog" aria-modal="true" aria-labelledby="autonomy-drawer-title"><header><span class="drawer-icon">${icon(item.icon)}</span><div><small>AUTONOMY CAPABILITY</small><h2 id="autonomy-drawer-title">${esc(item.title)}</h2><p>${esc(item.description)}</p></div><button class="modal-close" data-drawer-close type="button" aria-label="关闭">×</button></header><div class="drawer-toggle"><div><strong>总开关</strong><span>关闭后保存，相关事件会从当天计划移除。</span></div>${renderControl(item.toggle, state.schema[item.toggle])}</div><div class="drawer-fields">${renderFields(keys)}</div><footer><button class="button soft" data-drawer-close type="button">完成设置</button></footer></aside></div>`;
  bindConfigControls(modalRoot);
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
        ${sectionHead("24 小时时刻事件环", "三角指针指向当前时刻；事件按当天真实日程从 0 点起顺时针铺开", "clock", statusPill(autonomous ? "Bot 自主" : "固定计划", autonomous ? "violet" : "neutral"))}
        ${renderScheduleRing(events)}
        <div class="ring-legend">${Object.entries(EVENT_STYLES).map(([key, item]) => `<span><i style="--a:${item.gradient[0]};--b:${item.gradient[1]}"></i>${esc(item.label)}</span>`).join("")}</div>
      </article>
      <aside class="schedule-side"><article class="card event-card">${sectionHead("今日事件", `${completedCount} 已完成 · ${upcomingCount} 待执行${overdueCount ? ` · ${overdueCount} 已错过` : ""}${invalidCount ? ` · ${invalidCount} 时间无效` : ""}`, "calendar")}${renderSelectedEvent(events)}${renderEventList(events)}</article></aside>
    </section>
    ${renderPlanModeCard(plan, autonomous, events)}
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
  return `<div class="tool-picker-summary"><div class="tool-picker-copy"><span class="tool-picker-icon">${icon("controller")}</span><div><strong>${selected.length ? `已允许 ${selected.length} 个只读工具` : "未开放 B站工具"}</strong><p>${selected.length ? selected.map((tool) => tool.label || tool.name).join("、") : "B站评论与私信仍保持完全隔离。"}</p></div></div><button class="button soft" data-action="open-tool-picker" type="button">${icon("settings")}选择工具</button></div>`;
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
    <section class="security-hero ${isolated ? "is-safe" : "is-risk"}"><span>${icon(isolated ? "lock" : "unlock")}</span><div><small>TOOL ISOLATION</small><h2>${isolated ? "B站端权限已隔离" : "工具隔离已关闭"}</h2><p>${isolated ? "B站评论与私信无法运行 AstrBot/QQ 命令、文件、Shell 或写操作。" : "建议立即重新开启隔离；只读白名单仍由后端硬限制。"}</p></div>${statusPill(isolated ? "默认安全" : "需要处理", isolated ? "green" : "red")}</section>
    <section class="metrics-grid four">
      ${metricCard("今日安全事件", fmt(state.security.today_total), "过滤、拒绝与审计总数", "shield", "blue")}
      ${metricCard("内容过滤", fmt(securityCount(["low_value_filtered", "duplicate_filtered", "ad_filtered"])), "低价值、复读与广告", "message", "green")}
      ${metricCard("工具拒绝", fmt(securityCount(["bili_tool_denied"])), "未授权请求未执行", "lock", "violet")}
      ${metricCard("记忆策略", memoryMode === "safe_share" ? "安全共享" : "平台隔离", "仅向主人侧开放脱敏摘要", "memory-card", "orange")}
    </section>
    ${renderConfigSection("工具隔离总控", "高风险工具不会因为关闭前端开关而自动获得权限；后端仍执行硬白名单", ["BILI_TOOL_ISOLATION_ENABLED", "BILI_ALLOW_SEARCH_TOOLS", "BILI_PROMPT_INJECTION_DEFENSE", "BILI_TOOL_AUDIT_ENABLED"], "shield")}
    <section class="card section-card tool-access-card">${sectionHead("只读工具白名单", "从 AstrBot 当前真实注册表中选择；未提供 B站安全适配器的工具不能启用", "controller")}${renderToolSummary()}</section>
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

const BASIC_GROUP_ORDER = ["人设与模型", "性格演化", "Embedding 与记忆", "视频与图片视觉", "图片生成", "联网搜索", "总结", "Cookie 与系统", "高级接口", "其他长期配置"];

function basicGroupFor(key, field) {
  const group = descriptionMeta(field).group;
  if (/人设/.test(group) || ["LLM_PROVIDER_ID", "USE_ASTRBOT_PERSONA", "CUSTOM_SYSTEM_PROMPT"].includes(key)) return "人设与模型";
  if (/性格演化/.test(group) || key.startsWith("EVOLVE_")) return "性格演化";
  if (/高级·记忆/.test(group) || key.startsWith("EMBED_")) return "Embedding 与记忆";
  if (/视觉|视频分析/.test(group) || /VISION/.test(key)) return "视频与图片视觉";
  if (/图片生成/.test(group) || key.startsWith("IMAGE_GEN_")) return "图片生成";
  if (/联网搜索/.test(group) || key.startsWith("WEB_SEARCH_")) return "联网搜索";
  if (/总结/.test(group) || key.includes("DAILY") || key.includes("WEEKLY")) return "总结";
  if (/系统/.test(group) || key.startsWith("COOKIE_")) return "Cookie 与系统";
  if (/高级/.test(group) || /(API|MODEL|PROVIDER)/.test(key)) return "高级接口";
  return "其他长期配置";
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
  const allEntries = Object.entries(state.schema).filter(([key]) => !assigned.has(key));
  const query = state.settingsSearch.trim().toLowerCase();
  const filtered = allEntries.filter(([key, field]) => {
    if (!query) return true;
    return `${key} ${field.description || ""} ${field.hint || ""}`.toLowerCase().includes(query);
  });
  const groups = Object.fromEntries(BASIC_GROUP_ORDER.map((name) => [name, []]));
  filtered.forEach(([key, field]) => groups[basicGroupFor(key, field)].push(key));
  return `${pageHead("FOUNDATION", "基础设置", "这里只保留完成初始化后很少需要调整的人设、模型和高级能力；常用行为已拆到对应页面。")}
    ${renderCacheCard()}
    <section class="settings-search card"><span>${icon("search")}</span><input id="settings-search" type="search" value="${esc(state.settingsSearch)}" placeholder="搜索配置名称、说明或 KEY" aria-label="搜索基础设置" />${state.settingsSearch ? `<button data-action="clear-settings-search" type="button">清除</button>` : ""}</section>
    <div class="settings-summary"><span>共 ${allEntries.length} 项长期配置</span><span>当前显示 ${filtered.length} 项</span><span>${state.dirtyKeys.size} 项待保存</span></div>
    <div class="accordion-list">${BASIC_GROUP_ORDER.map((name, index) => {
      const keys = groups[name];
      if (!keys.length) return "";
      const iconName = { "人设与模型": "heart", "性格演化": "star", "Embedding 与记忆": "memory-card", "视频与图片视觉": "video", "图片生成": "sun", "联网搜索": "search", "总结": "calendar", "Cookie 与系统": "settings", "高级接口": "controller" }[name] || "settings";
      return `<details class="settings-group card" ${query || index < 2 ? "open" : ""}><summary><span class="section-icon">${icon(iconName)}</span><div><strong>${esc(name)}</strong><small>${keys.length} 项配置</small></div>${icon("arrow-right")}</summary><div class="settings-group-body"><div class="settings-group-inner">${renderFields(keys)}</div></div></details>`;
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
      setDraft(key, value);
      if (control.id === "activity-slider") {
        const sliderValue = clamp(num(value), 0, 100);
        const track = control.closest(".activity-track");
        track?.style.setProperty("--activity", `${sliderValue}%`);
        control.closest(".activity-panel")?.classList.toggle("is-max", sliderValue >= 100);
        const valueNode = root.querySelector("#activity-value") || content.querySelector("#activity-value");
        const labelNode = root.querySelector("#activity-label") || content.querySelector("#activity-label");
        if (valueNode) valueNode.textContent = sliderValue;
        if (labelNode) labelNode.textContent = `${activityLabel(sliderValue)}状态 · 活跃度越高，真实事件更频繁、活动时段也更长`;
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

function bindContent() {
  bindConfigControls();
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
  content.querySelectorAll("[data-step-key]").forEach((buttonNode) => buttonNode.addEventListener("click", () => {
    const key = buttonNode.dataset.stepKey;
    const input = content.querySelector(`[data-config-key="${key}"]`);
    if (!input) return;
    const field = state.schema[key] || {};
    const step = num(input.dataset.step, field.type === "float" ? 0.1 : 1);
    const min = num(input.dataset.min, -999999);
    const max = num(input.dataset.max, 999999);
    const next = clamp(num(input.value) + num(buttonNode.dataset.stepDir) * step, min, max);
    input.value = field.type === "int" ? String(Math.round(next)) : String(Math.round(next * 1000) / 1000);
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }));
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
    const event = events[index];
    const meta = eventPhaseMeta(event);
    center.innerHTML = `<span>已选择事件</span><strong>${esc(event.time)}</strong><b>${esc(event.label)}</b><small>${meta.label} · ${meta.detail}</small>`;
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
    const ok = await confirmModal("重新生成今日计划", "这会清空今天尚未完成的日程并立即根据当前活跃度与硬上限重新生成。", "重新生成");
    if (!ok) return;
    const regenerated = await apiPost("schedule/regenerate", {});
    state.schedule = { ...state.schedule, ...(regenerated || {}) };
    state.scheduleStats = await apiGet("schedule/stats") || {};
    renderCurrentPage();
    const plan = regenerated?.autonomous_plan;
    if (plan?.generation_status === "error") {
      toast("计划已生成，但模型调用失败", `${plan.model_error || "未配置模型提供商，或 AI 对话总开关未开启。"} 已使用安全 fallback。`, "error");
    } else {
      toast("今日计划已更新", "新计划已经过睡眠区间、最小间隔与硬上限校验");
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
  if (!keys.length || state.isSaving) return;
  const body = Object.fromEntries(keys.map((key) => [key, state.draft[key]]));
  const refreshSchedule = keys.some((key) => SCHEDULE_REGEN_KEYS.has(key));
  state.isSaving = true;
  updateSaveDock();
  try {
    await apiPost("config", body);
    Object.assign(state.config, body);
    Object.assign(mock.config, body);
    state.dirtyKeys.clear();
    state.draft = structuredClone(state.config);

    let scheduleError = null;
    let regeneratedPlan = null;
    if (refreshSchedule) {
      try {
        const regenerated = await apiPost("schedule/regenerate", {});
        state.schedule = { ...state.schedule, ...(regenerated || {}) };
        regeneratedPlan = regenerated?.autonomous_plan || null;
        state.scheduleStats = await apiGet("schedule/stats") || {};
        const eventCount = (state.schedule.events || []).length;
        state.selectedScheduleIndex = eventCount ? clamp(state.selectedScheduleIndex, -1, eventCount - 1) : -1;
      } catch (error) {
        scheduleError = error;
      }
    }

    state.isSaving = false;
    updateSaveDock();
    renderSidebar();
    renderCurrentPage();
    if (scheduleError) {
      toast("配置已保存，日程刷新失败", scheduleError.message || "可点击“重新生成今日计划”重试", "error");
    } else if (regeneratedPlan?.generation_status === "error") {
      toast("配置已保存，模型调用失败", `${regeneratedPlan.model_error || "未配置模型提供商，或 AI 对话总开关未开启。"} 已使用安全 fallback；请检查模型提供商与 AI 对话总开关。`, "error");
    } else if (refreshSchedule) {
      toast("配置与今日计划已更新", `已保存 ${keys.length} 项，并根据新边界重新生成真实日程`);
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
  updateSaveDock();
  renderSidebar();
  renderCurrentPage();
  toast("已放弃修改", "配置已恢复为上次保存状态");
}

function toolOriginLabel(tool) {
  return tool.origin_name || ({ builtin: "AstrBot Core", plugin: "插件工具", mcp: "MCP 服务", bilibot: "BiliBot 安全适配器" }[tool.origin] || "其他工具");
}

function openToolPicker() {
  state.toolSearch = "";
  state.toolPickerSelection = new Set(Array.isArray(currentValue("BILI_TOOL_ALLOWLIST")) ? currentValue("BILI_TOOL_ALLOWLIST") : []);
  renderToolPickerModal();
}

function renderToolPickerModal() {
  const groups = new Map();
  state.availableTools.forEach((tool) => {
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
  modalRoot.innerHTML = `<div class="modal-backdrop tool-picker-backdrop" data-modal-backdrop><div class="modal tool-modal" role="dialog" aria-modal="true" aria-labelledby="tool-modal-title"><div class="tool-modal-head"><span class="modal-icon">${icon("controller")}</span><div><h2 id="tool-modal-title">选择 B站只读工具</h2><p>列表来自 AstrBot 当前真实注册表。只有已提供 B站安全适配器的工具可以勾选。</p></div><button class="modal-close" data-tool-close type="button" aria-label="关闭">×</button></div><label class="tool-search">${icon("search")}<input id="tool-search-input" type="search" value="" placeholder="搜索工具或插件" /></label><div class="tool-modal-list">${[...groups.entries()].map(([group, items], index) => `<details class="tool-group" data-tool-group ${index < 2 ? "open" : ""}><summary><div><strong>${esc(group)}</strong><span data-group-count>${items.length} 项</span></div>${icon("arrow-right")}</summary><div class="tool-group-body">${items.map(optionHtml).join("")}</div></details>`).join("") || `<div class="empty-search">${icon("search")}<strong>没有已注册工具</strong><span>请检查 AstrBot 工具注册状态。</span></div>`}<div class="empty-search tool-search-empty" hidden>${icon("search")}<strong>没有匹配工具</strong><span>换一个关键词试试。</span></div></div><div class="tool-modal-actions"><span>已选择 <b data-tool-selected-count>${state.toolPickerSelection.size}</b> 项</span><div><button class="button soft" data-tool-close type="button">取消</button><button class="button primary" data-tool-confirm type="button">${icon("save")}确认选择</button></div></div></div></div>`;
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
  window.addEventListener("beforeunload", (event) => {
    if (state.dirtyKeys.size) {
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
