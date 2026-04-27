import os

from astrbot.api.star import StarTools

PLUGIN_NAME = "astrbot_plugin_bilibili_ai_bot"
DATA_DIR = str(StarTools.get_data_dir(PLUGIN_NAME))
REPLIED_FILE = os.path.join(DATA_DIR, "replied.json")
AFFECTION_FILE = os.path.join(DATA_DIR, "affection.json")
SCHEDULE_FILE = os.path.join(DATA_DIR, "schedule_today.json")
MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")
PERMANENT_MEMORY_FILE = os.path.join(DATA_DIR, "permanent_memory.json")
MOOD_FILE = os.path.join(DATA_DIR, "mood.json")
USER_PROFILE_FILE = os.path.join(DATA_DIR, "user_profiles.json")
MILESTONE_FILE = os.path.join(DATA_DIR, "milestones.json")
SECURITY_LOG_FILE = os.path.join(DATA_DIR, "security_log.json")
VIDEO_MEMORY_FILE = os.path.join(DATA_DIR, "video_memory.json")
BINDING_FILE = os.path.join(DATA_DIR, "qq_bili_bindings.json")
QQ_MEMORY_FILE = os.path.join(DATA_DIR, "qq_memory.json")
PERSONALITY_FILE = os.path.join(DATA_DIR, "personality_evolution.json")
PROACTIVE_LOG_FILE = os.path.join(DATA_DIR, "proactive_log.json")
EXTERNAL_MEMORY_FILE = os.path.join(DATA_DIR, "external_memory.json")
COMMENTED_FILE = os.path.join(DATA_DIR, "commented_videos.json")
WATCH_LOG_FILE = os.path.join(DATA_DIR, "watch_log.json")
DYNAMIC_LOG_FILE = os.path.join(DATA_DIR, "dynamic_log.json")
DYNAMIC_SCHEDULE_FILE = os.path.join(DATA_DIR, "dynamic_schedule.json")
TEMP_IMAGE_DIR = os.path.join(DATA_DIR, "temp_images")
TEMP_VIDEO_DIR = os.path.join(DATA_DIR, "temp_videos")
REPLIED_AT_FILE = os.path.join(DATA_DIR, "replied_at.json")
PROACTIVE_TRIGGER_LOG_FILE = os.path.join(DATA_DIR, "proactive_trigger_log.json")
WEB_SEARCH_CACHE_FILE = os.path.join(DATA_DIR, "web_search_cache.json")

# B站分区表（数据来源：bilibili-API-collect，极少变动）
# 格式：{主分区rid: {"name": "名称", "children": {子分区tid: "名称", ...}}}
BILI_ZONES = {
    1: {"name": "动画", "children": {24: "MAD·AMV", 25: "MMD·3D", 47: "短片·手书", 210: "手办·模玩", 86: "特摄", 27: "综合"}},
    13: {"name": "番剧", "children": {33: "连载动画", 32: "完结动画", 51: "资讯", 152: "官方延伸"}},
    167: {"name": "国创", "children": {153: "国产动画", 168: "原创相关", 169: "布袋戏", 195: "动态漫·广播剧", 170: "资讯"}},
    3: {"name": "音乐", "children": {28: "原创音乐", 31: "翻唱", 30: "VOCALOID", 194: "电音", 59: "演奏", 193: "MV", 130: "音乐综合", 243: "乐评盘点", 244: "音乐教学"}},
    129: {"name": "舞蹈", "children": {20: "宅舞", 198: "街舞", 199: "明星舞蹈", 200: "中国舞", 154: "舞蹈综合", 156: "舞蹈教程"}},
    4: {"name": "游戏", "children": {17: "单机游戏", 171: "电子竞技", 172: "手机游戏", 65: "网络游戏", 173: "桌游棋牌", 121: "GMV", 136: "音游", 19: "Mugen"}},
    36: {"name": "知识", "children": {201: "科学科普", 124: "社科·法律·心理", 228: "人文历史", 207: "财经商业", 208: "校园学习", 209: "职业职场", 229: "设计·创意", 122: "野生技能协会"}},
    188: {"name": "科技", "children": {95: "数码", 230: "软件应用", 231: "计算机技术", 232: "科工机械", 233: "极客DIY"}},
    234: {"name": "运动", "children": {235: "篮球", 249: "足球", 164: "健身", 236: "竞技体育", 237: "运动文化", 238: "运动综合"}},
    223: {"name": "汽车", "children": {245: "赛车", 246: "改装玩车", 247: "新能源车", 248: "房车", 240: "摩托车", 227: "购车攻略", 176: "汽车生活", 224: "汽车文化"}},
    160: {"name": "生活", "children": {138: "搞笑", 250: "出行", 251: "三农", 239: "家居房产", 161: "手工", 162: "绘画", 21: "日常"}},
    211: {"name": "美食", "children": {76: "美食制作", 212: "美食侦探", 213: "美食测评", 214: "田园美食", 215: "美食记录"}},
    217: {"name": "动物圈", "children": {218: "喵星人", 219: "汪星人", 220: "小宠异宠", 221: "野生动物", 222: "动物二创", 75: "动物综合"}},
    119: {"name": "鬼畜", "children": {22: "鬼畜调教", 26: "音MAD", 126: "人力VOCALOID", 216: "鬼畜剧场", 127: "教程演示"}},
    155: {"name": "时尚", "children": {157: "美妆护肤", 252: "仿妆cos", 158: "穿搭", 159: "时尚潮流"}},
    202: {"name": "资讯", "children": {203: "热点", 204: "环球", 205: "社会", 206: "综合"}},
    5: {"name": "娱乐", "children": {71: "综艺", 241: "娱乐杂谈", 242: "粉丝创作", 137: "明星综合"}},
    181: {"name": "影视", "children": {182: "影视杂谈", 183: "影视剪辑", 85: "小剧场", 184: "预告·资讯", 256: "短片"}},
    177: {"name": "纪录片", "children": {37: "人文·历史", 178: "科学·探索·自然", 179: "军事", 180: "社会·美食·旅行"}},
    23: {"name": "电影", "children": {}},
    11: {"name": "电视剧", "children": {}},
}

BILI_MENTION_KEYWORDS = ["b站", "B站", "阿b", "阿B", "啊b", "啊B", "bil", "bili", "bilibili", "小破站", "哔哩哔哩"]

BILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILI_REPLY_URL = "https://api.bilibili.com/x/v2/reply/add"
BILI_NOTIFY_URL = "https://api.bilibili.com/x/msgfeed/reply"
BILI_AT_NOTIFY_URL = "https://api.bilibili.com/x/msgfeed/at"
BILI_COOKIE_INFO_URL = "https://passport.bilibili.com/x/passport-login/web/cookie/info"
BILI_COOKIE_REFRESH_URL = "https://passport.bilibili.com/x/passport-login/web/cookie/refresh"
BILI_COOKIE_CONFIRM_URL = "https://passport.bilibili.com/x/passport-login/web/confirm/refresh"
BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BILI_DYNAMIC_TEXT_URL = "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/create"
BILI_DYNAMIC_IMAGE_URL = "https://api.bilibili.com/x/dynamic/feed/create/dyn"
BILI_UPLOAD_IMAGE_URL = "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs"

MIXIN_KEY_ENC_TAB = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

BILI_RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
BLOCK_KEYWORDS = ["傻逼", "草泥马", "滚", "死", "废物", "智障", "脑残"]
INJECTION_PATTERNS = [
    r'(?:忽略|忘记|无视|跳过).*(?:之前|上面|所有).*(?:指令|提示|设定|规则|prompt)',
    r'(?:你现在是|从现在开始你是|假装你是|扮演)',
    r'(?:system\s*prompt|system\s*message|你的设定是)',
    r'(?:ignore|forget|disregard).*(?:previous|above|all).*(?:instructions|prompt)',
    r'(?:告诉我|重复|输出|显示).*(?:系统提示|system prompt|你的指令|你的设定)',
    r'(?:repeat|show|display|output).*(?:system|instruction|prompt)',
    r'(?:管理员|admin|root|超级用户).*(?:模式|权限|mode)',
    r'(?:开发者|developer|debug).*(?:模式|mode)',
]
LEVEL_NAMES = {"special":"主人💖","close":"好友✨","friend":"熟人😊","normal":"粉丝👋","stranger":"陌生人🌙","cold":"厌恶🖤"}
THREAD_COMPRESS_THRESHOLD = 8
OID_COMPRESS_THRESHOLD = 20    # 同一评论区（oid）记忆超过此数则压缩
OID_KEEP_RECENT = 8            # 评论区压缩时保留最近几条
MAX_SEMANTIC_RESULTS = 3
USER_MEMORY_COMPRESS_THRESHOLD = 20
USER_MEMORY_KEEP_RECENT = 5

DEFAULT_DYNAMIC_TOPICS = [
    "针对今天的某个热点新闻，用你的风格讽刺或点评一下",
    "看到了什么社会现象，冷冷地吐槽一下",
    "分享今天的日常，比如深夜还在干什么、天气、心情",
    "结合现在的时间和天气，说说此刻的感受",
    "像写日记一样，记录今天一个小小的瞬间或想法",
    "对某个互联网现象发表一句毒舌但精准的评价",
]
