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

    def _is_reply_whitelisted(self, mid):
        """必回白名单：这些 UID 不受概率/语义去重影响，一定回复。"""
        wl = self.config.get("REPLY_ALWAYS_UIDS", []) or []
        return str(mid).strip() in {str(x).strip() for x in wl if str(x).strip()}

    def _is_block_whitelisted(self, mid):
        """拉黑白名单：主人 + 配置的 UID 永不被自动拉黑。"""
        if self._is_owner(mid):
            return True
        wl = self.config.get("BLOCK_WHITELIST_UIDS", []) or []
        return str(mid).strip() in {str(x).strip() for x in wl if str(x).strip()}

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
    @staticmethod
    def _normalize_user_profile(profile):
        p = dict(profile) if isinstance(profile, dict) else {}
        p.setdefault("username", "")
        p.setdefault("impression", "")
        p.setdefault("facts", [])
        p.setdefault("tags", [])
        legacy_encounters = p.get("video_encounters", [])
        refs = p.setdefault("video_refs", [])
        if not isinstance(refs, list):
            refs = []
            p["video_refs"] = refs
        # 旧数据兼容：video_encounters 表示曾在该视频评论区交流。
        known = {(str(item.get("bvid", "")), item.get("relation", "")) for item in refs if isinstance(item, dict)}
        for item in legacy_encounters:
            if not isinstance(item, dict) or not item.get("bvid"):
                continue
            key = (str(item["bvid"]), "commented_under")
            if key not in known:
                refs.append({
                    "bvid": str(item["bvid"]),
                    "title": str(item.get("title", "")),
                    "relation": "commented_under",
                    "time": str(item.get("time", "")),
                })
                known.add(key)
        p["video_refs"] = refs[-50:]
        p.pop("video_encounters", None)
        live = p.setdefault("live", {})
        if not isinstance(live, dict):
            live = {}
            p["live"] = live
        live.setdefault("event_counts", {})
        live.setdefault("memory_refs", [])
        return p

    def _get_user_profile_context(self, mid):
        profiles = self._load_json(USER_PROFILE_FILE, {})
        p = self._normalize_user_profile(profiles.get(str(mid))) if profiles.get(str(mid)) else None
        if not p:
            return ""
        entries = []
        if p.get("username"):
            entries.append(f"昵称：{p['username']}")
        refs = p.get("video_refs", [])
        relation_labels = {
            "commented_under": "曾在这些视频下交流",
            "uploaded_by": "该用户发布的视频",
            "about_user": "内容与该用户有关的视频",
        }
        for relation, label in relation_labels.items():
            recent = [item for item in refs if isinstance(item, dict) and item.get("relation") == relation][-5:]
            ref_texts = [
                f"《{item.get('title') or item.get('bvid')}》({item.get('bvid')}, {item.get('time', '?')})"
                for item in recent if item.get("bvid")
            ]
            if ref_texts:
                entries.append(f"{label}：{'；'.join(ref_texts)}")
        live = p.get("live") if isinstance(p.get("live"), dict) else {}
        counts = live.get("event_counts") if isinstance(live.get("event_counts"), dict) else {}
        if counts:
            count_text = "、".join(f"{key}:{value}" for key, value in counts.items() if value)
            if count_text:
                entries.append(f"直播互动：{count_text}；最近出现：{live.get('last_seen', '未知')}")
        if p.get("facts"):
            for f in p["facts"][-10:]:
                entries.append(f)
        if p.get("tags"):
            entries.append("标签：" + "、".join(p["tags"]))
        if p.get("impression"):
            entries.append(f"印象：{p['impression']}")
        return "【对该用户的了解】\n" + "\n".join(entries) if entries else ""

    def _update_user_profile(self, mid, username=None, impression=None, new_facts=None, new_tags=None, video_encounter=None, video_ref=None, live_event=None):
        """更新用户画像；视频与直播只保存轻量引用，不复制正文记忆。"""
        profiles = self._load_json(USER_PROFILE_FILE, {})
        uid = str(mid)
        profiles[uid] = self._normalize_user_profile(profiles.get(uid))
        if username:
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
        if video_encounter and video_encounter.get("bvid"):
            video_ref = {
                "bvid": video_encounter["bvid"],
                "title": video_encounter.get("title", ""),
                "time": video_encounter.get("time", ""),
                "relation": "commented_under",
            }
        if video_ref and video_ref.get("bvid"):
            refs = profiles[uid].setdefault("video_refs", [])
            ref = {
                "bvid": str(video_ref["bvid"]),
                "title": str(video_ref.get("title", "")),
                "relation": str(video_ref.get("relation") or "related"),
                "time": str(video_ref.get("time") or datetime.now().strftime("%Y-%m-%d")),
            }
            key = (ref["bvid"], ref["relation"])
            refs = [item for item in refs if not (isinstance(item, dict) and (str(item.get("bvid", "")), item.get("relation", "")) == key)]
            refs.append(ref)
            profiles[uid]["video_refs"] = refs[-50:]
        if live_event:
            live = profiles[uid].setdefault("live", {"event_counts": {}, "memory_refs": []})
            counts = live.setdefault("event_counts", {})
            event_type = str(live_event.get("event_type") or "interaction")
            counts[event_type] = int(counts.get(event_type, 0) or 0) + 1
            live["last_seen"] = str(live_event.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M"))
            if live_event.get("session_id"):
                live["last_session_id"] = str(live_event["session_id"])
            memory_ref = str(live_event.get("memory_ref") or "")
            if memory_ref:
                memory_refs = [str(item) for item in live.setdefault("memory_refs", []) if item]
                if memory_ref in memory_refs:
                    memory_refs.remove(memory_ref)
                memory_refs.append(memory_ref)
                live["memory_refs"] = memory_refs[-30:]
        self._save_json(USER_PROFILE_FILE, profiles)

    def _link_video_to_user_profile(self, user_id, username, bvid, title="", relation="uploaded_by"):
        if str(user_id or "").strip() in {"", "0"} or not bvid:
            return
        self._update_user_profile(
            str(user_id),
            username=username or None,
            video_ref={
                "bvid": str(bvid),
                "title": str(title or ""),
                "relation": relation,
                "time": datetime.now().strftime("%Y-%m-%d"),
            },
        )

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
