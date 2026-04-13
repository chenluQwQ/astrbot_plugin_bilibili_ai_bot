"""
AstrBot Plugin - Bilibili Bot v1.1.0
自动回复评论、好感度、记忆、心情、用户画像、主动视频、动态发布、Web管理面板。
"""
import io, os, re, time, json, math, random, asyncio, hashlib, base64, aiohttp, traceback
from datetime import datetime, timedelta
from functools import reduce
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_bilibili_bot"
DATA_DIR = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
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

BILI_MENTION_KEYWORDS = ["b站", "B站", "阿b", "阿B", "啊b", "啊B", "bil", "bili", "bilibili", "小破站", "哔哩哔哩"]

BILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILI_REPLY_URL = "https://api.bilibili.com/x/v2/reply/add"
BILI_NOTIFY_URL = "https://api.bilibili.com/x/msgfeed/reply"
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
LEVEL_NAMES = {"special":"主人💖","close":"好友✨","friend":"熟人😊","normal":"粉丝👋","stranger":"陌生人🌙","cold":"厌恶🖤"}
THREAD_COMPRESS_THRESHOLD = 8
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

@register("astrbot_plugin_bilibili_bot","chenluQwQ","B站 AI Bot — 自动回复评论、好感度、记忆、心情、用户画像、主动视频、性格演化、动态发布","1.1.0","https://github.com/chenluQwQ/astrbot_plugin_bilibili_bot")
class BiliBiliBot(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._ensure_data_dir()
        self._running = False
        self._task = None
        self._proactive_task = None
        self._last_cookie_check = 0
        self._login_qrcode_key = None
        self._first_poll = not os.path.exists(REPLIED_FILE)
        self._affection = self._load_json(AFFECTION_FILE, {})
        self._memory = self._load_json(MEMORY_FILE, [])
        self._embed_client = None
        self._video_vision_client = None
        self._image_vision_client = None
        self._consecutive_llm_failures = 0
        self._llm_cooldown_until = 0
        self._retry_counts: dict = {}
        self._proactive_times, self._proactive_triggered = [], set()
        self._dynamic_task = None
        self._dynamic_times, self._dynamic_triggered = [], set()
        self._web_panel = None
        if self._has_cookie():
            asyncio.create_task(self._auto_start())
        if self.config.get("ENABLE_WEB_PANEL", False):
            asyncio.create_task(self._start_web_panel())

    # ===== 工具 =====
    def _ensure_data_dir(self): os.makedirs(DATA_DIR, exist_ok=True); os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)
    async def _start_web_panel(self):
        try:
            from .web_panel import WebPanel
            port = self.config.get("WEB_PANEL_PORT", 5001)
            password = self.config.get("WEB_PANEL_PASSWORD", "admin123")
            self._web_panel = WebPanel(self, port=port, password=password)
            await self._web_panel.start()
        except Exception as e:
            logger.error(f"[BiliBot] Web面板启动失败: {e}")
    def _has_cookie(self): return bool(self.config.get("SESSDATA", ""))
    def _headers(self):
        return {"Cookie": f"SESSDATA={self.config.get('SESSDATA','')}; bili_jct={self.config.get('BILI_JCT','')}; DedeUserID={self.config.get('DEDE_USER_ID','')}", "User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com"}
    def _load_json(self, path, default=None):
        if default is None: default = {}
        try:
            if os.path.exists(path):
                with open(path,"r",encoding="utf-8") as f: return json.load(f)
        except: pass
        return default
    def _save_json(self, path, data):
        with open(path,"w",encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)

    async def _http_get(self, url, headers=None, params=None, timeout=10):
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers or self._headers(), params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return await r.json(content_type=None), r
    async def _http_post(self, url, headers=None, data=None, timeout=10):
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=headers or self._headers(), data=data, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return await r.json(content_type=None), r
    async def _http_get_text(self, url, headers=None, params=None, timeout=10):
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers or self._headers(), params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return await r.text(), r

    # ===== Embedding =====
    def _get_embed_client(self):
        if self._embed_client is None:
            api_key = self.config.get("EMBED_API_KEY","")
            if not api_key: return None
            from openai import OpenAI
            base_url = self.config.get("EMBED_API_BASE","https://api.siliconflow.cn/v1")
            self._embed_client = OpenAI(api_key=api_key, base_url=base_url)
        return self._embed_client
    def _get_embedding(self, text):
        client = self._get_embed_client()
        if not client: return None
        try:
            embed_model = self.config.get("EMBED_MODEL","BAAI/bge-m3")
            resp = client.embeddings.create(model=embed_model, input=text)
            return resp.data[0].embedding
        except Exception as e:
            logger.error(f"[BiliBot] Embedding 失败: {e}"); return None
    @staticmethod
    def _cosine_similarity(a, b):
        dot = sum(x*y for x,y in zip(a,b))
        na = math.sqrt(sum(x*x for x in a)); nb = math.sqrt(sum(x*x for x in b))
        return dot/(na*nb) if na and nb else 0

    # ===== 视觉模型 =====
    def _get_video_vision_client(self):
        if self._video_vision_client is None:
            api_key = self.config.get("VIDEO_VISION_API_KEY","")
            if not api_key: return None
            from openai import OpenAI
            base_url = self.config.get("VIDEO_VISION_API_BASE","https://api.siliconflow.cn/v1")
            self._video_vision_client = OpenAI(api_key=api_key, base_url=base_url)
        return self._video_vision_client
    def _get_image_vision_client(self):
        if self._image_vision_client is None:
            api_key = self.config.get("IMAGE_VISION_API_KEY","")
            if not api_key: return None
            from openai import OpenAI
            base_url = self.config.get("IMAGE_VISION_API_BASE","https://api.siliconflow.cn/v1")
            self._image_vision_client = OpenAI(api_key=api_key, base_url=base_url)
        return self._image_vision_client
    def _vision_call(self, client, model, content_parts, max_tokens=250):
        """通用视觉模型调用"""
        try:
            resp = client.chat.completions.create(model=model, messages=[{"role":"user","content":content_parts}], max_tokens=max_tokens)
            return resp.choices[0].message.content.strip() if resp.choices else None
        except Exception as e:
            logger.error(f"[BiliBot] 视觉模型调用失败: {e}"); return None
    async def _fetch_image_base64(self, url):
        """下载图片并转base64"""
        try:
            if not url.startswith("http"): url = "https:" + url
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers={"Referer":"https://www.bilibili.com"}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.read()
                        return base64.b64encode(data).decode()
        except Exception as e: logger.error(f"[BiliBot] 图片下载失败: {e}")
        return None
    async def _get_comment_images(self, oid, rpid, comment_type):
        """获取评论中的图片URL列表"""
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/v2/reply/detail", params={"oid":oid,"type":comment_type,"root":rpid})
            if d["code"] != 0: return []
            content = d.get("data",{}).get("root",{}).get("content",{})
            pictures = content.get("pictures",[])
            return [p["img_src"] for p in pictures if "img_src" in p]
        except: return []
    async def _recognize_images(self, image_urls):
        """用视觉模型识别评论中的图片"""
        if not image_urls: return ""
        client = self._get_image_vision_client()
        model = self.config.get("IMAGE_VISION_MODEL","")
        if not client or not model: return ""
        try:
            content = []
            for url in image_urls[:3]:
                b64 = await self._fetch_image_base64(url)
                if b64:
                    content.append({"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}})
            if not content: return ""
            content.append({"type":"text","text":"请用50字以内描述这些图片的内容。"})
            result = self._vision_call(client, model, content, max_tokens=100)
            return result or ""
        except Exception as e:
            logger.error(f"[BiliBot] 图片识别失败: {e}"); return ""

    # ===== 好感度 =====
    def _get_level(self, score, mid=None):
        if str(mid)==str(self.config.get("OWNER_MID","")): return "special"
        if score<=-10: return "cold"
        if score>=51: return "close"
        if score>=31: return "friend"
        if score>=11: return "normal"
        return "stranger"
    def _get_level_prompts(self):
        on = self.config.get("OWNER_NAME","") or "主人"
        return {
            "special": f"这是你的主人{on}。内心：深深的喜爱和依恋。外在：随意、自然、可以撒娇。语气：宠溺、温柔、像亲人。",
            "close":"这是你的好友（好感度高）。内心：真诚关心。外在：温柔亲近。语气：温暖、真实、可以调皮。",
            "friend":"这是熟悉的粉丝（好感度中）。内心：放松和信任。外在：自然，话变多。语气：友好、轻松、偶尔调侃。",
            "normal":"这是普通粉丝（好感度低）。保持善意，温和有礼但保持距离。语气：简洁、客气。",
            "stranger":"这是陌生人。保持礼貌和善意，简洁客气。",
            "cold":"这个人多次恶意攻击你。平静坚定划清界限，回复极简短，不恶语相向。",
        }
    def _check_milestone(self, mid, old_score, new_score, username):
        mm = {10:f"「{username}」，你对我来说不再是陌生人了哦。",30:f"不知不觉就和「{username}」变熟了呢。",50:f"「{username}」...我们算是好朋友了吧？",80:f"能和「{username}」走到这一步，我挺开心的。",99:f"「{username}」，你是我最重要的人之一。"}
        triggered = self._load_json(MILESTONE_FILE, {})
        um = triggered.get(str(mid), [])
        for t,msg in mm.items():
            if old_score < t <= new_score and t not in um:
                um.append(t); triggered[str(mid)]=um; self._save_json(MILESTONE_FILE, triggered)
                logger.info(f"[BiliBot] 🏆 里程碑！{username} 达到 {t} 分"); return msg
        return None
    @staticmethod
    def _is_blocked(text): return any(kw in text for kw in BLOCK_KEYWORDS)
    async def _block_user(self, mid):
        try:
            d, _ = await self._http_post("https://api.bilibili.com/x/relation/modify", data={"fid":mid,"act":5,"re_src":11,"csrf":self.config.get("BILI_JCT","")})
            return d["code"]==0
        except: return False
    def _log_security_event(self, event_type, mid, username, content, detail):
        logs = self._load_json(SECURITY_LOG_FILE, [])
        logs.append({"time":datetime.now().strftime("%Y-%m-%d %H:%M"),"type":event_type,"uid":str(mid),"username":username,"content":content[:200],"detail":detail})
        self._save_json(SECURITY_LOG_FILE, logs[-500:])

    # ===== 用户画像 =====
    def _get_user_profile_context(self, mid):
        profiles = self._load_json(USER_PROFILE_FILE, {})
        p = profiles.get(str(mid))
        if not p: return ""
        entries = []
        if p.get("username"): entries.append(f"昵称：{p['username']}")
        if p.get("facts"):
            for f in p["facts"][-10:]: entries.append(f)
        if p.get("tags"): entries.append("标签：" + "、".join(p["tags"]))
        if p.get("impression"): entries.append(f"印象：{p['impression']}")
        return "【对该用户的了解】\n" + "\n".join(entries) if entries else ""
    def _update_user_profile(self, mid, username=None, impression=None, new_facts=None, new_tags=None):
        profiles = self._load_json(USER_PROFILE_FILE, {})
        uid = str(mid)
        if uid not in profiles: profiles[uid] = {"username":"","impression":"","facts":[],"tags":[]}
        if username and not profiles[uid].get("username"): profiles[uid]["username"] = username
        if impression: profiles[uid]["impression"] = impression
        if new_facts:
            ex = profiles[uid].get("facts",[])
            for f in new_facts:
                f=f.strip()
                if f and f not in ex: ex.append(f)
            profiles[uid]["facts"] = ex[-20:]
        if new_tags:
            et = profiles[uid].get("tags",[])
            for t in new_tags:
                t=t.strip()
                if t and t not in et: et.append(t)
            profiles[uid]["tags"] = et[-10:]
        self._save_json(USER_PROFILE_FILE, profiles)

    # ===== 心情 =====
    def _get_today_mood(self):
        if not self.config.get("ENABLE_MOOD",True): return "🌙 平静如常",""
        md = self._load_json(MOOD_FILE, {})
        today = datetime.now().strftime("%Y-%m-%d")
        if md.get("date")==today: return md["mood"],md["mood_prompt"]
        moods = [("☀️ 心情不错","语气稍微轻快一点。"),("🌙 平静如常","按正常性格回复。"),("🌧️ 有点安静","话少一点。"),("😏 有点皮","偶尔多一点调侃。"),("🧊 懒得废话","回复更简洁。")]
        mood,mp = random.choice(moods)
        self._save_json(MOOD_FILE, {"date":today,"mood":mood,"mood_prompt":mp})
        return mood,mp
    def _get_festival_prompt(self):
        today = datetime.now().strftime("%m-%d")
        try:
            from lunardate import LunarDate
            l = LunarDate.fromSolarDate(datetime.now().year,datetime.now().month,datetime.now().day)
            lunar_md = f"{l.month:02d}-{l.day:02d}"
        except: lunar_md = ""
        fests = {"01-01":"今天是元旦！语气温暖。","02-14":"今天是情人节。","04-01":"今天是愚人节！可以开小玩笑。","05-01":"今天是劳动节。","10-31":"今天是万圣节，语气神秘。","12-25":"今天是圣诞节，语气温柔。","12-31":"今天是跨年夜。"}
        lfests = {"01-01":"今天是春节！热情说新年快乐。","01-15":"今天是元宵节。","05-05":"今天是端午节。","08-15":"今天是中秋节。"}
        return fests.get(today,"") or lfests.get(lunar_md,"")

    # ===== 性格演化系统 =====
    def _get_personality_prompt(self):
        evo = self._load_json(PERSONALITY_FILE, {})
        if not evo: return ""
        parts = []
        traits = evo.get("evolved_traits", [])
        if traits:
            parts.append("【最近的成长变化】")
            for t in traits[-3:]: parts.append(f"- {t['change']}")
        habits = evo.get("speech_habits", [])
        if habits: parts.append("【当前说话习惯】" + "；".join(habits))
        opinions = evo.get("opinions", [])
        if opinions: parts.append("【对事物的看法】" + "；".join(opinions))
        return "\n".join(parts) if parts else ""

    @staticmethod
    def _parse_evolve_json(raw_text, old_habits, old_opinions):
        text = raw_text.replace("```json","").replace("```","").strip()
        try: return json.loads(text)
        except: pass
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try: return json.loads(m.group())
            except: pass
        json_start = text.find('{')
        if json_start != -1:
            fragment = text[json_start:]
            ob = fragment.count('{') - fragment.count('}')
            oq = fragment.count('[') - fragment.count(']')
            fragment = re.sub(r',?\s*"[^"]*$', '', fragment)
            fragment = re.sub(r',\s*$', '', fragment)
            fragment += ']' * max(0, oq) + '}' * max(0, ob)
            try: return json.loads(fragment)
            except: pass
        logger.warning(f"[BiliBot] 性格演化JSON解析失败：{raw_text[:200]}")
        reflection = ""
        rm = re.search(r'"reflection"\s*:\s*"([^"]*)"', text)
        if rm: reflection = rm.group(1)
        return {"new_trait":"","trigger":"","speech_habits":old_habits,"opinions":old_opinions,"reflection":reflection or "今天的反思没能整理好..."}

    async def _maybe_evolve_personality(self):
        if not self.config.get("ENABLE_PERSONALITY_EVOLUTION", True): return
        evo = self._load_json(PERSONALITY_FILE, {})
        today = datetime.now().strftime("%Y-%m-%d")
        if evo.get("last_evolve","")[:10] == today: return
        evolve_hour = self.config.get("EVOLVE_HOUR", 1)
        if datetime.now().hour != evolve_hour: return
        logger.info("[BiliBot] 🌱 开始每日性格演化反思...")
        recent = sorted(self._memory, key=lambda x: x.get("time",""), reverse=True)[:30]
        if len(recent) < 5:
            logger.info("[BiliBot] 🌱 记忆太少，跳过演化"); return
        recent_texts = "\n".join([m["text"] for m in recent[:20]])
        old_traits = evo.get("evolved_traits", [])
        old_habits = evo.get("speech_habits", [])
        old_opinions = evo.get("opinions", [])
        sp = self._get_system_prompt()
        on = self.config.get("OWNER_NAME","") or "主人"
        default_evolve_prompt = """现在是睡前反思时间。请根据你最近的互动经历，思考自己有没有发生什么变化。

【之前已经发生的变化】
{old_traits}

【当前说话习惯】
{old_habits}

【当前对事物的看法】
{old_opinions}

【最近的互动记录】
{recent_texts}

请思考：
1. 最近的经历有没有让你的语气或说话方式产生微妙变化？
2. 有没有形成新的说话习惯？
3. 对什么事物产生了新的看法？

注意：变化应该是微妙的、渐进的，不要突变。如果没什么变化就如实说。

请以JSON格式回复：
{{"new_trait": "新的变化描述（没有就留空）", "trigger": "什么触发了这个变化", "speech_habits": ["当前所有说话习惯，含旧的，最多5条"], "opinions": ["当前所有看法，含旧的，最多5条"], "reflection": "一句话的睡前感想"}}"""
        custom_prompt = self.config.get("EVOLVE_PROMPT", "").strip()
        tpl = custom_prompt if custom_prompt else default_evolve_prompt
        prompt = tpl.format(
            old_traits=json.dumps(old_traits[-5:], ensure_ascii=False) if old_traits else "暂无",
            old_habits=json.dumps(old_habits, ensure_ascii=False) if old_habits else "暂无",
            old_opinions=json.dumps(old_opinions, ensure_ascii=False) if old_opinions else "暂无",
            recent_texts=recent_texts,
            owner_name=on
        )
        for attempt in range(3):
            try:
                text = await self._llm_call(prompt, system_prompt=sp, max_tokens=1024)
                if not text: raise ValueError("LLM返回空")
                result = self._parse_evolve_json(text, old_habits, old_opinions)
                if not result.get("new_trait") and result.get("reflection") == "今天的反思没能整理好...":
                    raise ValueError(f"JSON解析兜底：{text[:100]}")
                new_trait = result.get("new_trait","")
                if new_trait:
                    old_traits.append({"time":today,"change":new_trait,"trigger":result.get("trigger","")})
                    old_traits = old_traits[-10:]
                evo = {
                    "version": evo.get("version",0)+1,
                    "last_evolve": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "evolved_traits": old_traits,
                    "speech_habits": result.get("speech_habits",old_habits)[-5:],
                    "opinions": result.get("opinions",old_opinions)[-5:],
                    "last_reflection": result.get("reflection","")
                }
                self._save_json(PERSONALITY_FILE, evo)
                if new_trait: logger.info(f"[BiliBot] 🌱 性格演化：{new_trait}")
                else: logger.info("[BiliBot] 🌱 今日无明显变化")
                logger.info(f"[BiliBot] 🌱 反思：{result.get('reflection','')}")
                return
            except Exception as e:
                logger.warning(f"[BiliBot] 性格演化失败（第{attempt+1}/3次）：{e}")
                if attempt < 2: await asyncio.sleep(30)
        evo["last_evolve"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._save_json(PERSONALITY_FILE, evo)
        logger.error("[BiliBot] 🌱 性格演化连续3次失败，今日放弃")

    # ===== 记忆系统 =====
    def _save_memory_record(self, rpid, thread_id, user_id, username, content, reply_text, source="bilibili"):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        text = f"[{now}] 用户{user_id}({username})说：{content} | Bot回复：{reply_text}"
        emb = self._get_embedding(text)
        rec = {"rpid":str(rpid),"thread_id":str(thread_id),"user_id":str(user_id),"time":now,"text":text,"source":source}
        if emb: rec["embedding"]=emb
        self._memory.append(rec); self._save_json(MEMORY_FILE, self._memory)
    def _get_thread_memories(self, thread_id):
        docs = [m for m in self._memory if m.get("thread_id")==str(thread_id)]
        docs.sort(key=lambda x:x.get("time",""))
        return [m["text"] for m in docs]
    def _get_user_semantic_memories(self, user_id, query_text):
        um = [m for m in self._memory if m.get("user_id")==str(user_id) and not m.get("text","").startswith("[记忆压缩]") and "embedding" in m]
        # 也搜QQ记忆
        qq_mem = self._load_json(QQ_MEMORY_FILE, [])
        um += [m for m in qq_mem if m.get("user_id")==str(user_id) and "embedding" in m]
        if not um: return []
        qe = self._get_embedding(query_text)
        if not qe: return []
        scored = [(self._cosine_similarity(qe, m["embedding"]), m["text"]) for m in um]
        scored.sort(reverse=True)
        return [t for s,t in scored[:MAX_SEMANTIC_RESULTS] if s>0.6]
    def _search_memories(self, query_text, limit=5, source=None):
        cands = list(self._memory)
        # 合并QQ记忆
        if source != "bilibili":
            qq_mem = self._load_json(QQ_MEMORY_FILE, [])
            cands += qq_mem
        if source: cands = [m for m in cands if m.get("source")==source]
        cands = [m for m in cands if "embedding" in m]
        if not cands: return []
        qe = self._get_embedding(query_text)
        if not qe: return []
        scored = [(self._cosine_similarity(qe,m["embedding"]),m) for m in cands]
        scored.sort(reverse=True)
        results = []
        for s,m in scored[:limit]:
            if s>0.5:
                tag = f"[{m.get('source','?')}]" if not source else ""
                results.append(f"{tag}{m['text']}")
        return results
    async def _compress_user_memory(self, user_id, username):
        um = [m for m in self._memory if m.get("user_id")==str(user_id)]
        if len(um) <= USER_MEMORY_COMPRESS_THRESHOLD: return
        logger.info(f"[BiliBot] 🗜️ {username} 记忆达 {len(um)} 条，压缩...")
        um.sort(key=lambda x:x.get("time","")); old = um[:-USER_MEMORY_KEEP_RECENT]
        old_texts = "\n".join([m["text"] for m in old])
        prompt = f'请根据以下与用户"{username}"的历史互动，完成：\n1. 总结（100字以内）\n2. 3-5个标签\n3. 提取用户个人信息\n\n历史：\n{old_texts[:3000]}\n\nJSON格式：{{"summary":"","tags":[],"user_facts":[]}}'
        try:
            text = await self._llm_call(prompt, max_tokens=400)
            if not text: return
            text = text.replace("```json","").replace("```","").strip()
            try: result = json.loads(text)
            except:
                m = re.search(r'\{.*\}', text, re.DOTALL)
                result = json.loads(m.group()) if m else {"summary":text[:100],"tags":[],"user_facts":[]}
            self._update_user_profile(user_id, impression=result.get("summary") or None, new_facts=result.get("user_facts") or None, new_tags=result.get("tags") or None)
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            emb = self._get_embedding(result.get("summary",""))
            comp = {"rpid":f"compressed_{int(datetime.now().timestamp())}","thread_id":"compressed","user_id":str(user_id),"time":now,"text":f"[记忆压缩] {result.get('summary','')}","source":"bilibili"}
            if emb: comp["embedding"]=emb
            old_rpids = {m["rpid"] for m in old}
            self._memory = [m for m in self._memory if m.get("rpid") not in old_rpids]
            self._memory.append(comp); self._save_json(MEMORY_FILE, self._memory)
            logger.info(f"[BiliBot] 🗜️ 压缩完成：{len(old)} 条 → 1 条")
        except Exception as e: logger.error(f"[BiliBot] 记忆压缩失败：{e}")
    async def _build_memory_context(self, thread_id, user_id, query_text, oid=0, comment_type=1):
        """
        完整记忆调取逻辑：
        必定注入：永久记忆、用户画像
        视频评论区：视频信息+总结、UP主印象（非自己）、UP主相关记忆
        评论线上下文：同thread最近10条
        用户语义记忆：优先该UID，再补通用相关
        """
        parts = []
        bot_mid = self.config.get("DEDE_USER_ID","")
        video_cache_entry = None

        # 1. 永久记忆
        perm = self._load_json(PERMANENT_MEMORY_FILE, [])
        if perm:
            parts.append("【Bot的自我认知】\n" + "\n".join([f"[{p.get('time','?')}] {p['text']}" for p in perm[-20:]]))

        # 2. 用户画像（词条式）
        upc = self._get_user_profile_context(user_id)
        if upc: parts.append(upc)

        # 3. 视频评论区场景
        if comment_type == 1 and oid:
            vc, cache_entry = await self._get_video_context(oid, comment_type)
            video_cache_entry = cache_entry
            if vc: parts.append(vc)
            # UP主印象（不是自己的视频才注入）
            if cache_entry:
                up_mid = str(cache_entry.get("owner_mid",""))
                if up_mid and up_mid != bot_mid:
                    up_profile = self._get_user_profile_context(up_mid)
                    if up_profile:
                        parts.append(up_profile.replace("【对该用户的了解】","【该视频UP主的了解】"))
                    # UP主相关语义记忆
                    up_mems = self._get_user_semantic_memories(up_mid, query_text)
                    if up_mems:
                        parts.append("【与该UP主的历史互动】\n" + "\n".join(up_mems))

        # 4. 评论线上下文（最近10条）
        td = self._get_thread_memories(thread_id)
        if td:
            parts.append("【本评论线对话】\n" + "\n".join(td[-10:]))

        # 5. 用户语义记忆（优先该UID相关）
        user_mems = self._get_user_semantic_memories(user_id, query_text)
        if user_mems:
            parts.append("【与该用户的相关记忆】\n" + "\n".join(user_mems))
        else:
            # 没有该用户的记忆，搜全局相关
            general_mems = self._search_memories(query_text, limit=3)
            if general_mems:
                parts.append("【相关历史记忆】\n" + "\n".join(general_mems))

        return "\n\n".join(parts) if parts else ""

    # ===== 视频信息 =====
    async def _oid_to_bvid(self, oid):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/view", params={"aid":oid})
            if d["code"]==0: return d["data"].get("bvid","")
        except: pass
        return ""
    async def _get_video_info(self, oid):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/view", params={"aid":oid})
            if d["code"]==0:
                v=d["data"]
                return {"bvid":v.get("bvid",""),"title":v.get("title",""),"desc":v.get("desc",""),"owner_name":v.get("owner",{}).get("name",""),"owner_mid":v.get("owner",{}).get("mid",""),"tname":v.get("tname",""),"duration":v.get("duration",0),"pic":v.get("pic","")}
        except Exception as e: logger.error(f"[BiliBot] 获取视频信息失败：{e}")
        return None
    async def _analyze_video_with_vision(self, video_info):
        """用视觉模型分析视频封面+信息，生成内容概括"""
        client = self._get_video_vision_client()
        model = self.config.get("VIDEO_VISION_MODEL","")
        dur_min = video_info.get("duration",0) // 60
        dur_sec = video_info.get("duration",0) % 60
        text_prompt = f"""请根据以下B站视频信息，写一段简洁的内容概括（150字以内），包括：这个视频大概在讲什么、是什么类型/风格、可能的受众。

视频标题：{video_info.get('title','未知')}
UP主：{video_info.get('owner_name','未知')}
分区：{video_info.get('tname','未知')}
时长：{dur_min}分{dur_sec}秒
简介：{video_info.get('desc','无')[:500]}

直接输出概括内容，不要加前缀。"""
        # 尝试用视觉模型 + 封面
        if client and model and video_info.get("pic"):
            try:
                b64 = await self._fetch_image_base64(video_info["pic"])
                if b64:
                    content = [{"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}},{"type":"text","text":text_prompt}]
                    result = self._vision_call(client, model, content, max_tokens=250)
                    if result: return result
            except Exception as e: logger.warning(f"[BiliBot] 视觉分析封面失败: {e}")
        # 回退：纯文本LLM分析
        result = await self._llm_call(text_prompt, max_tokens=250)
        return result or f"视频《{video_info.get('title','未知')}》，UP主：{video_info.get('owner_name','未知')}，分区：{video_info.get('tname','未知')}。简介：{video_info.get('desc','无')[:100]}"
    async def _get_video_context(self, oid, comment_type):
        """获取视频上下文：基础信息 + 内容总结（有缓存用缓存，没有就分析）"""
        if comment_type != 1: return "", None
        vc = self._load_json(VIDEO_MEMORY_FILE, {})
        bvid = await self._oid_to_bvid(oid)
        if not bvid: return "", None
        # 有缓存直接用
        if bvid in vc:
            c = vc[bvid]
            ctx = f"【当前视频】\n标题：{c['title']}\nUP主：{c['owner_name']}（UID:{c.get('owner_mid','')})）\n分区：{c.get('tname','')}\n简介：{c.get('desc','')[:150]}\n内容概括：{c.get('analysis','')}"
            return ctx, c
        # 没缓存，获取视频信息并分析
        vi = await self._get_video_info(oid)
        if not vi: return "", None
        logger.info(f"[BiliBot] 📹 新视频，分析中：《{vi['title']}》by {vi['owner_name']}")
        analysis = await self._analyze_video_with_vision(vi)
        logger.info(f"[BiliBot] 📹 分析结果：{analysis[:60]}...")
        cache_entry = {"title":vi["title"],"desc":vi.get("desc","")[:200],"owner_name":vi["owner_name"],"owner_mid":str(vi["owner_mid"]),"tname":vi["tname"],"analysis":analysis,"time":datetime.now().strftime("%Y-%m-%d %H:%M")}
        vc[bvid] = cache_entry; self._save_json(VIDEO_MEMORY_FILE, vc)
        ctx = f"【当前视频】\n标题：{vi['title']}\nUP主：{vi['owner_name']}（UID:{vi['owner_mid']}）\n分区：{vi['tname']}\n简介：{vi.get('desc','')[:150]}\n内容概括：{analysis}"
        return ctx, cache_entry

    # ===== Cookie管理 =====
    async def check_cookie(self):
        s = self.config.get("SESSDATA","")
        if not s: return False,"SESSDATA 为空"
        try:
            d, _ = await self._http_get(BILI_NAV_URL)
            if d["code"]==0: return True,f"✅ {d['data'].get('uname','?')} (UID:{d['data'].get('mid','')}) LV{d['data'].get('level_info',{}).get('current_level',0)}"
            return False,f"❌ Cookie 已失效 (code:{d['code']})"
        except Exception as e: return False,f"❌ 检查失败: {e}"
    async def check_need_refresh(self):
        try:
            d, _ = await self._http_get(BILI_COOKIE_INFO_URL, params={"csrf":self.config.get("BILI_JCT","")})
            if d["code"]!=0: return False,f"检查失败: {d.get('message','')}"
            return (True,"需要刷新") if d["data"].get("refresh",False) else (False,"Cookie 仍然有效")
        except Exception as e: return False,f"检查出错: {e}"
    def _generate_correspond_path(self, ts):
        from cryptography.hazmat.primitives.asymmetric import padding; from cryptography.hazmat.primitives import hashes, serialization
        pk = serialization.load_pem_public_key(BILI_RSA_PUBLIC_KEY.encode())
        return pk.encrypt(f"refresh_{ts}".encode(), padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),algorithm=hashes.SHA256(),label=None)).hex()
    async def refresh_cookie(self):
        rt=self.config.get("REFRESH_TOKEN","")
        if not rt: return False,"没有 REFRESH_TOKEN"
        bjct=self.config.get("BILI_JCT","")
        if not self.config.get("SESSDATA",""): return False,"SESSDATA 为空"
        try:
            need,msg = await self.check_need_refresh()
            if not need: return True,msg
            cp = self._generate_correspond_path(int(time.time()*1000))
            html, _ = await self._http_get_text(f"https://www.bilibili.com/correspond/1/{cp}")
            m = re.search(r'<div\s+id="1-name"\s*>([^<]+)</div>', html)
            if not m: return False,"无法提取 refresh_csrf"
            async with aiohttp.ClientSession() as s:
                async with s.post(BILI_COOKIE_REFRESH_URL, headers=self._headers(), data={"csrf":bjct,"refresh_csrf":m.group(1).strip(),"source":"main_web","refresh_token":rt}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    result = await resp.json(content_type=None)
                    if result["code"]!=0: return False,f"刷新失败: {result.get('message',result['code'])}"
                    updates={}
                    nrt=result["data"].get("refresh_token","")
                    if nrt: updates["REFRESH_TOKEN"]=nrt
                    for k,cookie in resp.cookies.items():
                        if k=="SESSDATA": updates["SESSDATA"]=cookie.value
                        elif k=="bili_jct": updates["BILI_JCT"]=cookie.value
                        elif k=="DedeUserID": updates["DEDE_USER_ID"]=cookie.value
            if "SESSDATA" not in updates: return False,"刷新响应中未找到新 SESSDATA"
            try:
                ch=dict(self._headers()); ch["Cookie"]=f"SESSDATA={updates['SESSDATA']}; bili_jct={updates.get('BILI_JCT',bjct)}"
                await self._http_post(BILI_COOKIE_CONFIRM_URL, headers=ch, data={"csrf":updates.get("BILI_JCT",bjct),"refresh_token":rt})
            except: pass
            for k,v in updates.items(): self.config[k]=v
            self.config.save_config()
            return True,f"✅ Cookie 刷新成功！"
        except Exception as e: return False,f"刷新出错: {e}"

    # ===== WBI签名 =====
    async def _get_wbi_keys(self):
        d, _ = await self._http_get(BILI_NAV_URL); d=d["data"]["wbi_img"]
        return d["img_url"].rsplit("/",1)[1].split(".")[0], d["sub_url"].rsplit("/",1)[1].split(".")[0]
    def _get_mixin_key(self, orig): return reduce(lambda s,i:s+orig[i], MIXIN_KEY_ENC_TAB, "")[:32]
    async def sign_wbi_params(self, params):
        try:
            ik,sk=await self._get_wbi_keys(); mk=self._get_mixin_key(ik+sk); params["wts"]=int(time.time()); params=dict(sorted(params.items()))
            params["w_rid"]=hashlib.md5(("&".join(f"{k}={v}" for k,v in params.items())+mk).encode()).hexdigest(); return params
        except: return params

    # ===== 扫码登录 =====
    async def _qr_login_generate(self):
        try:
            d, _ = await self._http_get(BILI_QR_GENERATE_URL, headers={"User-Agent":USER_AGENT})
            if d["code"]==0: return d["data"]["url"],d["data"]["qrcode_key"]
        except Exception as e: logger.error(f"生成二维码失败: {e}")
        return None,None
    async def _qr_login_poll(self, qrcode_key):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(BILI_QR_POLL_URL, params={"qrcode_key":qrcode_key}, headers={"User-Agent":USER_AGENT}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    d_full = await resp.json(content_type=None)
                    d=d_full["data"]; code=d["code"]
                    mm={0:"登录成功",86038:"二维码已失效",86090:"已扫码，请在手机上确认",86101:"等待扫码中..."}
                    cookies={}
                    if code==0:
                        url=d.get("url",""); rt=d.get("refresh_token","")
                        if url:
                            from urllib.parse import urlparse,parse_qs; p=parse_qs(urlparse(url).query)
                            cookies={"SESSDATA":p.get("SESSDATA",[""])[0],"bili_jct":p.get("bili_jct",[""])[0],"DedeUserID":p.get("DedeUserID",[""])[0],"REFRESH_TOKEN":rt}
                        for k,cookie in resp.cookies.items():
                            if k in ("SESSDATA","bili_jct","DedeUserID"): cookies[k]=cookie.value
                    return code, mm.get(code,f"未知({code})"), cookies
        except Exception as e: return -1,f"轮询失败: {e}",{}

    # ===== LLM =====
    async def _llm_call(self, prompt, system_prompt="", max_tokens=300):
        try:
            pid = self.config.get("LLM_PROVIDER_ID","")
            if not pid:
                logger.warning("[BiliBot] 未配置 LLM_PROVIDER_ID，请在插件设置中选择模型")
                return None
            full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
            resp = await self.context.llm_generate(chat_provider_id=pid, prompt=full_prompt)
            return resp.completion_text.strip() if resp and resp.completion_text else None
        except Exception as e: logger.error(f"[BiliBot] LLM 调用失败: {e}"); return None
    def _get_system_prompt(self):
        if self.config.get("USE_ASTRBOT_PERSONA",True):
            try:
                persona = self.context.persona_manager.get_default_persona_v3()
                if persona: return persona["prompt"]
            except: pass
        return self.config.get("CUSTOM_SYSTEM_PROMPT","你是一个B站UP主的AI助手。")
    async def _generate_reply(self, content, mid, username, thread_id, oid, comment_type, image_desc=""):
        try:
            sp = self._get_system_prompt(); on = self.config.get("OWNER_NAME","") or "主人"
            is_owner = str(mid)==str(self.config.get("OWNER_MID",""))
            cs = self._affection.get(str(mid),0); lv = self._get_level(cs, mid)
            lp = self._get_level_prompts()[lv]
            mc = await self._build_memory_context(thread_id, mid, content, oid=oid, comment_type=comment_type)
            ms = f"\n\n【记忆参考】\n{mc}" if mc else ""
            mood,mp = self._get_today_mood(); fest = self._get_festival_prompt()
            fs = f"\n特殊日期：{fest}" if fest else ""
            pp = self._get_personality_prompt()
            pps = f"\n{pp}" if pp else ""
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            comment_text = content
            if image_desc: comment_text += f"\n[用户发送了图片，内容是：{image_desc}]"
            prompt = f"""{lp}{pps}\n\n【底线】拒绝：表白暧昧、引战、黄赌毒政治。\n\n【今日状态】{mood} — {mp}{fs}\n\n当前时间：{now}{ms}\n\n「{username}」{'（这是'+on+'）' if is_owner else ''}的评论：「{comment_text}」\n\n请以JSON格式回复：\n{{"score_delta": 数字, "reply": "回复内容", "impression": "印象", "user_facts": ["个人信息"], "permanent_memory": "永久记忆(没有则留空)"}}\n\nscore_delta：友善+2，普通+1，不友善-2，辱骂-5。reply不超过50字。"""
            rt = await self._llm_call(prompt, system_prompt=sp)
            if not rt: return None
            rt = rt.replace("```json","").replace("```","").strip()
            r = None
            try: r = json.loads(rt)
            except: pass
            if r is None:
                m = re.search(r'\{.*\}', rt, re.DOTALL)
                if m:
                    try: r = json.loads(m.group())
                    except: pass
            if r is None or not isinstance(r, dict):
                rm = re.search(r'"reply"\s*:\s*"([^"]*)"', rt)
                reply_text = rm.group(1) if rm else rt[:50]
                r = {"score_delta":1,"reply":reply_text,"impression":"","user_facts":[],"permanent_memory":""}
                logger.warning(f"[BiliBot] JSON解析失败，使用兜底回复: {reply_text[:30]}")
            return {"score_delta":r.get("score_delta",1),"reply":r.get("reply",""),"impression":r.get("impression",""),"user_facts":r.get("user_facts",[]),"permanent_memory":r.get("permanent_memory","")}
        except Exception as e: logger.error(f"[BiliBot] 回复生成失败: {e}\n{traceback.format_exc()}"); return None

    # ===== B站API =====
    async def _send_reply(self, oid, rpid, reply_type, content):
        try:
            d, _ = await self._http_post(BILI_REPLY_URL, data={"oid":oid,"type":reply_type,"root":rpid,"parent":rpid,"message":content,"csrf":self.config.get("BILI_JCT","")})
            if d["code"]==0: return True
            elif d["code"]==-101: logger.error("[BiliBot] SESSDATA 失效！")
            elif d["code"]==-111: logger.error("[BiliBot] bili_jct 错误！")
            else: logger.warning(f"[BiliBot] 回复失败: {d.get('message',d['code'])}")
            return False
        except Exception as e: logger.error(f"[BiliBot] 回复出错: {e}"); return False
    async def get_followings(self, mid=None):
        target = mid or self.config.get("DEDE_USER_ID","")
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/relation/followings", params={"vmid":target,"ps":50,"pn":1})
            if d["code"]==0: return [i["mid"] for i in d.get("data",{}).get("list",[])]
        except Exception as e: logger.error(f"[BiliBot] 获取关注列表失败: {e}")
        return []

    # ===== 主动视频系统 =====
    PREFERRED_TIDS = [17, 160, 211, 3, 13, 167, 321, 36, 129]  # 游戏/生活/美食/音乐/番剧/同人/vup/科技/绘画

    def _generate_daily_schedule(self):
        n_times = self.config.get("PROACTIVE_TIMES_COUNT", 2)
        times = sorted(random.sample(range(10, 23), min(n_times, 12)))
        times = [(h, random.randint(0, 59)) for h in times]
        schedule = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "proactive_times": [f"{h}:{m:02d}" for h, m in times],
            "proactive_triggered": [],
        }
        self._save_json(SCHEDULE_FILE, schedule)
        return times, set()

    def _load_or_generate_schedule(self):
        try:
            schedule = self._load_json(SCHEDULE_FILE, {})
            if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
                times = []
                for t in schedule.get("proactive_times", []):
                    h, m = t.split(":"); times.append((int(h), int(m)))
                triggered = set(schedule.get("proactive_triggered", []))
                return times, triggered
        except: pass
        return self._generate_daily_schedule()

    def _save_schedule_state(self, times, triggered):
        schedule = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "proactive_times": [f"{h}:{m:02d}" for h, m in times],
            "proactive_triggered": list(triggered),
        }
        self._save_json(SCHEDULE_FILE, schedule)

    # ===== 动态发布功能 =====
    def _get_image_gen_config(self):
        """获取图片生成模型配置"""
        api_key = self.config.get("IMAGE_GEN_API_KEY", "") or self.config.get("VIDEO_VISION_API_KEY", "")
        base_url = self.config.get("IMAGE_GEN_API_BASE", "https://openrouter.ai/api/v1")
        model = self.config.get("IMAGE_GEN_MODEL", "black-forest-labs/flux-schnell")
        return api_key, base_url, model

    async def _generate_image(self, prompt):
        """用图片模型生成图片，返回本地文件路径"""
        api_key, base_url, model = self._get_image_gen_config()
        if not api_key:
            logger.warning("[BiliBot] 图片生成模型未配置")
            return None
        styled_prompt = f"anime style illustration, not photorealistic, soft lighting, beautiful colors: {prompt}"
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": [{"role": "user", "content": styled_prompt}], "modalities": ["image"]}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120)) as r:
                    if r.status != 200:
                        logger.error(f"[BiliBot] 图片生成HTTP错误: {r.status}")
                        return None
                    data = await r.json()
            if "error" in data:
                logger.error(f"[BiliBot] 图片生成API错误: {data['error']}")
                return None
            message = data.get("choices", [{}])[0].get("message", {})
            images = message.get("images", [])
            if images:
                img_item = images[0]
                if isinstance(img_item, dict):
                    img_url = img_item.get("url", "") or img_item.get("b64_json", "") or (img_item.get("image_url", {}) or {}).get("url", "")
                else:
                    img_url = str(img_item)
                if img_url.startswith("data:image"):
                    img_b64 = img_url.split(",", 1)[1]
                    img_data = base64.b64decode(img_b64)
                    save_path = os.path.join(TEMP_IMAGE_DIR, f"dynamic_{int(time.time())}.png")
                    with open(save_path, "wb") as f: f.write(img_data)
                    logger.info(f"[BiliBot] 🖼️ 图片生成成功（{len(img_data)//1024}KB）")
                    return save_path
            content = message.get("content", "")
            if isinstance(content, str) and "data:image" in content:
                match = re.search(r'data:image/\w+;base64,([A-Za-z0-9+/=]+)', content)
                if match:
                    img_data = base64.b64decode(match.group(1))
                    save_path = os.path.join(TEMP_IMAGE_DIR, f"dynamic_{int(time.time())}.png")
                    with open(save_path, "wb") as f: f.write(img_data)
                    logger.info(f"[BiliBot] 🖼️ 图片生成成功（{len(img_data)//1024}KB）")
                    return save_path
            logger.warning("[BiliBot] 图片生成返回无图片")
            return None
        except Exception as e:
            logger.error(f"[BiliBot] 图片生成异常: {e}")
            return None

    async def _generate_dynamic_content(self):
        """生成动态文案和可选的图片描述"""
        perm = self._load_json(PERMANENT_MEMORY_FILE, [])
        perm_section = ""
        if perm:
            perm_section = "\n【你的自我认知】\n" + "\n".join([p["text"] for p in perm[-20:]])
        history_log = self._load_json(DYNAMIC_LOG_FILE, [])
        history_section = ""
        if history_log:
            recent_dynamics = [h.get("text", "") for h in history_log[-10:] if h.get("text")]
            if recent_dynamics:
                history_section = "\n【最近发过的动态（不要重复类似内容）】\n" + "\n".join([f"- {d[:50]}..." if len(d) > 50 else f"- {d}" for d in recent_dynamics])
        now = datetime.now()
        hour = now.hour
        if hour < 6: time_hint = "现在是深夜/凌晨"
        elif hour < 12: time_hint = "现在是上午"
        elif hour < 18: time_hint = "现在是下午"
        else: time_hint = "现在是晚上"
        custom_topics = self.config.get("DYNAMIC_TOPICS", [])
        topics = custom_topics if custom_topics and isinstance(custom_topics, list) else DEFAULT_DYNAMIC_TOPICS
        topic = random.choice(topics)
        sp = self._get_system_prompt()
        prompt = f"""{sp}{perm_section}

{time_hint}，你想发一条B站动态。主题方向：{topic}{history_section}

风格要求：
- 说话自然有网感，像真人发的动态
- 结合当前时间（{time_hint}）写出真实感
- 不要和最近发过的动态内容重复或相似

请以JSON格式回复：
{{"text": "动态文案（50-150字，自然随意）", "need_image": true或false, "image_prompt": "如果need_image为true，写一段英文图片描述用于AI生图，否则留空"}}

注意：动态文案要有个性，不要像AI写的。不是每次都需要图片。"""
        try:
            text = await self._llm_call(prompt, max_tokens=500)
            if not text: return None
            text = text.replace("```json", "").replace("```", "").strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                match = re.search(r'\{.*\}', text, re.DOTALL)
                if match:
                    try: return json.loads(match.group())
                    except: pass
            logger.warning(f"[BiliBot] 动态内容JSON解析失败: {text[:100]}")
            return None
        except Exception as e:
            logger.error(f"[BiliBot] 生成动态内容失败: {e}")
            return None

    async def _upload_image_to_bilibili(self, image_path):
        """上传图片到B站图床"""
        try:
            with open(image_path, "rb") as f: img_data = f.read()
            form = aiohttp.FormData()
            form.add_field('file_up', img_data, filename='image.png', content_type='image/png')
            form.add_field('category', 'daily')
            form.add_field('csrf', self.config.get("BILI_JCT", ""))
            headers = {"Cookie": self._headers()["Cookie"], "User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com"}
            async with aiohttp.ClientSession() as s:
                async with s.post(BILI_UPLOAD_IMAGE_URL, headers=headers, data=form, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    result = await r.json()
            if result.get("code") == 0:
                img_info = result["data"]
                logger.info("[BiliBot] 📤 图片上传成功")
                return {"img_src": img_info["image_url"], "img_width": img_info["image_width"], "img_height": img_info["image_height"], "img_size": os.path.getsize(image_path) / 1024}
            else:
                logger.warning(f"[BiliBot] 图片上传失败: {result}")
                return None
        except Exception as e:
            logger.error(f"[BiliBot] 图片上传异常: {e}")
            return None

    async def _post_dynamic_text(self, text):
        """发送纯文字动态"""
        data = {"dynamic_id": 0, "type": 4, "rid": 0, "content": text, "up_choose_comment": 0, "up_close_comment": 0,
                "extension": '{"emoji_type":1,"from":{"emoji_type":1},"flag_cfg":{}}', "at_uids": "", "ctrl": "[]",
                "csrf_token": self.config.get("BILI_JCT", ""), "csrf": self.config.get("BILI_JCT", "")}
        try:
            result, _ = await self._http_post(BILI_DYNAMIC_TEXT_URL, data=data)
            if result.get("code") == 0:
                logger.info("[BiliBot] ✅ 纯文字动态发送成功")
                return True
            else:
                logger.warning(f"[BiliBot] 动态发送失败: {result}")
                return False
        except Exception as e:
            logger.error(f"[BiliBot] 动态发送异常: {e}")
            return False

    async def _post_dynamic_with_image(self, text, img_info):
        """发送带图片的动态"""
        params = {"csrf": self.config.get("BILI_JCT", "")}
        payload = {"dyn_req": {"content": {"contents": [{"raw_text": text, "type": 1, "biz_id": ""}]}, "pics": [img_info], "scene": 2}}
        try:
            headers = {**self._headers(), "Content-Type": "application/json"}
            async with aiohttp.ClientSession() as s:
                async with s.post(BILI_DYNAMIC_IMAGE_URL, params=params, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    result = await r.json()
            if result.get("code") == 0:
                logger.info("[BiliBot] ✅ 带图动态发送成功")
                return True
            else:
                logger.warning(f"[BiliBot] 带图动态失败: {result}，尝试纯文字...")
                return await self._post_dynamic_text(text)
        except Exception as e:
            logger.error(f"[BiliBot] 带图动态异常: {e}，尝试纯文字...")
            return await self._post_dynamic_text(text)

    async def _run_dynamic(self):
        """执行一次动态发布"""
        logger.info("[BiliBot] 📢 开始发布动态...")
        log = self._load_json(DYNAMIC_LOG_FILE, [])
        today = datetime.now().strftime("%Y-%m-%d")
        today_posts = [l for l in log if l.get("time", "").startswith(today)]
        max_daily = self.config.get("DYNAMIC_DAILY_COUNT", 1)
        if len(today_posts) >= max_daily:
            logger.info(f"[BiliBot] 今天已发 {len(today_posts)} 条动态，跳过")
            return
        logger.info("[BiliBot] 🤔 正在想要发什么...")
        content = await self._generate_dynamic_content()
        if not content:
            logger.error("[BiliBot] ❌ 生成动态内容失败")
            return
        text = content.get("text", "")
        need_image = content.get("need_image", False)
        image_prompt = content.get("image_prompt", "")
        logger.info(f"[BiliBot] 📝 文案：{text[:50]}...")
        logger.info(f"[BiliBot] 🖼️ 需要图片：{need_image}")
        success = False
        if need_image and image_prompt:
            logger.info(f"[BiliBot] 🎨 生图提示：{image_prompt[:50]}...")
            local_path = await self._generate_image(image_prompt)
            if local_path:
                img_info = await self._upload_image_to_bilibili(local_path)
                if img_info:
                    success = await self._post_dynamic_with_image(text, img_info)
                else:
                    success = await self._post_dynamic_text(text)
                try: os.remove(local_path)
                except: pass
            else:
                success = await self._post_dynamic_text(text)
        else:
            success = await self._post_dynamic_text(text)
        if success:
            log.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": text, "has_image": need_image and bool(image_prompt), "image_prompt": image_prompt if need_image else ""})
            self._save_json(DYNAMIC_LOG_FILE, log[-100:])
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            short_text = text[:60] if len(text) > 60 else text
            self._memory.append({"rpid": f"dynamic_{int(time.time())}", "thread_id": "dynamic", "user_id": "self", "time": now_str, "text": f"[{now_str}] Bot发了一条动态：{short_text}", "source": "bilibili"})
            self._save_json(MEMORY_FILE, self._memory)
            logger.info("[BiliBot] 🎉 动态发布完成！")
        else:
            logger.error("[BiliBot] ❌ 动态发布失败")

    def _generate_dynamic_schedule(self):
        """生成每日动态发布时间表"""
        n_times = self.config.get("DYNAMIC_TIMES_COUNT", 1)
        times = sorted(random.sample(range(10, 23), min(n_times, 12)))
        times = [(h, random.randint(0, 59)) for h in times]
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "dynamic_times": [f"{h}:{m:02d}" for h, m in times], "dynamic_triggered": []}
        self._save_json(DYNAMIC_SCHEDULE_FILE, schedule)
        return times, set()

    def _load_or_generate_dynamic_schedule(self):
        """加载或生成动态发布时间表"""
        try:
            schedule = self._load_json(DYNAMIC_SCHEDULE_FILE, {})
            if schedule.get("date") == datetime.now().strftime("%Y-%m-%d"):
                times = []
                for t in schedule.get("dynamic_times", []):
                    h, m = t.split(":"); times.append((int(h), int(m)))
                triggered = set(schedule.get("dynamic_triggered", []))
                return times, triggered
        except: pass
        return self._generate_dynamic_schedule()

    def _save_dynamic_schedule_state(self, times, triggered):
        """保存动态调度状态"""
        schedule = {"date": datetime.now().strftime("%Y-%m-%d"), "dynamic_times": [f"{h}:{m:02d}" for h, m in times], "dynamic_triggered": list(triggered)}
        self._save_json(DYNAMIC_SCHEDULE_FILE, schedule)

    async def _get_up_latest_video(self, mid):
        try:
            params = await self.sign_wbi_params({"mid": mid, "ps": 1, "pn": 1, "order": "pubdate"})
            d, _ = await self._http_get("https://api.bilibili.com/x/space/wbi/arc/search", params=params)
            if d.get("code") != 0: return None
            vlist = d.get("data", {}).get("list", {}).get("vlist", [])
            if not vlist: return None
            v = vlist[0]
            return {"bvid": v["bvid"], "title": v["title"], "desc": v.get("description",""), "up_name": v["author"], "up_mid": mid, "pubdate": v["created"], "pic": v.get("pic","")}
        except Exception as e:
            logger.error(f"[BiliBot] 获取UP主最新视频失败: {e}"); return None

    async def _get_hot_videos_by_tid(self, tid):
        MIN_VIEWS = 10000; videos = []
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/ranking/region", params={"rid": tid, "day": 7})
            if d["code"] == 0:
                for v in d.get("data", []):
                    play = int(v.get("play", v.get("stat", {}).get("view", 0)) or 0)
                    if play >= MIN_VIEWS:
                        videos.append({"bvid": v.get("bvid",""), "title": v.get("title",""), "desc": v.get("description", v.get("desc","")), "up_name": v.get("author", v.get("owner",{}).get("name","")), "up_mid": v.get("mid", v.get("owner",{}).get("mid",0)), "pubdate": v.get("pubdate", v.get("create", v.get("created",0))), "pic": v.get("pic",""), "view": play})
        except Exception as e: logger.warning(f"[BiliBot] 热榜API失败: {e}")
        if len(videos) < 5:
            try:
                d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/newlist", params={"rid": tid, "ps": 50, "pn": 1, "type": 0})
                if d["code"] == 0:
                    for v in d.get("data",{}).get("archives",[]):
                        play = int(v.get("stat",{}).get("view",0) or 0)
                        if play >= MIN_VIEWS:
                            videos.append({"bvid": v["bvid"], "title": v["title"], "desc": v.get("desc",""), "up_name": v["owner"]["name"], "up_mid": v["owner"]["mid"], "pubdate": v.get("pubdate",0), "pic": v.get("pic",""), "view": play})
            except Exception as e: logger.warning(f"[BiliBot] newlist API失败: {e}")
        seen = set(); unique = []
        for v in videos:
            if v["bvid"] and v["bvid"] not in seen: seen.add(v["bvid"]); unique.append(v)
        unique.sort(key=lambda x: x.get("view",0), reverse=True)
        return unique

    async def _get_video_oid(self, bvid):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid})
            if d.get("code") == 0: return d["data"]["aid"]
        except: pass
        return None

    async def _analyze_video_text(self, video_info):
        """用LLM纯文本分析视频（基于标题、简介、分区）"""
        prompt = f"""请根据以下B站视频信息，写一段简洁的内容概括（150字以内），包括：这个视频大概在讲什么、是什么类型/风格、可能的受众。

视频标题：{video_info.get('title', '未知')}
UP主：{video_info.get('up_name', '未知')}
分区：{video_info.get('tname', '未知')}
简介：{video_info.get('desc', '无')[:500]}

直接输出概括内容，不要加前缀。"""
        result = await self._llm_call(prompt, max_tokens=250)
        return result or f"视频《{video_info.get('title','未知')}》，UP主：{video_info.get('up_name','未知')}"

    async def _evaluate_video(self, video_info, video_description):
        sp = self._get_system_prompt()
        prompt = f"""你刚看完一个B站视频：
- UP主：{video_info.get('up_name','')}
- 标题：{video_info.get('title','')}
- 简介：{video_info.get('desc','')[:100]}
- 视频内容：{video_description}

请以JSON格式回复：
{{"score": 1到10的整数评分, "comment": "你想在评论区说的话（15-30字）", "mood": "看完后的心情（开心/平静/无聊/感动/好笑/震撼/困惑 选一个）", "review": "稍微详细的感想（50字以内）", "want_follow": true或false, "recommend_owner": true或false, "recommend_reason": "推荐理由（20字以内，不推荐则留空）"}}

comment要求：像B站用户真实评论，可以玩梗吐槽。
评分：1-3差，4-5一般，6-7不错，8-9很好，10神作。不要无脑高分。
直接输出JSON。"""
        try:
            text = await self._llm_call(prompt, system_prompt=sp, max_tokens=350)
            if not text: return None
            text = text.replace("```json","").replace("```","").strip()
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m: return json.loads(m.group())
            return json.loads(text)
        except Exception as e:
            logger.error(f"[BiliBot] 视频评价失败: {e}"); return None

    async def _generate_proactive_comment(self, video_info, video_description):
        sp = self._get_system_prompt()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt = f"""当前时间：{now}

你刚刚看完了一个视频：
- UP主：{video_info.get('up_name','')}
- 标题：{video_info.get('title','')}
- 视频内容：{video_description}

请以B站观众的身份，发一条自然真实的评论。要求：
1. 根据视频内容说有意义的话，不要无脑夸
2. 体现你的性格
3. 不超过40字
4. 直接输出评论内容，不加任何前缀"""
        result = await self._llm_call(prompt, system_prompt=sp, max_tokens=100)
        return result or "这个视频还不错"

    async def _send_comment(self, oid, comment_text, oid_type=1):
        try:
            d, _ = await self._http_post(BILI_REPLY_URL, data={"oid": oid, "type": oid_type, "message": comment_text, "csrf": self.config.get("BILI_JCT","")})
            return d.get("code") == 0
        except Exception as e:
            logger.error(f"[BiliBot] 发送评论异常: {e}"); return False

    async def _like_video(self, aid):
        try:
            d, _ = await self._http_post("https://api.bilibili.com/x/web-interface/archive/like", data={"aid": aid, "like": 1, "csrf": self.config.get("BILI_JCT","")})
            return d.get("code") == 0
        except: return False

    async def _coin_video(self, aid, num=1):
        try:
            d, _ = await self._http_post("https://api.bilibili.com/x/web-interface/coin/add", data={"aid": aid, "multiply": num, "select_like": 0, "csrf": self.config.get("BILI_JCT","")})
            return d.get("code") == 0
        except: return False

    async def _fav_video(self, aid):
        try:
            d, _ = await self._http_get("https://api.bilibili.com/x/v3/fav/folder/created/list-all", params={"up_mid": self.config.get("DEDE_USER_ID",""), "type": 2})
            if d["code"] != 0: return False
            fav_id = d["data"]["list"][0]["id"]
            d2, _ = await self._http_post("https://api.bilibili.com/x/v3/fav/resource/deal", data={"rid": aid, "type": 2, "add_media_ids": fav_id, "csrf": self.config.get("BILI_JCT","")})
            return d2.get("code") == 0
        except: return False

    async def _follow_user(self, mid):
        try:
            d, _ = await self._http_post("https://api.bilibili.com/x/relation/modify", data={"fid": mid, "act": 1, "re_src": 11, "csrf": self.config.get("BILI_JCT","")})
            return d.get("code") == 0
        except: return False

    async def _run_proactive(self):
        """主动刷B站：看视频、评价、点赞/投币/收藏/关注/评论"""
        daily_watch = self.config.get("PROACTIVE_VIDEO_COUNT", 3)
        daily_comment = self.config.get("PROACTIVE_COMMENT_COUNT", 2)
        watch_log = self._load_json(WATCH_LOG_FILE, [])
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_watched = [l for l in watch_log if l.get("time","").startswith(today_str)]
        if len(today_watched) >= daily_watch:
            logger.info(f"[BiliBot] 今天已看 {len(today_watched)} 个视频，不再刷"); return
        logger.info(f"[BiliBot] 🎯 主动刷B站 | 目标：看 {daily_watch} 个视频，评论 {daily_comment} 条")
        external_memory = self._load_json(EXTERNAL_MEMORY_FILE, {})
        commented_videos = set(self._load_json(COMMENTED_FILE, []))
        watched_bvids = set(commented_videos)
        for entry in watch_log: watched_bvids.add(entry.get("bvid",""))
        min_pubdate = int(datetime(2025, 1, 1).timestamp())
        target_videos = []
        # 1. 特别关心的UP主
        special_mids = self.config.get("PROACTIVE_FOLLOW_UIDS", [])
        for mid in special_mids:
            video = await self._get_up_latest_video(mid)
            if video and video["bvid"] not in watched_bvids:
                pubdate = video.get("pubdate", 0)
                if isinstance(pubdate, str):
                    try: pubdate = int(pubdate)
                    except: pubdate = 0
                if pubdate >= min_pubdate:
                    target_videos.insert(0, video)
                    logger.info(f"[BiliBot] ⭐ 特别关心：{video['up_name']} - {video['title']}")
        # 2. 关注的UP主
        following_mids = await self.get_followings()
        today = datetime.now().date()
        for mid in following_mids:
            video = await self._get_up_latest_video(mid)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            if video and video["bvid"] not in watched_bvids:
                pubdate = video.get("pubdate", 0)
                if isinstance(pubdate, str):
                    try: pubdate = int(pubdate)
                    except: pubdate = 0
                if pubdate < min_pubdate: continue
                is_today = pubdate and datetime.fromtimestamp(pubdate).date() == today
                if is_today:
                    target_videos.insert(0, video)
                    logger.info(f"[BiliBot] 🔔 今日更新：{video['up_name']} - {video['title']}")
        # 3. 分区热门
        tids = list(self.PREFERRED_TIDS); random.shuffle(tids)
        for tid in tids:
            if len(target_videos) >= daily_watch + 5: break
            hot = await self._get_hot_videos_by_tid(tid)
            for v in hot:
                if v["bvid"] not in watched_bvids:
                    pubdate = v.get("pubdate", 0)
                    if isinstance(pubdate, str):
                        try: pubdate = int(pubdate)
                        except: pubdate = 0
                    if pubdate >= min_pubdate: target_videos.append(v)
        # 去重 + 随机
        seen = set(); unique = []
        for v in target_videos:
            if v["bvid"] not in seen: seen.add(v["bvid"]); unique.append(v)
        sc = len(special_mids)
        if len(unique) > sc:
            tail = unique[sc:]; random.shuffle(tail); unique = unique[:sc] + tail
        logger.info(f"[BiliBot] 📋 共找到 {len(unique)} 个视频")
        watch_count = 0; comment_count = 0
        for video in unique:
            if watch_count >= daily_watch: break
            bvid = video["bvid"]
            if str(video.get("up_mid","")) == self.config.get("DEDE_USER_ID",""): continue
            logger.info(f"[BiliBot] 🎬 [{watch_count+1}/{daily_watch}] {video['title']} by {video.get('up_name','')}")
            # 文本分析（不下载视频，用标题+简介分析）
            vi = await self._get_video_info(video.get("oid") or await self._get_video_oid(bvid) or 0) if not video.get("tname") else None
            tname = (vi or video).get("tname", "")
            analysis_info = {**video, "tname": tname}
            video_description = await self._analyze_video_text(analysis_info)
            logger.info(f"[BiliBot] 📝 分析：{video_description[:60]}...")
            # 评价
            evaluation = await self._evaluate_video(video, video_description)
            if not evaluation:
                logger.warning("[BiliBot] 评价失败，跳过互动")
                watch_log.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "bvid": bvid, "title": video.get("title",""), "up_name": video.get("up_name",""), "score": 0, "mood": "未知", "comment": "评价失败", "review": "", "actions": [], "pic": video.get("pic","")})
                self._save_json(WATCH_LOG_FILE, watch_log[-200:]); watched_bvids.add(bvid); watch_count += 1; continue
            score = evaluation.get("score", 5)
            comment = evaluation.get("comment", "")
            mood = evaluation.get("mood", "平静")
            review = evaluation.get("review", "")
            want_follow = evaluation.get("want_follow", False)
            logger.info(f"[BiliBot] ⭐ 评分：{score}/10 | 心情：{mood} | 短评：{comment}")
            # 根据评分互动
            oid = await self._get_video_oid(bvid)
            actions = []
            if oid:
                if score >= 6 and self.config.get("PROACTIVE_LIKE", True):
                    if await self._like_video(oid): actions.append("👍点赞"); logger.info("[BiliBot] 👍 点赞成功")
                if score >= 8 and self.config.get("PROACTIVE_COIN", False):
                    if await self._coin_video(oid): actions.append("🪙投币"); logger.info("[BiliBot] 🪙 投币成功")
                if score >= 8 and self.config.get("PROACTIVE_FAV", True):
                    if await self._fav_video(oid): actions.append("⭐收藏"); logger.info("[BiliBot] ⭐ 收藏成功")
                if score >= 7 and comment_count < daily_comment and self.config.get("PROACTIVE_COMMENT", True):
                    proactive_comment = await self._generate_proactive_comment(video, video_description)
                    if await self._send_comment(oid, proactive_comment):
                        actions.append("💬评论"); comment_count += 1
                        logger.info(f"[BiliBot] 💬 评论成功：{proactive_comment}")
                        commented_videos.add(bvid); self._save_json(COMMENTED_FILE, list(commented_videos))
                        # 保存主动评论日志
                        pl = self._load_json(PROACTIVE_LOG_FILE, [])
                        pl.append({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "bvid": bvid, "title": video.get("title",""), "comment": proactive_comment})
                        self._save_json(PROACTIVE_LOG_FILE, pl[-100:])
                # 推荐给主人
                if evaluation.get("recommend_owner", False):
                    on = self.config.get("OWNER_NAME","") or "主人"
                    owner_bili = self.config.get("OWNER_BILI_NAME", "")
                    if owner_bili:
                        try:
                            rec_prompt = f"""你刚看完视频「{video.get('title','')}」，觉得很不错想推荐给{on}。
写一句简短的推荐语，要求：
- 用你自己的语气，自然随意
- 不超过25字
- 不要带@、不要带任何人名或称呼
- 直接输出推荐语"""
                            rec_text = await self._llm_call(rec_prompt, system_prompt=self._get_system_prompt(), max_tokens=60)
                            rec_text = re.sub(r'@\S+\s*', '', rec_text or "你可能会喜欢这个")
                            rec_text = re.sub(r'^(主人|柠弥|亲爱的)[，,\s]*', '', rec_text)
                            rec_msg = f"@{owner_bili} {rec_text}"
                            if await self._send_comment(oid, rec_msg):
                                actions.append("📢推荐给主人"); logger.info(f"[BiliBot] 📢 已@主人：{rec_msg}")
                        except: pass
            # 关注UP主
            if (score >= 9 or want_follow) and self.config.get("PROACTIVE_FOLLOW", True):
                if str(video.get("up_mid","")) != str(self.config.get("OWNER_MID","")):
                    if await self._follow_user(video["up_mid"]):
                        actions.append("➕关注"); logger.info(f"[BiliBot] ➕ 关注了 {video.get('up_name','')}")
            # 保存观影日记
            log_entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "bvid": bvid, "title": video.get("title",""), "up_name": video.get("up_name",""), "up_mid": str(video.get("up_mid","")), "score": score, "mood": mood, "comment": comment, "review": review, "actions": actions, "pic": video.get("pic","")}
            watch_log.append(log_entry); self._save_json(WATCH_LOG_FILE, watch_log[-200:])
            # 存入外部记忆
            if bvid not in external_memory:
                external_memory[bvid] = {"title": video.get("title",""), "up_name": video.get("up_name",""), "up_mid": str(video.get("up_mid","")), "description": video_description, "score": score, "mood": mood, "review": review, "watched_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "comments": []}
                self._save_json(EXTERNAL_MEMORY_FILE, external_memory)
            watched_bvids.add(bvid); watch_count += 1
            action_str = " ".join(actions) if actions else "（默默看完）"
            logger.info(f"[BiliBot] 📊 互动：{action_str}")
            wait = random.randint(30, 120)
            logger.info(f"[BiliBot] ⏳ 等待 {wait} 秒...")
            await asyncio.sleep(wait)
        logger.info(f"[BiliBot] 🎉 刷B站完成！看了 {watch_count} 个视频，评论了 {comment_count} 条")

    # ===== QQ命令 =====
    @filter.command("bili登录")
    async def cmd_login(self, event: AstrMessageEvent):
        qr_url, qrcode_key = await self._qr_login_generate()
        if not qr_url: yield event.plain_result("❌ 生成二维码失败"); return
        self._login_qrcode_key = qrcode_key
        import qrcode; qr=qrcode.QRCode(version=1,box_size=8,border=2); qr.add_data(qr_url); qr.make(fit=True)
        img=qr.make_image(fill_color="black",back_color="white"); buf=io.BytesIO(); img.save(buf,format="PNG"); buf.seek(0)
        qr_path=os.path.join(DATA_DIR,"login_qr.png")
        with open(qr_path,"wb") as f: f.write(buf.getvalue())
        yield event.chain_result([Plain("📱 请用B站APP扫描下方二维码：\n扫码后发送 /bili确认"), Image.fromFileSystem(qr_path)])
    @filter.command("bili确认")
    async def cmd_login_confirm(self, event: AstrMessageEvent):
        if not self._login_qrcode_key: yield event.plain_result("❌ 没有待确认的登录"); return
        for i in range(3):
            code,msg,cookies = await self._qr_login_poll(self._login_qrcode_key)
            if code==0:
                for k,ck in [("SESSDATA","SESSDATA"),("BILI_JCT","bili_jct"),("DEDE_USER_ID","DedeUserID"),("REFRESH_TOKEN","REFRESH_TOKEN")]:
                    if cookies.get(ck): self.config[k]=cookies[ck]
                self.config.save_config(); self._login_qrcode_key=None
                valid,info=await self.check_cookie(); yield event.plain_result(f"✅ 登录成功！\n{info}")
                if not self._running: await self._start_bot(); yield event.plain_result("🚀 后台任务已自动启动")
                return
            elif code==86090: yield event.plain_result(f"📱 {msg}"); await asyncio.sleep(2)
            elif code==86101: yield event.plain_result(f"⏳ {msg}"); await asyncio.sleep(3)
            elif code==86038: self._login_qrcode_key=None; yield event.plain_result(f"❌ {msg}，请重新 /bili登录"); return
            else: yield event.plain_result(f"❌ {msg}"); return
        yield event.plain_result("⏳ 还没确认成功，请在手机上确认后再发 /bili确认")
    @filter.command("bili状态")
    async def cmd_status(self, event: AstrMessageEvent):
        valid,info=await self.check_cookie(); mood,_=self._get_today_mood()
        mc=len(self._memory); pc=len(self._load_json(USER_PROFILE_FILE,{})); pmc=len(self._load_json(PERMANENT_MEMORY_FILE,[]))
        evo=self._load_json(PERSONALITY_FILE,{}); evo_ver=evo.get("version",0); evo_last=evo.get("last_evolve","从未")
        wl=self._load_json(WATCH_LOG_FILE,[]); today_watched=len([l for l in wl if l.get("time","").startswith(datetime.now().strftime("%Y-%m-%d"))])
        dl=self._load_json(DYNAMIC_LOG_FILE,[]); today_dynamic=len([l for l in dl if l.get("time","").startswith(datetime.now().strftime("%Y-%m-%d"))])
        lines = [
            f"📺 BiliBot v1.1.0 状态","━━━━━━━━━━━━",f"🍪 {info}",
            f"{'🟢 运行中' if self._running else '🔴 未运行'}",
            f"🧠 记忆:{mc}条 | 💎永久:{pmc}条 | 👤档案:{pc}个",
            f"🎭 心情:{mood} | 🌱性格v{evo_ver}（{evo_last[:10]}）",
            f"📹 今日已看:{today_watched}个视频 | 📝动态:{today_dynamic}条",
            f"回复:{'✅' if self.config.get('ENABLE_REPLY',True) else '❌'} 好感:{'✅' if self.config.get('ENABLE_AFFECTION',True) else '❌'} 心情:{'✅' if self.config.get('ENABLE_MOOD',True) else '❌'}",
            f"主动:{'✅' if self.config.get('ENABLE_PROACTIVE',False) else '❌'} 动态:{'✅' if self.config.get('ENABLE_DYNAMIC',False) else '❌'} 演化:{'✅' if self.config.get('ENABLE_PERSONALITY_EVOLUTION',True) else '❌'}",
            f"视频视觉:{'✅' if self.config.get('VIDEO_VISION_API_KEY','') else '❌'} 图片识别:{'✅' if self.config.get('IMAGE_VISION_API_KEY','') else '❌'}"
        ]
        yield event.plain_result("\n".join(lines))
    @filter.command("bili启动")
    async def cmd_start(self, event: AstrMessageEvent):
        if self._running: yield event.plain_result("⚠️ 已在运行"); return
        if not self._has_cookie(): yield event.plain_result("❌ 请先 /bili登录"); return
        await self._start_bot(); yield event.plain_result("🚀 已启动！")
    @filter.command("bili停止")
    async def cmd_stop(self, event: AstrMessageEvent):
        if not self._running: yield event.plain_result("⚠️ 没在运行"); return
        await self._stop_bot(); yield event.plain_result("⏹️ 已停止")
    @filter.command("bili开关")
    async def cmd_toggle(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts)<2:
            tm = {"回复":"ENABLE_REPLY","主动":"ENABLE_PROACTIVE","动态":"ENABLE_DYNAMIC","好感":"ENABLE_AFFECTION","心情":"ENABLE_MOOD","演化":"ENABLE_PERSONALITY_EVOLUTION","点赞":"PROACTIVE_LIKE","投币":"PROACTIVE_COIN","收藏":"PROACTIVE_FAV","关注":"PROACTIVE_FOLLOW","评论":"PROACTIVE_COMMENT"}
            lines = ["可切换功能："] + [f"  {n} ({'✅' if self.config.get(k,True) else '❌'})" for n,k in tm.items()] + ["","用法: /bili开关 回复"]
            yield event.plain_result("\n".join(lines)); return
        name=parts[1].strip()
        tm = {"回复":"ENABLE_REPLY","主动":"ENABLE_PROACTIVE","动态":"ENABLE_DYNAMIC","好感":"ENABLE_AFFECTION","心情":"ENABLE_MOOD","演化":"ENABLE_PERSONALITY_EVOLUTION","点赞":"PROACTIVE_LIKE","投币":"PROACTIVE_COIN","收藏":"PROACTIVE_FAV","关注":"PROACTIVE_FOLLOW","评论":"PROACTIVE_COMMENT"}
        key=tm.get(name)
        if not key: yield event.plain_result(f"❌ 不认识：{name}"); return
        cur=self.config.get(key,True); self.config[key]=not cur; self.config.save_config()
        yield event.plain_result(f"{name}: {'✅ 已开启' if not cur else '❌ 已关闭'}")
    @filter.command("bili刷新")
    async def cmd_refresh_cookie(self, event: AstrMessageEvent):
        yield event.plain_result("🔄 刷新中..."); _,msg=await self.refresh_cookie(); yield event.plain_result(msg)
    @filter.command("bili记忆")
    async def cmd_memory(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts)<2:
            mc=len(self._memory); bc=len([m for m in self._memory if m.get("source")=="bilibili"]); qc=len([m for m in self._memory if m.get("source")=="qq"])
            yield event.plain_result(f"🧠 记忆统计\n总计:{mc} | B站:{bc} | QQ:{qc}\n\n用法: /bili记忆 <关键词>\n/bili记忆 关键词 qq ← 只搜QQ"); return
        query=parts[1]; source=parts[2] if len(parts)>2 else None
        if source=="all": source=None
        results = self._search_memories(query, limit=5, source=source)
        if not results: yield event.plain_result(f"🧠 没找到「{query}」的记忆"); return
        lines = [f"🧠 关于「{query}」的记忆：",""]
        for i,r in enumerate(results,1): lines.append(f"{i}. {r[:150]+'...' if len(r)>150 else r}")
        yield event.plain_result("\n".join(lines))
    @filter.command("bili好感")
    async def cmd_affection(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts)>=2:
            uid=parts[1].strip(); sc=self._affection.get(uid,0); lv=self._get_level(sc,uid)
            p=self._load_json(USER_PROFILE_FILE,{}).get(uid,{})
            lines=[f"👤 用户 {uid}",f"💛 {sc}分 | {LEVEL_NAMES[lv]}",f"📝 {p.get('impression','暂无')}"]
            if p.get("facts"): lines.append(f"📋 {'；'.join(p['facts'][-5:])}")
            yield event.plain_result("\n".join(lines)); return
        if not self._affection: yield event.plain_result("💛 无记录"); return
        sa=sorted(self._affection.items(),key=lambda x:x[1],reverse=True)[:10]
        lines=["💛 好感度 Top 10","━━━━━━━━━━━━"]
        ps=self._load_json(USER_PROFILE_FILE,{})
        for i,(uid,sc) in enumerate(sa,1):
            lv=self._get_level(sc,uid); imp=ps.get(uid,{}).get("impression","")
            lines.append(f"{i}. UID:{uid} | {sc}分 {LEVEL_NAMES[lv]}{' — '+imp[:20] if imp else ''}")
        yield event.plain_result("\n".join(lines))
    @filter.command("bili拉黑")
    async def cmd_block(self, event: AstrMessageEvent):
        """手动拉黑用户。用法: /bili拉黑 <UID>"""
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts)<2: yield event.plain_result("用法: /bili拉黑 <UID>"); return
        uid=parts[1].strip()
        if not uid.isdigit(): yield event.plain_result("❌ UID必须是数字"); return
        if uid==str(self.config.get("OWNER_MID","")): yield event.plain_result("❌ 不能拉黑主人！"); return
        success = await self._block_user(int(uid))
        bl = self._load_json(os.path.join(DATA_DIR,"block_log.json"),{})
        bl[uid] = {"username":"手动拉黑","reason":"手动拉黑","time":datetime.now().strftime("%Y-%m-%d %H:%M")}
        self._save_json(os.path.join(DATA_DIR,"block_log.json"), bl)
        yield event.plain_result(f"{'✅' if success else '⚠️'} 已拉黑 UID:{uid}{'（B站API调用成功）' if success else '（B站API失败，但已加入本地黑名单）'}")
    @filter.command("bili解黑")
    async def cmd_unblock(self, event: AstrMessageEvent):
        """解除拉黑。用法: /bili解黑 <UID>"""
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts)<2: yield event.plain_result("用法: /bili解黑 <UID>"); return
        uid=parts[1].strip()
        bl = self._load_json(os.path.join(DATA_DIR,"block_log.json"),{})
        if uid not in bl: yield event.plain_result(f"⚠️ UID:{uid} 不在黑名单中"); return
        # B站解除拉黑: act=6
        try:
            d, _ = await self._http_post("https://api.bilibili.com/x/relation/modify", data={"fid":uid,"act":6,"re_src":11,"csrf":self.config.get("BILI_JCT","")})
            api_ok = d["code"]==0
        except: api_ok=False
        del bl[uid]; self._save_json(os.path.join(DATA_DIR,"block_log.json"), bl)
        # 重置好感度为0
        self._affection[uid] = 0; self._save_json(AFFECTION_FILE, self._affection)
        yield event.plain_result(f"✅ 已解除拉黑 UID:{uid}，好感度重置为0{'' if api_ok else '（B站API失败，但已从本地黑名单移除）'}")
    @filter.command("bili黑名单")
    async def cmd_blocklist(self, event: AstrMessageEvent):
        """查看拉黑名单"""
        bl = self._load_json(os.path.join(DATA_DIR,"block_log.json"),{})
        if not bl: yield event.plain_result("🚫 黑名单为空"); return
        lines = ["🚫 黑名单","━━━━━━━━━━━━"]
        for uid,info in bl.items():
            lines.append(f"UID:{uid} | {info.get('reason','未知')} | {info.get('time','')}")
        yield event.plain_result("\n".join(lines))
    @filter.command("bili性格")
    async def cmd_personality(self, event: AstrMessageEvent):
        """查看性格演化记录。用法: /bili性格"""
        evo = self._load_json(PERSONALITY_FILE, {})
        if not evo:
            yield event.plain_result("🌱 还没有性格演化记录"); return
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
            yield event.plain_result("用法：\n/bili性格编辑 习惯 <内容>\n/bili性格编辑 看法 <内容>\n/bili性格编辑 变化 <内容>"); return
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
            yield event.plain_result("❌ 类别不对，可选：习惯、看法、变化"); return
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
            yield event.plain_result("用法：/bili性格删除 <习惯|看法|变化> <序号>"); return
        category, idx_str = parts[1].strip(), parts[2].strip()
        if not idx_str.isdigit():
            yield event.plain_result("❌ 序号必须是数字"); return
        idx = int(idx_str) - 1
        evo = self._load_json(PERSONALITY_FILE, {})
        if not evo:
            yield event.plain_result("🌱 没有演化记录"); return
        key_map = {"习惯": "speech_habits", "看法": "opinions", "变化": "evolved_traits"}
        key = key_map.get(category)
        if not key:
            yield event.plain_result("❌ 类别不对，可选：习惯、看法、变化"); return
        items = evo.get(key, [])
        if idx < 0 or idx >= len(items):
            yield event.plain_result(f"❌ 序号超范围（1-{len(items)}）"); return
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
            yield event.plain_result(f"📋 {target_date} 没有主动行为记录"); return
        lines = [f"📋 {target_date} 主动行为日志", "━━━━━━━━━━━━"]
        if today_watch:
            lines.append(f"\n🎬 看了 {len(today_watch)} 个视频：")
            for i, w in enumerate(today_watch, 1):
                score = w.get("score", "?")
                actions = " ".join(w.get("actions", [])) or "无互动"
                lines.append(f"  {i}. 「{w.get('title','?')[:30]}」")
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
                yield event.plain_result("❌ 序号必须是数字"); return
            idx = int(idx_str) - 1
            if idx < 0 or idx >= len(perm):
                yield event.plain_result(f"❌ 序号超范围（1-{len(perm)}）"); return
            removed = perm.pop(idx)
            self._save_json(PERMANENT_MEMORY_FILE, perm)
            yield event.plain_result(f"✅ 已删除永久记忆：{removed.get('text','')[:50]}"); return
        if not perm:
            yield event.plain_result("💎 还没有永久记忆"); return
        lines = [f"💎 永久记忆（{len(perm)}/20）", "━━━━━━━━━━━━"]
        for i, p in enumerate(perm, 1):
            lines.append(f"  {i}. [{p.get('time','?')}] {p.get('text','')[:80]}")
        lines.append("\n删除用: /bili永久记忆 删除 <序号>")
        yield event.plain_result("\n".join(lines))

    @filter.command("bili动态")
    async def cmd_dynamic(self, event: AstrMessageEvent):
        """手动发布动态"""
        if not self._has_cookie():
            yield event.plain_result("❌ 请先 /bili登录"); return
        yield event.plain_result("📢 正在发布动态...")
        await self._run_dynamic()
        yield event.plain_result("📢 动态发布流程已完成，请查看日志")

    @filter.command("bili动态日志")
    async def cmd_dynamic_log(self, event: AstrMessageEvent):
        """查看动态发布日志"""
        log = self._load_json(DYNAMIC_LOG_FILE, [])
        if not log:
            yield event.plain_result("📝 还没有动态记录"); return
        lines = ["📝 最近动态记录", "━━━━━━━━━━━━"]
        for i, l in enumerate(log[-10:], 1):
            img = "🖼️" if l.get("has_image") else "📄"
            lines.append(f"{i}. [{l.get('time','')}] {img}")
            lines.append(f"   {l.get('text','')[:60]}...")
        yield event.plain_result("\n".join(lines))

    @filter.command("bili帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result("📺 BiliBot 命令\n━━━━━━━━━━━━\n/bili登录 — 扫码登录\n/bili确认 — 确认扫码\n/bili状态 — 运行状态\n/bili启动 — 启动\n/bili停止 — 停止\n/bili开关 — 功能开关\n/bili刷新 — 刷新Cookie\n/bili记忆 — 搜索记忆\n/bili好感 — 好感度\n/bili拉黑 — 手动拉黑\n/bili解黑 — 解除拉黑\n/bili黑名单 — 查看黑名单\n/bili性格 — 查看性格演化\n/bili性格编辑 — 手动编辑性格\n/bili性格删除 — 删除演化条目\n/bili日志 — 今日视频/评论日志\n/bili永久记忆 — 查看/删除永久记忆\n/bili动态 — 手动发动态\n/bili动态日志 — 动态记录\n/bili绑定 — 绑定QQ与B站UID\n/bili解绑 — 解除绑定\n/bili帮助 — 本帮助\n━━━━━━━━━━━━\n💡 首次用 /bili登录")

    # ===== QQ↔B站 记忆互通 =====
    @filter.command("bili绑定")
    async def cmd_bind(self, event: AstrMessageEvent):
        """绑定QQ与B站UID: /bili绑定 12345"""
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法：/bili绑定 <B站UID>"); return
        bili_uid = parts[1].strip()
        if not bili_uid.isdigit():
            yield event.plain_result("⚠️ B站UID应为数字"); return
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
            yield event.plain_result("⚠️ 你还没有绑定B站UID"); return
        del bindings[qq_id]
        self._save_json(BINDING_FILE, bindings)
        yield event.plain_result("✅ 已解除绑定")

    @filter.on_llm_request()
    async def inject_bili_memory(self, event: AstrMessageEvent, req: ProviderRequest):
        """QQ对话提到B站相关关键词时，注入B站侧的永久记忆"""
        try:
            msg = event.message_str or ""
            if not any(kw in msg.lower() for kw in BILI_MENTION_KEYWORDS):
                return
            qq_id = str(event.get_sender_id())
            bindings = self._load_json(BINDING_FILE, {})
            if qq_id not in bindings:
                return
            perm = self._load_json(PERMANENT_MEMORY_FILE, [])
            bili_memories = [p for p in perm if p.get("source") == "bilibili"]
            if not bili_memories:
                return
            mem_text = "\n".join([f"[{p.get('time','?')}] {p['text']}" for p in bili_memories[-10:]])
            req.system_prompt += f"\n\n【B站侧记忆（该用户已绑定B站UID:{bindings[qq_id]}）】\n{mem_text}"
            logger.debug(f"[BiliBot] QQ→B站记忆注入：{len(bili_memories)}条")
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
            emb = self._get_embedding(text)
            rec = {"rpid": f"qq_{int(datetime.now().timestamp())}","thread_id":"qq","user_id":bili_uid,"time":now,"text":text,"source":"qq"}
            if emb: rec["embedding"] = emb
            qq_mem = self._load_json(QQ_MEMORY_FILE, [])
            qq_mem.append(rec)
            self._save_json(QQ_MEMORY_FILE, qq_mem)
            logger.debug(f"[BiliBot] QQ记忆存入: {text[:50]}")
        except Exception as e:
            logger.error(f"[BiliBot] QQ记忆捕获失败: {e}")

    # ===== 后台任务 =====
    async def _auto_start(self):
        await asyncio.sleep(3)
        valid,_=await self.check_cookie()
        if valid: await self._start_bot(); logger.info("[BiliBot] 自动启动")
        else: logger.warning("[BiliBot] Cookie无效")
    async def _start_bot(self):
        if self._running: return
        self._running=True; self._task=asyncio.create_task(self._main_loop()); logger.info("[BiliBot] 启动")
    async def _stop_bot(self):
        self._running=False
        if self._task: self._task.cancel(); self._task=None
        if self._proactive_task and not self._proactive_task.done(): self._proactive_task.cancel(); self._proactive_task=None
        if self._dynamic_task and not self._dynamic_task.done(): self._dynamic_task.cancel(); self._dynamic_task=None
        logger.info("[BiliBot] 停止")
    async def _main_loop(self):
        logger.info("[BiliBot] 主循环开始")
        while self._running:
            try:
                # 性格演化（独立于休眠）
                await self._maybe_evolve_personality()
                h=datetime.now().hour; ss=self.config.get("SLEEP_START",2); se=self.config.get("SLEEP_END",8)
                if ss<=h<se: await asyncio.sleep(60); continue
                ci=self.config.get("COOKIE_CHECK_INTERVAL",6)*3600
                if time.time()-self._last_cookie_check>ci: await self._check_and_refresh_cookie(); self._last_cookie_check=time.time()
                # 主动视频调度
                if self.config.get("ENABLE_PROACTIVE", False):
                    now_dt = datetime.now()
                    today_str = now_dt.strftime("%Y-%m-%d")
                    sched = self._load_json(SCHEDULE_FILE, {})
                    if sched.get("date") != today_str:
                        self._proactive_times, self._proactive_triggered = self._generate_daily_schedule()
                        logger.info(f"[BiliBot] 📅 新的一天！主动视频时间：{[f'{h}:{m:02d}' for h,m in self._proactive_times]}")
                    elif not self._proactive_times:
                        self._proactive_times, self._proactive_triggered = self._load_or_generate_schedule()
                    for ph, pm in self._proactive_times:
                        key = f"{ph}:{pm:02d}"
                        if key not in self._proactive_triggered and (now_dt.hour > ph or (now_dt.hour == ph and now_dt.minute >= pm)):
                            if self._proactive_task is None or self._proactive_task.done():
                                self._proactive_task = asyncio.create_task(self._run_proactive())
                                self._proactive_triggered.add(key)
                                self._save_schedule_state(self._proactive_times, self._proactive_triggered)
                                logger.info(f"[BiliBot] 🎯 触发主动视频（{key}）")
                # 动态发布调度
                if self.config.get("ENABLE_DYNAMIC", False):
                    now_dt = datetime.now()
                    today_str = now_dt.strftime("%Y-%m-%d")
                    sched = self._load_json(DYNAMIC_SCHEDULE_FILE, {})
                    if sched.get("date") != today_str:
                        self._dynamic_times, self._dynamic_triggered = self._generate_dynamic_schedule()
                        logger.info(f"[BiliBot] 📅 动态时间：{[f'{h}:{m:02d}' for h,m in self._dynamic_times]}")
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
                if self.config.get("ENABLE_REPLY",True): await self._poll_and_reply()
                await asyncio.sleep(self.config.get("POLL_INTERVAL",20))
            except asyncio.CancelledError: break
            except Exception as e: logger.error(f"[BiliBot] 主循环出错: {e}\n{traceback.format_exc()}"); await asyncio.sleep(30)
        self._running=False
    async def _check_and_refresh_cookie(self):
        valid,info=await self.check_cookie()
        if valid: logger.info(f"[BiliBot] Cookie OK: {info}"); return
        logger.warning(f"[BiliBot] Cookie 失效: {info}")
        if self.config.get("COOKIE_AUTO_REFRESH",True):
            ok,msg=await self.refresh_cookie(); logger.info(f"[BiliBot] 刷新{'成功' if ok else '失败'}: {msg}")
    async def _poll_and_reply(self):
        if time.time() < self._llm_cooldown_until:
            return  # LLM冷却中，跳过
        try:
            d,_=await self._http_get(BILI_NOTIFY_URL, params={"ps":10,"pn":1})
            if d["code"]!=0: return
            items=d.get("data",{}).get("items",[])
            if not items: return
            replied=set(self._load_json(REPLIED_FILE,[]))
            if self._first_poll:
                for item in items:
                    rpid=str(item.get("item",{}).get("source_id",""))
                    if rpid: replied.add(rpid)
                self._save_json(REPLIED_FILE,list(replied)); self._first_poll=False
                logger.info(f"[BiliBot] 首次运行，标记 {len(items)} 条已读"); return
            count=0; mr=self.config.get("MAX_REPLIES_PER_RUN",3)
            for item in items:
                if count>=mr: break
                r=item.get("item",{}); rpid=str(r.get("source_id",""))
                if rpid in replied: continue
                mid=str(item.get("user",{}).get("mid","")); username=item.get("user",{}).get("nickname","")
                content=r.get("source_content",""); oid=r.get("subject_id",0); ct=r.get("business_id",1)
                thread_id=str(r.get("root_id") or rpid)
                if not content or not rpid: continue
                # 拉黑用户跳过（不调LLM不花钱）
                bl = self._load_json(os.path.join(DATA_DIR,"block_log.json"),{})
                if mid in bl:
                    replied.add(rpid); self._save_json(REPLIED_FILE,list(replied)); continue
                if self._is_blocked(content):
                    self._log_security_event("keyword_blocked",mid,username,content,"关键词过滤")
                    replied.add(rpid); self._save_json(REPLIED_FILE,list(replied)); continue
                cs=self._affection.get(str(mid),0); lv=self._get_level(cs,mid)
                logger.info(f"[BiliBot] 📩 {username}（{LEVEL_NAMES[lv]}|{cs}分）：{content[:50]}")
                # 检测评论中的图片
                image_desc = ""
                image_urls = await self._get_comment_images(oid, rpid, ct)
                if image_urls:
                    logger.info(f"[BiliBot] 🖼️ 发现 {len(image_urls)} 张图片，识别中...")
                    image_desc = await self._recognize_images(image_urls)
                    if image_desc: logger.info(f"[BiliBot] 🖼️ 图片内容：{image_desc[:50]}...")
                result = await self._generate_reply(content, mid, username, thread_id, oid, ct, image_desc=image_desc)
                if not result or not result.get("reply"):
                    self._retry_counts[rpid] = self._retry_counts.get(rpid, 0) + 1
                    self._consecutive_llm_failures += 1
                    if self._retry_counts[rpid] >= 3:
                        logger.warning(f"[BiliBot] {username} 重试3次仍失败，放弃")
                        replied.add(rpid); self._save_json(REPLIED_FILE,list(replied))
                        self._retry_counts.pop(rpid, None)
                    else:
                        logger.warning(f"[BiliBot] {username} 第{self._retry_counts[rpid]}次失败，下轮重试")
                    if self._consecutive_llm_failures >= 5:
                        self._llm_cooldown_until = time.time() + 300
                        logger.error("[BiliBot] ⚠️ 连续5次LLM失败，冷却5分钟")
                        break
                    continue
                self._consecutive_llm_failures = 0  # LLM活着，全局计数归零
                self._retry_counts.pop(rpid, None)
                ai_reply=result["reply"]; sd=result.get("score_delta",1)
                imp=result.get("impression",""); uf=result.get("user_facts",[]); pm=result.get("permanent_memory","")
                if self.config.get("ENABLE_AFFECTION",True):
                    mx=100 if str(mid)==str(self.config.get("OWNER_MID","")) else 99
                    ns=max(0,min(mx,cs+sd)); self._affection[str(mid)]=ns; self._save_json(AFFECTION_FILE,self._affection)
                    ds=f"+{sd}" if sd>=0 else str(sd)
                    logger.info(f"[BiliBot] 💛 {cs}→{ns}（{ds}）| {LEVEL_NAMES[self._get_level(ns,mid)]}")
                    mm=self._check_milestone(mid,cs,ns,username)
                    if mm: ai_reply=mm
                    should_block=False
                    if ns<=-30: should_block=True
                    if sd<=-3:
                        bc=self._load_json(os.path.join(DATA_DIR,"block_count.json"),{}); bc[mid]=bc.get(mid,0)+1
                        self._save_json(os.path.join(DATA_DIR,"block_count.json"),bc)
                        if bc[mid]>=5: should_block=True
                        self._log_security_event("negative",mid,username,content,f"{cs}→{ns}({ds})")
                    else:
                        bc=self._load_json(os.path.join(DATA_DIR,"block_count.json"),{})
                        if mid in bc: bc[mid]=0; self._save_json(os.path.join(DATA_DIR,"block_count.json"),bc)
                    if should_block and str(mid)!=str(self.config.get("OWNER_MID","")):
                        await self._send_reply(oid,rpid,ct,"我不想和你说话了。"); await self._block_user(int(mid))
                        logger.info(f"[BiliBot] 🚫 拉黑 {username}"); replied.add(rpid); self._save_json(REPLIED_FILE,list(replied)); continue
                if imp or uf: self._update_user_profile(mid, username=username, impression=imp or None, new_facts=uf or None)
                if pm:
                    perm = self._load_json(PERMANENT_MEMORY_FILE, [])
                    if len(perm) < 20:
                        perm.append({"text": pm, "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
                        self._save_json(PERMANENT_MEMORY_FILE, perm)
                        logger.info(f"[BiliBot] 💎 新增永久记忆：{pm[:50]}")
                    else:
                        logger.info(f"[BiliBot] 💎 永久记忆已满（20条），跳过：{pm[:30]}")
                logger.info(f"[BiliBot] 💬 {username}: {ai_reply[:50]}")
                success=await self._send_reply(oid,rpid,ct,ai_reply)
                if success:
                    self._save_memory_record(rpid,thread_id,mid,username,content,ai_reply); count+=1
                    await self._compress_user_memory(mid,username)
                replied.add(rpid); self._save_json(REPLIED_FILE,list(replied))
        except Exception as e: logger.error(f"[BiliBot] 轮询出错: {e}\n{traceback.format_exc()}")

    async def terminate(self):
        await self._stop_bot()
        if self._web_panel:
            await self._web_panel.stop()
        logger.info("[BiliBot] 已停用")