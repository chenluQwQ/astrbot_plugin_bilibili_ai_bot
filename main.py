"""
AstrBot Plugin - Bilibili Bot v0.2.0
自动回复评论、好感度、记忆、心情、用户画像、主动视频、动态发布。
"""
import io, os, re, time, json, math, random, asyncio, hashlib, base64, requests, traceback
from datetime import datetime, timedelta
from functools import reduce
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain
from astrbot.api import logger, AstrBotConfig

PLUGIN_NAME = "astrbot_plugin_bilibili_bot"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
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

BILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILI_REPLY_URL = "https://api.bilibili.com/x/v2/reply/add"
BILI_NOTIFY_URL = "https://api.bilibili.com/x/msgfeed/reply"
BILI_COOKIE_INFO_URL = "https://passport.bilibili.com/x/passport-login/web/cookie/info"
BILI_COOKIE_REFRESH_URL = "https://passport.bilibili.com/x/passport-login/web/cookie/refresh"
BILI_COOKIE_CONFIRM_URL = "https://passport.bilibili.com/x/passport-login/web/confirm/refresh"
BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

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

@register("astrbot_plugin_bilibili_bot","chenluQwQ","B站 AI Bot — 自动回复评论、主动看视频、发动态","0.2.0","https://github.com/chenluQwQ/astrbot_plugin_bilibili_bot")
class BiliBiliBot(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._ensure_data_dir()
        self._running = False
        self._task = None
        self._last_cookie_check = 0
        self._login_qrcode_key = None
        self._first_poll = not os.path.exists(REPLIED_FILE)
        self._affection = self._load_json(AFFECTION_FILE, {})
        self._memory = self._load_json(MEMORY_FILE, [])
        self._embed_client = None
        self._consecutive_llm_failures = 0  # 全局连续失败，达5次冷却
        self._llm_cooldown_until = 0  # 冷却截止时间戳
        self._retry_counts: dict = {}  # {rpid: 已失败次数}，单条最多重试3次
        if self._has_cookie():
            asyncio.create_task(self._auto_start())

    # ===== 工具 =====
    def _ensure_data_dir(self): os.makedirs(DATA_DIR, exist_ok=True)
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
    def _block_user(self, mid):
        try:
            resp = requests.post("https://api.bilibili.com/x/relation/modify", headers=self._headers(), data={"fid":mid,"act":5,"re_src":11,"csrf":self.config.get("BILI_JCT","")}, timeout=10)
            return resp.json()["code"]==0
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
        parts = []
        if p.get("impression"): parts.append(f"印象：{p['impression']}")
        if p.get("facts"): parts.append("已知信息：" + "；".join(p["facts"][-10:]))
        if p.get("tags"): parts.append("标签：" + "、".join(p["tags"]))
        return "【对该用户的了解】\n" + "\n".join(parts) if parts else ""
    def _update_user_profile(self, mid, impression=None, new_facts=None, new_tags=None):
        profiles = self._load_json(USER_PROFILE_FILE, {})
        uid = str(mid)
        if uid not in profiles: profiles[uid] = {"impression":"","facts":[],"tags":[]}
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
        if not um: return []
        qe = self._get_embedding(query_text)
        if not qe: return []
        scored = [(self._cosine_similarity(qe, m["embedding"]), m["text"]) for m in um]
        scored.sort(reverse=True)
        return [t for s,t in scored[:MAX_SEMANTIC_RESULTS] if s>0.6]
    def _search_memories(self, query_text, limit=5, source=None):
        cands = self._memory
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
    def _build_memory_context(self, thread_id, user_id, query_text, video_context=""):
        parts = []
        if video_context: parts.append(video_context)
        perm = self._load_json(PERMANENT_MEMORY_FILE, [])
        if perm: parts.append("【Bot的自我认知】\n" + "\n".join([f"[{p.get('time','?')}] {p['text']}" for p in perm[-20:]]))
        upc = self._get_user_profile_context(user_id)
        if upc: parts.append(upc)
        td = self._get_thread_memories(thread_id)
        if td:
            if len(td)>THREAD_COMPRESS_THRESHOLD: parts.append("【本评论线近期对话】\n"+"\n".join(td[-4:]))
            else: parts.append("【本评论线对话】\n"+"\n".join(td))
        else:
            sd = self._get_user_semantic_memories(user_id, query_text)
            if sd: parts.append("【相关历史记忆】\n"+"\n".join(sd))
        return "\n\n".join(parts) if parts else ""

    # ===== 视频信息 =====
    def _oid_to_bvid(self, oid):
        try:
            resp = requests.get("https://api.bilibili.com/x/web-interface/view", headers=self._headers(), params={"aid":oid}, timeout=10)
            d = resp.json()
            if d["code"]==0: return d["data"].get("bvid","")
        except: pass
        return ""
    def _get_video_info(self, oid):
        try:
            resp = requests.get("https://api.bilibili.com/x/web-interface/view", headers=self._headers(), params={"aid":oid}, timeout=10)
            d = resp.json()
            if d["code"]==0:
                v=d["data"]; return {"bvid":v.get("bvid",""),"title":v.get("title",""),"desc":v.get("desc",""),"owner_name":v.get("owner",{}).get("name",""),"owner_mid":v.get("owner",{}).get("mid",""),"tname":v.get("tname","")}
        except Exception as e: logger.error(f"[BiliBot] 获取视频信息失败：{e}")
        return None
    def _get_video_context(self, oid, comment_type):
        if comment_type!=1: return ""
        vc = self._load_json(VIDEO_MEMORY_FILE, {})
        bvid = self._oid_to_bvid(oid)
        if not bvid: return ""
        if bvid in vc:
            c=vc[bvid]; return f"【当前视频】标题：{c['title']} | UP主：{c['owner_name']} | {c.get('analysis','')}"
        vi = self._get_video_info(oid)
        if not vi: return ""
        analysis = f"视频《{vi['title']}》，UP主：{vi['owner_name']}，分区：{vi['tname']}。简介：{vi.get('desc','')[:200]}"
        vc[bvid] = {"title":vi["title"],"desc":vi.get("desc","")[:200],"owner_name":vi["owner_name"],"owner_mid":vi["owner_mid"],"tname":vi["tname"],"analysis":analysis,"time":datetime.now().strftime("%Y-%m-%d %H:%M")}
        self._save_json(VIDEO_MEMORY_FILE, vc)
        return f"【当前视频】标题：{vi['title']} | UP主：{vi['owner_name']} | {analysis}"

    # ===== Cookie管理 =====
    def check_cookie(self):
        s = self.config.get("SESSDATA","")
        if not s: return False,"SESSDATA 为空"
        try:
            resp = requests.get(BILI_NAV_URL, headers=self._headers(), timeout=10); d=resp.json()
            if d["code"]==0: return True,f"✅ {d['data'].get('uname','?')} (UID:{d['data'].get('mid','')}) LV{d['data'].get('level_info',{}).get('current_level',0)}"
            return False,f"❌ Cookie 已失效 (code:{d['code']})"
        except Exception as e: return False,f"❌ 检查失败: {e}"
    def check_need_refresh(self):
        try:
            resp = requests.get(BILI_COOKIE_INFO_URL, params={"csrf":self.config.get("BILI_JCT","")}, headers=self._headers(), timeout=10); d=resp.json()
            if d["code"]!=0: return False,f"检查失败: {d.get('message','')}"
            return (True,"需要刷新") if d["data"].get("refresh",False) else (False,"Cookie 仍然有效")
        except Exception as e: return False,f"检查出错: {e}"
    def _generate_correspond_path(self, ts):
        from cryptography.hazmat.primitives.asymmetric import padding; from cryptography.hazmat.primitives import hashes, serialization
        pk = serialization.load_pem_public_key(BILI_RSA_PUBLIC_KEY.encode())
        return pk.encrypt(f"refresh_{ts}".encode(), padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),algorithm=hashes.SHA256(),label=None)).hex()
    def refresh_cookie(self):
        rt=self.config.get("REFRESH_TOKEN","")
        if not rt: return False,"没有 REFRESH_TOKEN"
        bjct=self.config.get("BILI_JCT","")
        if not self.config.get("SESSDATA",""): return False,"SESSDATA 为空"
        try:
            need,msg = self.check_need_refresh()
            if not need: return True,msg
            cp = self._generate_correspond_path(int(time.time()*1000))
            resp = requests.get(f"https://www.bilibili.com/correspond/1/{cp}", headers=self._headers(), timeout=10)
            m = re.search(r'<div\s+id="1-name"\s*>([^<]+)</div>', resp.text)
            if not m: return False,"无法提取 refresh_csrf"
            resp = requests.post(BILI_COOKIE_REFRESH_URL, headers=self._headers(), data={"csrf":bjct,"refresh_csrf":m.group(1).strip(),"source":"main_web","refresh_token":rt}, timeout=10)
            result=resp.json()
            if result["code"]!=0: return False,f"刷新失败: {result.get('message',result['code'])}"
            updates={}
            nrt=result["data"].get("refresh_token","")
            if nrt: updates["REFRESH_TOKEN"]=nrt
            for c in resp.cookies:
                if c.name=="SESSDATA": updates["SESSDATA"]=c.value
                elif c.name=="bili_jct": updates["BILI_JCT"]=c.value
                elif c.name=="DedeUserID": updates["DEDE_USER_ID"]=c.value
            if "SESSDATA" not in updates: return False,"刷新响应中未找到新 SESSDATA"
            try:
                ch=dict(self._headers()); ch["Cookie"]=f"SESSDATA={updates['SESSDATA']}; bili_jct={updates.get('BILI_JCT',bjct)}"
                requests.post(BILI_COOKIE_CONFIRM_URL, headers=ch, data={"csrf":updates.get("BILI_JCT",bjct),"refresh_token":rt}, timeout=10)
            except: pass
            for k,v in updates.items(): self.config[k]=v
            self.config.save_config()
            return True,f"✅ Cookie 刷新成功！"
        except Exception as e: return False,f"刷新出错: {e}"

    # ===== WBI签名 =====
    def _get_wbi_keys(self):
        resp=requests.get(BILI_NAV_URL,headers=self._headers(),timeout=10); d=resp.json()["data"]["wbi_img"]
        return d["img_url"].rsplit("/",1)[1].split(".")[0], d["sub_url"].rsplit("/",1)[1].split(".")[0]
    def _get_mixin_key(self, orig): return reduce(lambda s,i:s+orig[i], MIXIN_KEY_ENC_TAB, "")[:32]
    def sign_wbi_params(self, params):
        try:
            ik,sk=self._get_wbi_keys(); mk=self._get_mixin_key(ik+sk); params["wts"]=int(time.time()); params=dict(sorted(params.items()))
            params["w_rid"]=hashlib.md5(("&".join(f"{k}={v}" for k,v in params.items())+mk).encode()).hexdigest(); return params
        except: return params

    # ===== 扫码登录 =====
    async def _qr_login_generate(self):
        try:
            resp=requests.get(BILI_QR_GENERATE_URL, headers={"User-Agent":USER_AGENT}, timeout=10); d=resp.json()
            if d["code"]==0: return d["data"]["url"],d["data"]["qrcode_key"]
        except Exception as e: logger.error(f"生成二维码失败: {e}")
        return None,None
    async def _qr_login_poll(self, qrcode_key):
        try:
            resp=requests.get(BILI_QR_POLL_URL, params={"qrcode_key":qrcode_key}, headers={"User-Agent":USER_AGENT}, timeout=10)
            d=resp.json()["data"]; code=d["code"]
            mm={0:"登录成功",86038:"二维码已失效",86090:"已扫码，请在手机上确认",86101:"等待扫码中..."}
            cookies={}
            if code==0:
                url=d.get("url",""); rt=d.get("refresh_token","")
                if url:
                    from urllib.parse import urlparse,parse_qs; p=parse_qs(urlparse(url).query)
                    cookies={"SESSDATA":p.get("SESSDATA",[""])[0],"bili_jct":p.get("bili_jct",[""])[0],"DedeUserID":p.get("DedeUserID",[""])[0],"REFRESH_TOKEN":rt}
                for c in resp.cookies:
                    if c.name in ("SESSDATA","bili_jct","DedeUserID"): cookies[c.name]=c.value
            return code, mm.get(code,f"未知({code})"), cookies
        except Exception as e: return -1,f"轮询失败: {e}",{}

    # ===== LLM =====
    async def _llm_call(self, prompt, system_prompt="", max_tokens=300):
        try:
            pid = self.config.get("LLM_PROVIDER_ID","")
            if not pid:
                # 没选provider，用默认
                provider = self.context.get_using_provider()
                if not provider: logger.error("[BiliBot] 没有可用的 LLM provider"); return None
                resp = await provider.text_chat(prompt=prompt, session_id="bili_reply", contexts=[], system_prompt=system_prompt or "你是一个AI助手。")
                if resp:
                    if hasattr(resp,'completion_text') and resp.completion_text: return resp.completion_text.strip()
                    elif hasattr(resp,'result_chain') and resp.result_chain:
                        for comp in resp.result_chain.chain:
                            if hasattr(comp,'text') and comp.text: return comp.text.strip()
                return None
            else:
                full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
                resp = await self.context.llm_generate(chat_provider_id=pid, prompt=full_prompt)
                return resp.completion_text.strip() if resp and resp.completion_text else None
        except Exception as e: logger.error(f"[BiliBot] LLM 调用失败: {e}"); return None
    def _get_system_prompt(self):
        if self.config.get("USE_ASTRBOT_PERSONA",True):
            try:
                ps = self.context.provider_manager.personas
                if ps: return ps[0].prompt
            except: pass
        return self.config.get("CUSTOM_SYSTEM_PROMPT","你是一个B站UP主的AI助手。")
    async def _generate_reply(self, content, mid, username, thread_id, oid, comment_type):
        try:
            sp = self._get_system_prompt(); on = self.config.get("OWNER_NAME","") or "主人"
            is_owner = str(mid)==str(self.config.get("OWNER_MID",""))
            cs = self._affection.get(str(mid),0); lv = self._get_level(cs, mid)
            lp = self._get_level_prompts()[lv]
            vc = self._get_video_context(oid, comment_type)
            mc = self._build_memory_context(thread_id, mid, content, vc)
            ms = f"\n\n【记忆参考】\n{mc}" if mc else ""
            mood,mp = self._get_today_mood(); fest = self._get_festival_prompt()
            fs = f"\n特殊日期：{fest}" if fest else ""
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            prompt = f"""{lp}\n\n【底线】拒绝：表白暧昧、引战、黄赌毒政治。\n\n【今日状态】{mood} — {mp}{fs}\n\n当前时间：{now}{ms}\n\n「{username}」{'（这是'+on+'）' if is_owner else ''}的评论：「{content}」\n\n请以JSON格式回复：\n{{"score_delta": 数字, "reply": "回复内容", "impression": "印象", "user_facts": ["个人信息"], "permanent_memory": "永久记忆(没有则留空)"}}\n\nscore_delta：友善+2，普通+1，不友善-2，辱骂-5。reply不超过50字。"""
            rt = await self._llm_call(prompt, system_prompt=sp)
            if not rt: return None
            rt = rt.replace("```json","").replace("```","").strip()
            try: r = json.loads(rt)
            except:
                m = re.search(r'\{.*\}', rt, re.DOTALL)
                r = json.loads(m.group()) if m else {"score_delta":1,"reply":rt[:50],"impression":"","user_facts":[],"permanent_memory":""}
            return {"score_delta":r.get("score_delta",1),"reply":r.get("reply",""),"impression":r.get("impression",""),"user_facts":r.get("user_facts",[]),"permanent_memory":r.get("permanent_memory","")}
        except Exception as e: logger.error(f"[BiliBot] 回复生成失败: {e}\n{traceback.format_exc()}"); return None

    # ===== B站API =====
    def _send_reply(self, oid, rpid, reply_type, content):
        try:
            resp = requests.post(BILI_REPLY_URL, headers=self._headers(), data={"oid":oid,"type":reply_type,"root":rpid,"parent":rpid,"message":content,"csrf":self.config.get("BILI_JCT","")}, timeout=10)
            d=resp.json()
            if d["code"]==0: return True
            elif d["code"]==-101: logger.error("[BiliBot] SESSDATA 失效！")
            elif d["code"]==-111: logger.error("[BiliBot] bili_jct 错误！")
            else: logger.warning(f"[BiliBot] 回复失败: {d.get('message',d['code'])}")
            return False
        except Exception as e: logger.error(f"[BiliBot] 回复出错: {e}"); return False
    def get_followings(self, mid=None):
        target = mid or self.config.get("DEDE_USER_ID","")
        try:
            resp = requests.get("https://api.bilibili.com/x/relation/followings", headers=self._headers(), params={"vmid":target,"ps":50,"pn":1}, timeout=10)
            d=resp.json()
            if d["code"]==0: return [i["mid"] for i in d.get("data",{}).get("list",[])]
        except Exception as e: logger.error(f"[BiliBot] 获取关注列表失败: {e}")
        return []

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
                valid,info=self.check_cookie(); yield event.plain_result(f"✅ 登录成功！\n{info}")
                if not self._running: await self._start_bot(); yield event.plain_result("🚀 后台任务已自动启动")
                return
            elif code==86090: yield event.plain_result(f"📱 {msg}"); await asyncio.sleep(2)
            elif code==86101: yield event.plain_result(f"⏳ {msg}"); await asyncio.sleep(3)
            elif code==86038: self._login_qrcode_key=None; yield event.plain_result(f"❌ {msg}，请重新 /bili登录"); return
            else: yield event.plain_result(f"❌ {msg}"); return
        yield event.plain_result("⏳ 还没确认成功，请在手机上确认后再发 /bili确认")
    @filter.command("bili状态")
    async def cmd_status(self, event: AstrMessageEvent):
        valid,info=self.check_cookie(); mood,_=self._get_today_mood()
        mc=len(self._memory); pc=len(self._load_json(USER_PROFILE_FILE,{})); pmc=len(self._load_json(PERMANENT_MEMORY_FILE,[]))
        lines = [f"📺 BiliBot 状态","━━━━━━━━━━━━",f"🍪 {info}",f"{'🟢 运行中' if self._running else '🔴 未运行'}",f"🧠 记忆:{mc}条 | 💎永久:{pmc}条 | 👤档案:{pc}个",f"🎭 心情:{mood}",f"回复:{'✅' if self.config.get('ENABLE_REPLY',True) else '❌'} 好感:{'✅' if self.config.get('ENABLE_AFFECTION',True) else '❌'} 心情:{'✅' if self.config.get('ENABLE_MOOD',True) else '❌'}"]
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
            tm = {"回复":"ENABLE_REPLY","主动":"ENABLE_PROACTIVE","动态":"ENABLE_DYNAMIC","好感":"ENABLE_AFFECTION","心情":"ENABLE_MOOD","点赞":"PROACTIVE_LIKE","投币":"PROACTIVE_COIN","收藏":"PROACTIVE_FAV","关注":"PROACTIVE_FOLLOW","评论":"PROACTIVE_COMMENT"}
            lines = ["可切换功能："] + [f"  {n} ({'✅' if self.config.get(k,True) else '❌'})" for n,k in tm.items()] + ["","用法: /bili开关 回复"]
            yield event.plain_result("\n".join(lines)); return
        name=parts[1].strip()
        tm = {"回复":"ENABLE_REPLY","主动":"ENABLE_PROACTIVE","动态":"ENABLE_DYNAMIC","好感":"ENABLE_AFFECTION","心情":"ENABLE_MOOD","点赞":"PROACTIVE_LIKE","投币":"PROACTIVE_COIN","收藏":"PROACTIVE_FAV","关注":"PROACTIVE_FOLLOW","评论":"PROACTIVE_COMMENT"}
        key=tm.get(name)
        if not key: yield event.plain_result(f"❌ 不认识：{name}"); return
        cur=self.config.get(key,True); self.config[key]=not cur; self.config.save_config()
        yield event.plain_result(f"{name}: {'✅ 已开启' if not cur else '❌ 已关闭'}")
    @filter.command("bili刷新")
    async def cmd_refresh_cookie(self, event: AstrMessageEvent):
        yield event.plain_result("🔄 刷新中..."); _,msg=self.refresh_cookie(); yield event.plain_result(msg)
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
        success = self._block_user(int(uid))
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
            resp = requests.post("https://api.bilibili.com/x/relation/modify", headers=self._headers(), data={"fid":uid,"act":6,"re_src":11,"csrf":self.config.get("BILI_JCT","")}, timeout=10)
            api_ok = resp.json()["code"]==0
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
    @filter.command("bili帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result("📺 BiliBot 命令\n━━━━━━━━━━━━\n/bili登录 — 扫码登录\n/bili确认 — 确认扫码\n/bili状态 — 运行状态\n/bili启动 — 启动\n/bili停止 — 停止\n/bili开关 — 功能开关\n/bili刷新 — 刷新Cookie\n/bili记忆 — 搜索记忆\n/bili好感 — 好感度\n/bili拉黑 — 手动拉黑\n/bili解黑 — 解除拉黑\n/bili黑名单 — 查看黑名单\n/bili帮助 — 本帮助\n━━━━━━━━━━━━\n💡 首次用 /bili登录")

    # ===== 后台任务 =====
    async def _auto_start(self):
        await asyncio.sleep(3)
        valid,_=self.check_cookie()
        if valid: await self._start_bot(); logger.info("[BiliBot] 自动启动")
        else: logger.warning("[BiliBot] Cookie无效")
    async def _start_bot(self):
        if self._running: return
        self._running=True; self._task=asyncio.create_task(self._main_loop()); logger.info("[BiliBot] 启动")
    async def _stop_bot(self):
        self._running=False
        if self._task: self._task.cancel(); self._task=None
        logger.info("[BiliBot] 停止")
    async def _main_loop(self):
        logger.info("[BiliBot] 主循环开始")
        while self._running:
            try:
                h=datetime.now().hour; ss=self.config.get("SLEEP_START",2); se=self.config.get("SLEEP_END",8)
                if ss<=h<se: await asyncio.sleep(60); continue
                ci=self.config.get("COOKIE_CHECK_INTERVAL",6)*3600
                if time.time()-self._last_cookie_check>ci: await self._check_and_refresh_cookie(); self._last_cookie_check=time.time()
                if self.config.get("ENABLE_REPLY",True): await self._poll_and_reply()
                await asyncio.sleep(self.config.get("POLL_INTERVAL",20))
            except asyncio.CancelledError: break
            except Exception as e: logger.error(f"[BiliBot] 主循环出错: {e}\n{traceback.format_exc()}"); await asyncio.sleep(30)
        self._running=False
    async def _check_and_refresh_cookie(self):
        valid,info=self.check_cookie()
        if valid: logger.info(f"[BiliBot] Cookie OK: {info}"); return
        logger.warning(f"[BiliBot] Cookie 失效: {info}")
        if self.config.get("COOKIE_AUTO_REFRESH",True):
            ok,msg=self.refresh_cookie(); logger.info(f"[BiliBot] 刷新{'成功' if ok else '失败'}: {msg}")
    async def _poll_and_reply(self):
        if time.time() < self._llm_cooldown_until:
            return  # LLM冷却中，跳过
        try:
            resp=requests.get(BILI_NOTIFY_URL, headers=self._headers(), params={"ps":10,"pn":1}, timeout=10); d=resp.json()
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
                result = await self._generate_reply(content, mid, username, thread_id, oid, ct)
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
                        self._send_reply(oid,rpid,ct,"我不想和你说话了。"); self._block_user(int(mid))
                        logger.info(f"[BiliBot] 🚫 拉黑 {username}"); replied.add(rpid); self._save_json(REPLIED_FILE,list(replied)); continue
                if imp or uf: self._update_user_profile(mid, impression=imp or None, new_facts=uf or None)
                if pm:
                    perm=self._load_json(PERMANENT_MEMORY_FILE,[])
                    if len(perm)<20: perm.append({"text":pm,"time":datetime.now().strftime("%Y-%m-%d %H:%M")}); self._save_json(PERMANENT_MEMORY_FILE,perm)
                logger.info(f"[BiliBot] 💬 {username}: {ai_reply[:50]}")
                success=self._send_reply(oid,rpid,ct,ai_reply)
                if success:
                    self._save_memory_record(rpid,thread_id,mid,username,content,ai_reply); count+=1
                    await self._compress_user_memory(mid,username)
                replied.add(rpid); self._save_json(REPLIED_FILE,list(replied))
        except Exception as e: logger.error(f"[BiliBot] 轮询出错: {e}\n{traceback.format_exc()}")

    async def terminate(self):
        await self._stop_bot(); logger.info("[BiliBot] 已停用")
