"""用户画像：每个 B站 UID 一份，昵称、印象、事实、标签、视频引用、直播互动、关系计数。

``user_profiles.json`` 只由这里读写。画像里每条信息都记着来源域（评论、私信、直播……），
读的时候按读者所在的域过滤，私信里说的不会出现在公开评论的提示词里。
数据库里的 ``profiles`` 表只记互动次数，不是画像的真身，这里不碰它。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from ..security import readable_scopes
from ..storage.json_file import JsonFile

RELATION_LABELS = {
    "commented_under": "曾在这些视频下交流",
    "uploaded_by": "该用户发布的视频",
    "about_user": "内容与该用户有关的视频",
}


def normalize(profile):
    """补齐字段、把旧格式改成现在的样子；不改传进来的对象的顶层。"""
    p = dict(profile) if isinstance(profile, dict) else {}
    p.setdefault("username", "")
    p.setdefault("impression", "")
    p.setdefault("facts", [])
    p.setdefault("tags", [])
    impressions = p.setdefault("impressions", {})
    if not isinstance(impressions, dict):
        impressions = {}
        p["impressions"] = impressions
    # 老版本只有一个全局字段；插件历史上以公开评论为主，迁移时保守归入
    # 公开评论域。此后所有新画像信息都记录精确来源域。
    if p.get("impression") and not impressions:
        impressions["bili_comment"] = str(p["impression"])
    fact_scopes = p.setdefault("fact_scopes", {})
    if not isinstance(fact_scopes, dict):
        fact_scopes = {}
        p["fact_scopes"] = fact_scopes
    for fact in p.get("facts", []):
        if isinstance(fact, str) and fact:
            fact_scopes.setdefault(fact, "bili_comment")
    tag_scopes = p.setdefault("tag_scopes", {})
    if not isinstance(tag_scopes, dict):
        tag_scopes = {}
        p["tag_scopes"] = tag_scopes
    for tag in p.get("tags", []):
        if isinstance(tag, str) and tag:
            tag_scopes.setdefault(tag, "bili_comment")
    legacy_encounters = p.get("video_encounters", [])
    refs = p.setdefault("video_refs", [])
    if not isinstance(refs, list):
        refs = []
        p["video_refs"] = refs
    for item in refs:
        if isinstance(item, dict):
            item.setdefault("scope", "bili_comment")
    # 旧数据兼容：video_encounters 表示曾在该视频评论区交流。
    known = {
        (
            str(item.get("bvid", "")),
            item.get("relation", ""),
            item.get("scope", "bili_comment"),
        )
        for item in refs if isinstance(item, dict)
    }
    for item in legacy_encounters:
        if not isinstance(item, dict) or not item.get("bvid"):
            continue
        key = (str(item["bvid"]), "commented_under", "bili_comment")
        if key not in known:
            refs.append({
                "bvid": str(item["bvid"]),
                "title": str(item.get("title", "")),
                "relation": "commented_under",
                "time": str(item.get("time", "")),
                "scope": "bili_comment",
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


class UserProfiles:
    def __init__(self, file: JsonFile):
        self._file = file

    def _all(self) -> dict:
        return self._file.read({})

    # ── 读 ──
    def raw(self, uid) -> dict:
        """文件里原样的那份（没整理过），没有就是空字典。"""
        profile = self._all().get(str(uid))
        return profile if isinstance(profile, dict) else {}

    def get(self, uid) -> dict | None:
        """整理过的画像副本；没有这个人返回 ``None``。"""
        profile = self._all().get(str(uid))
        if not isinstance(profile, dict):
            return None
        return deepcopy(normalize(profile))

    def all(self) -> dict:
        """整个文件原样返回，WebUI 列表和统计用。"""
        profiles = self._all()
        return profiles if isinstance(profiles, dict) else {}

    def __len__(self) -> int:
        return len(self.all())

    def __contains__(self, uid) -> bool:
        return str(uid) in self.all()

    def context(self, mid, reader_scope="bili_comment") -> str:
        """给提示词用的「对该用户的了解」，只放读者所在域能看到的部分。"""
        profiles = self._all()
        p = normalize(profiles.get(str(mid))) if profiles.get(str(mid)) else None
        if not p:
            return ""
        allowed_scopes = {scope.value for scope in readable_scopes(reader_scope)}
        entries = []
        if p.get("username"):
            entries.append(f"昵称：{p['username']}")
        refs = p.get("video_refs", [])
        for relation, label in RELATION_LABELS.items():
            recent = [
                item for item in refs
                if isinstance(item, dict)
                and item.get("relation") == relation
                and str(item.get("scope", "bili_comment")) in allowed_scopes
            ][-5:]
            ref_texts = [
                f"《{item.get('title') or item.get('bvid')}》({item.get('bvid')}, {item.get('time', '?')})"
                for item in recent if item.get("bvid")
            ]
            if ref_texts:
                entries.append(f"{label}：{'；'.join(ref_texts)}")
        live = p.get("live") if isinstance(p.get("live"), dict) else {}
        counts = live.get("event_counts") if isinstance(live.get("event_counts"), dict) else {}
        if counts and "bili_live" in allowed_scopes:
            count_text = "、".join(f"{key}:{value}" for key, value in counts.items() if value)
            if count_text:
                entries.append(f"直播互动：{count_text}；最近出现：{live.get('last_seen', '未知')}")
        fact_scopes = p.get("fact_scopes", {})
        visible_facts = [
            fact for fact in p.get("facts", [])
            if str(fact_scopes.get(str(fact), "bili_comment")) in allowed_scopes
        ]
        if visible_facts:
            for f in visible_facts[-10:]:
                entries.append(f)
        tag_scopes = p.get("tag_scopes", {})
        visible_tags = [
            tag for tag in p.get("tags", [])
            if str(tag_scopes.get(str(tag), "bili_comment")) in allowed_scopes
        ]
        if visible_tags:
            entries.append("标签：" + "、".join(visible_tags[-10:]))
        impressions = p.get("impressions", {})
        visible_impressions = [
            str(text) for scope, text in impressions.items()
            if scope in allowed_scopes and str(text).strip()
        ]
        if visible_impressions:
            entries.append(f"印象：{visible_impressions[-1]}")
        return "【对该用户的了解】\n" + "\n".join(entries) if entries else ""

    # ── 写 ──
    def update(self, mid, username=None, impression=None, new_facts=None, new_tags=None, video_encounter=None, video_ref=None, live_event=None, source_scope="bili_comment"):
        """更新画像；视频与直播只保存轻量引用，不复制正文记忆。"""
        uid = str(mid)

        def change(profiles):
            profiles[uid] = normalize(profiles.get(uid))
            if username:
                profiles[uid]["username"] = username
            if impression:
                profiles[uid]["impression"] = impression
                profiles[uid].setdefault("impressions", {})[str(source_scope)] = impression
            if new_facts:
                ex = profiles[uid].get("facts", [])
                fact_scopes = profiles[uid].setdefault("fact_scopes", {})
                for f in new_facts:
                    f = f.strip()
                    if f and f not in ex:
                        ex.append(f)
                    if f:
                        fact_scopes[f] = str(source_scope)
                profiles[uid]["facts"] = ex[-20:]
                profiles[uid]["fact_scopes"] = {
                    fact: fact_scopes.get(fact, "bili_comment")
                    for fact in profiles[uid]["facts"]
                }
            if new_tags:
                et = profiles[uid].get("tags", [])
                tag_scopes = profiles[uid].setdefault("tag_scopes", {})
                for t in new_tags:
                    t = t.strip()
                    if t and t not in et:
                        et.append(t)
                    if t:
                        tag_scopes[t] = str(source_scope)
                profiles[uid]["tags"] = et[-10:]
                profiles[uid]["tag_scopes"] = {
                    tag: tag_scopes.get(tag, "bili_comment")
                    for tag in profiles[uid]["tags"]
                }
            ref_in = video_ref
            if video_encounter and video_encounter.get("bvid"):
                ref_in = {
                    "bvid": video_encounter["bvid"],
                    "title": video_encounter.get("title", ""),
                    "time": video_encounter.get("time", ""),
                    "relation": "commented_under",
                }
            if ref_in and ref_in.get("bvid"):
                refs = profiles[uid].setdefault("video_refs", [])
                ref = {
                    "bvid": str(ref_in["bvid"]),
                    "title": str(ref_in.get("title", "")),
                    "relation": str(ref_in.get("relation") or "related"),
                    "time": str(ref_in.get("time") or datetime.now().strftime("%Y-%m-%d")),
                    "scope": str(ref_in.get("scope") or source_scope),
                }
                key = (ref["bvid"], ref["relation"], ref["scope"])
                refs = [
                    item for item in refs
                    if not (
                        isinstance(item, dict)
                        and (
                            str(item.get("bvid", "")),
                            item.get("relation", ""),
                            item.get("scope", "bili_comment"),
                        ) == key
                    )
                ]
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

        self._file.update(change, {})

    def drop_tags(self, mid, tags) -> int:
        """去掉几个标签（收藏夹修正画像时用），返回去掉了几个。"""
        uid, tags, dropped = str(mid), {str(t).strip() for t in tags or []}, [0]

        def change(profiles):
            if uid not in profiles:
                return
            profile = profiles[uid] = normalize(profiles.get(uid))
            kept = [t for t in profile.get("tags", []) if t not in tags]
            dropped[0] = len(profile.get("tags", [])) - len(kept)
            profile["tags"] = kept
            profile["tag_scopes"] = {t: s for t, s in (profile.get("tag_scopes") or {}).items() if t in kept}

        if tags:
            self._file.update(change, {})
        return dropped[0]

    def link_video(self, user_id, username, bvid, title="", relation="uploaded_by"):
        """主动看片时把视频挂到 UP 主（或相关用户）的画像上。"""
        if str(user_id or "").strip() in {"", "0"} or not bvid:
            return
        self.update(
            str(user_id),
            username=username or None,
            video_ref={
                "bvid": str(bvid),
                "title": str(title or ""),
                "relation": relation,
                "time": datetime.now().strftime("%Y-%m-%d"),
            },
            source_scope="proactive",
        )

    def record_interaction(self, mid, username, score_delta, source):
        """关系计数跟着画像存，好感度本身仍只在 ``Affection``；返回更新后的计数。"""
        uid = str(mid)
        holder = {}

        def change(profiles):
            profile = normalize(profiles.get(uid))
            relation = profile.setdefault("relationship", {"positive_interactions": 0, "negative_interactions": 0, "severe_negative_streak": 0})
            relation.setdefault("positive_interactions", 0)
            relation.setdefault("negative_interactions", 0)
            relation.setdefault("severe_negative_streak", 0)
            relation["last_source"] = str(source)
            relation["last_interaction_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            if score_delta > 0:
                relation["positive_interactions"] += 1
                relation["severe_negative_streak"] = 0
            elif score_delta < 0:
                relation["negative_interactions"] += 1
                if score_delta <= -3:
                    relation["severe_negative_streak"] += 1
                else:
                    relation["severe_negative_streak"] = 0
            else:
                relation["severe_negative_streak"] = 0
            profile["username"] = username or profile.get("username", "")
            profiles[uid] = profile
            holder["relation"] = relation

        self._file.update(change, {})
        return holder["relation"]

    def forget(self, keep) -> dict:
        """``keep(uid)`` 为假的人，整条画像删掉（印象、事实、标签、互动计数、视频关系都没了）。

        返回删掉的画像（按 UID），调用方拿去写备份。好感分数不在这里，不受影响。
        """
        removed = {}

        def change(profiles):
            for uid in [uid for uid in profiles if not keep(uid)]:
                removed[uid] = profiles.pop(uid)

        if self._all():
            self._file.update(change, {})
        return removed

    def remove_memory_refs(self, removed_rpids) -> int:
        """记忆删掉后，清掉画像里指向它们的直播引用；返回清掉几条。"""
        removed = {str(item) for item in removed_rpids if item}
        if not removed:
            return 0
        with self._file.locked():
            profiles = self._file.read({})
            if not isinstance(profiles, dict):
                return 0
            changed = 0
            for profile in profiles.values():
                if not isinstance(profile, dict):
                    continue
                live = profile.get("live")
                if not isinstance(live, dict):
                    continue
                refs = live.get("memory_refs")
                if not isinstance(refs, list):
                    continue
                kept = [ref for ref in refs if str(ref) not in removed]
                changed += len(refs) - len(kept)
                if len(kept) != len(refs):
                    live["memory_refs"] = kept
            if changed:
                self._file.write(profiles)
            return changed
