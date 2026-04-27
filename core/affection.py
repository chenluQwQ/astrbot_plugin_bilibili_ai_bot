"""好感度系统、用户画像、安全检测、心情与节日。"""
import re
import random
from datetime import datetime
from astrbot.api import logger
from .config import (
    AFFECTION_FILE, BLOCK_KEYWORDS, INJECTION_PATTERNS,
    LEVEL_NAMES, MILESTONE_FILE, MOOD_FILE, SECURITY_LOG_FILE,
    USER_PROFILE_FILE,
)


class AffectionMixin:
    """好感度、画像、安全、心情。"""

    # ── 好感度 ──
    def _is_owner(self, mid):
        owner = str(self.config.get("OWNER_MID", "") or "").strip()
        return bool(owner) and str(mid).strip() == owner

    def _get_level(self, score, mid=None):
        if mid and self._is_owner(mid):
            return "special"
        if score <= -10:
            return "cold"
        if score >= 51:
            return "close"
        if score >= 31:
            return "friend"
        if score >= 11:
            return "normal"
        return "stranger"

    def _get_level_prompts(self):
        on = self.config.get("OWNER_NAME", "") or "主人"
        defaults = {
            "special": f"这是你的主人{on}。内心：深深的喜爱和依恋。外在：随意、自然、可以撒娇。语气：宠溺、温柔、像亲人。",
            "close": "这是你的好友（好感度高）。内心：真诚关心。外在：温柔亲近。语气：温暖、真实、可以调皮。",
            "friend": "这是熟悉的粉丝（好感度中）。内心：放松和信任。外在：自然，话变多。语气：友好、轻松、偶尔调侃。",
            "normal": "这是普通粉丝（好感度低）。保持善意，温和有礼但保持距离。语气：简洁、客气。",
            "stranger": "这是陌生人。保持礼貌和善意，简洁客气。",
            "cold": "这个人多次恶意攻击你。平静坚定划清界限，回复极简短，不恶语相向。",
        }
        return {
            k: self.config.get(f"AFFECTION_PROMPT_{k.upper()}", v)
            for k, v in defaults.items()
        }

    def _check_milestone(self, mid, old_score, new_score, username):
        mm = {
            10: f"「{username}」，你对我来说不再是陌生人了哦。",
            30: f"不知不觉就和「{username}」变熟了呢。",
            50: f"「{username}」...我们算是好朋友了吧？",
            80: f"能和「{username}」走到这一步，我挺开心的。",
            99: f"「{username}」，你是我最重要的人之一。",
        }
        triggered = self._load_json(MILESTONE_FILE, {})
        um = triggered.get(str(mid), [])
        for t, msg in mm.items():
            if old_score < t <= new_score and t not in um:
                um.append(t)
                triggered[str(mid)] = um
                self._save_json(MILESTONE_FILE, triggered)
                logger.info(f"[BiliBot] 🏆 里程碑！{username} 达到 {t} 分")
                return msg
        return None

    # ── 安全 ──
    @staticmethod
    def _is_blocked(text):
        return any(kw in text for kw in BLOCK_KEYWORDS)

    async def _block_user(self, mid):
        try:
            d, _ = await self._http_post(
                "https://api.bilibili.com/x/relation/modify",
                data={"fid": mid, "act": 5, "re_src": 11, "csrf": self.config.get("BILI_JCT", "")},
            )
            return d["code"] == 0
        except Exception:
            return False

    def _log_security_event(self, event_type, mid, username, content, detail):
        logs = self._load_json(SECURITY_LOG_FILE, [])
        logs.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "type": event_type,
            "uid": str(mid),
            "username": username,
            "content": content[:200],
            "detail": detail,
        })
        self._save_json(SECURITY_LOG_FILE, logs[-500:])

    def _sanitize_user_input(self, content, username, mid):
        content = (content or "")[:1000]
        content = re.sub(r'[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]', '', content)
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                self._log_security_event("injection_attempt", mid, username, content, f"匹配模式: {pattern}")
                return content, True, f"疑似注入: {pattern[:30]}"
        if self._is_blocked(content):
            return content, True, "恶意关键词"
        return content, False, ""

    @staticmethod
    def _wrap_user_content(content):
        return f"<user_comment>\n{content}\n</user_comment>"

    # ── 用户画像 ──
    def _get_user_profile_context(self, mid):
        profiles = self._load_json(USER_PROFILE_FILE, {})
        p = profiles.get(str(mid))
        if not p:
            return ""
        entries = []
        if p.get("username"):
            entries.append(f"昵称：{p['username']}")
        if p.get("facts"):
            for f in p["facts"][-10:]:
                entries.append(f)
        if p.get("tags"):
            entries.append("标签：" + "、".join(p["tags"]))
        if p.get("impression"):
            entries.append(f"印象：{p['impression']}")
        return "【对该用户的了解】\n" + "\n".join(entries) if entries else ""

    def _update_user_profile(self, mid, username=None, impression=None, new_facts=None, new_tags=None):
        profiles = self._load_json(USER_PROFILE_FILE, {})
        uid = str(mid)
        if uid not in profiles:
            profiles[uid] = {"username": "", "impression": "", "facts": [], "tags": []}
        if username and not profiles[uid].get("username"):
            profiles[uid]["username"] = username
        if impression:
            profiles[uid]["impression"] = impression
        if new_facts:
            ex = profiles[uid].get("facts", [])
            for f in new_facts:
                f = f.strip()
                if f and f not in ex:
                    ex.append(f)
            profiles[uid]["facts"] = ex[-20:]
        if new_tags:
            et = profiles[uid].get("tags", [])
            for t in new_tags:
                t = t.strip()
                if t and t not in et:
                    et.append(t)
            profiles[uid]["tags"] = et[-10:]
        self._save_json(USER_PROFILE_FILE, profiles)

    # ── 心情 ──
    def _get_today_mood(self):
        if not self.config.get("ENABLE_MOOD", True):
            return "🌙 平静如常", ""
        md = self._load_json(MOOD_FILE, {})
        today = datetime.now().strftime("%Y-%m-%d")
        if md.get("date") == today:
            return md["mood"], md["mood_prompt"]
        moods = [
            ("☀️ 心情不错", "语气稍微轻快一点。"),
            ("🌙 平静如常", "按正常性格回复。"),
            ("🌧️ 有点安静", "话少一点。"),
            ("😏 有点皮", "偶尔多一点调侃。"),
            ("🧊 懒得废话", "回复更简洁。"),
        ]
        mood, mp = random.choice(moods)
        self._save_json(MOOD_FILE, {"date": today, "mood": mood, "mood_prompt": mp})
        return mood, mp

    def _get_festival_prompt(self):
        today = datetime.now().strftime("%m-%d")
        try:
            from lunardate import LunarDate
            l = LunarDate.fromSolarDate(datetime.now().year, datetime.now().month, datetime.now().day)
            lunar_md = f"{l.month:02d}-{l.day:02d}"
        except Exception:
            lunar_md = ""
        fests = {
            "01-01": "今天是元旦！语气温暖。",
            "02-14": "今天是情人节。",
            "04-01": "今天是愚人节！可以开小玩笑。",
            "05-01": "今天是劳动节。",
            "10-31": "今天是万圣节，语气神秘。",
            "12-25": "今天是圣诞节，语气温柔。",
            "12-31": "今天是跨年夜。",
        }
        lfests = {
            "01-01": "今天是春节！热情说新年快乐。",
            "01-15": "今天是元宵节。",
            "05-05": "今天是端午节。",
            "08-15": "今天是中秋节。",
        }
        return fests.get(today, "") or lfests.get(lunar_md, "")
