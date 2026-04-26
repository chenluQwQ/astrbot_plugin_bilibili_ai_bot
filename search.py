"""记忆系统：存储、检索、压缩、上下文构建。

上下文优先级（评论回复场景）：
  第一层（主上下文）：永久记忆 + 视频/动态内容 + 本评论区所有对话(oid) + 当前评论线(thread)
  第二层（认识这人）：用户画像/印象（不拉聊天记录）
  第三层（相关调取）：全局语义搜索（高阈值，不相关不注入）
"""
import re
import json
from datetime import datetime
from astrbot.api import logger
from .config import (
    MAX_SEMANTIC_RESULTS, MEMORY_FILE, PERMANENT_MEMORY_FILE,
    QQ_MEMORY_FILE, THREAD_COMPRESS_THRESHOLD,
    OID_COMPRESS_THRESHOLD, OID_KEEP_RECENT,
    USER_MEMORY_COMPRESS_THRESHOLD, USER_MEMORY_KEEP_RECENT,
)


class MemoryMixin:
    """记忆的增删改查、语义搜索、压缩与上下文构建。"""

    # ══════════════════════════════════════
    #  归一化 & 基础
    # ══════════════════════════════════════
    def _normalize_memory_entry(self, record):
        rec = dict(record)
        if rec.get("memory_type"):
            return rec
        thread_id = str(rec.get("thread_id", ""))
        text = str(rec.get("text", ""))
        if thread_id == "dynamic" or "Bot发了一条动态" in text:
            rec["memory_type"] = "dynamic"
        elif thread_id.startswith("video:") or thread_id == "proactive_watch" or "Bot看了视频" in text or "视频分析记忆" in text:
            rec["memory_type"] = "video"
        elif text.startswith("[记忆压缩]") or text.startswith("[评论区总结]"):
            rec["memory_type"] = "user_summary"
        else:
            rec["memory_type"] = "chat"
        return rec

    def _save_memory_entry(self, record):
        self._memory.append(self._normalize_memory_entry(record))
        self._save_json(MEMORY_FILE, self._memory)

    @staticmethod
    def _memory_type_label(memory_type):
        return {"chat": "交流", "video": "视频", "dynamic": "动态", "user_summary": "用户总结"}.get(memory_type, memory_type)

    def _match_memory_type(self, memory, memory_types=None):
        if not memory_types:
            return True
        return self._normalize_memory_entry(memory).get("memory_type") in set(memory_types)

    # ══════════════════════════════════════
    #  写入记忆
    # ══════════════════════════════════════
    async def _save_memory_record(self, rpid, thread_id, user_id, username, content, reply_text, source="bilibili", oid=0):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        text = f"[{now}] 用户{user_id}({username})说：{content} | Bot回复：{reply_text}"
        emb = await self._get_embedding(text)
        rec = {
            "rpid": str(rpid), "thread_id": str(thread_id),
            "user_id": str(user_id), "username": username,
            "time": now, "text": text, "source": source,
            "memory_type": "chat",
        }
        if oid:
            rec["oid"] = str(oid)
        if emb:
            rec["embedding"] = emb
        self._save_memory_entry(rec)

    async def _save_self_memory_record(self, thread_id, text, source="bilibili", memory_type="chat", user_id="self", extra=None):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        rec = {
            "rpid": f"{thread_id}_{int(datetime.now().timestamp())}",
            "thread_id": str(thread_id),
            "user_id": str(user_id),
            "time": now, "text": text,
            "source": source, "memory_type": memory_type,
        }
        if extra:
            rec.update(extra)
        emb = await self._get_embedding(text)
        if emb:
            rec["embedding"] = emb
        self._save_memory_entry(rec)

    # ══════════════════════════════════════
    #  检索
    # ══════════════════════════════════════
    def _get_thread_memories(self, thread_id):
        """当前评论线（reply chain）的对话"""
        docs = [m for m in self._memory if m.get("thread_id") == str(thread_id) and self._match_memory_type(m, {"chat"})]
        docs.sort(key=lambda x: x.get("time", ""))
        return [m["text"] for m in docs]

    def _get_oid_memories(self, oid, exclude_thread_id=None):
        """同一评论区（oid）下的所有对话记忆，不限用户。排除当前 thread 避免重复。"""
        oid_str = str(oid)
        docs = [
            m for m in self._memory
            if m.get("oid") == oid_str
            and self._match_memory_type(m, {"chat", "user_summary"})
            and (exclude_thread_id is None or m.get("thread_id") != str(exclude_thread_id))
        ]
        docs.sort(key=lambda x: x.get("time", ""))
        return [m["text"] for m in docs]

    def _get_bvid_memories(self, bvid, exclude_oid=None):
        """按bvid调取所有与该视频相关的历史记忆（主动看视频、以前的评论区互动等）。
        排除当前oid避免和评论区记忆重复。"""
        exclude_oid_str = str(exclude_oid) if exclude_oid else ""
        docs = [
            m for m in self._memory
            if (m.get("bvid") == bvid or m.get("thread_id") == f"video:{bvid}")
            and (not exclude_oid_str or m.get("oid", "") != exclude_oid_str)
        ]
        docs.sort(key=lambda x: x.get("time", ""))
        return [self._format_memory_with_meta(m) for m in docs]

    async def _get_user_semantic_memories(self, user_id, query_text, memory_types=None):
        um = [
            m for m in self._memory
            if m.get("user_id") == str(user_id)
            and "embedding" in m
            and self._match_memory_type(m, memory_types or {"chat", "user_summary"})
        ]
        qq_mem = self._load_json(QQ_MEMORY_FILE, [])
        um += [m for m in qq_mem if m.get("user_id") == str(user_id) and "embedding" in m]
        if not um:
            return []
        qe = await self._get_embedding(query_text)
        if not qe:
            return []
        scored = [(self._cosine_similarity(qe, m["embedding"]), m["text"]) for m in um]
        scored.sort(reverse=True)
        return [t for s, t in scored[:MAX_SEMANTIC_RESULTS] if s > 0.6]

    async def _search_memories_raw(self, query_text, limit=5, source=None, memory_types=None, user_id=None, score_threshold=0.5):
        """底层语义搜索：返回 [(score, record), ...]"""
        cands = list(self._memory)
        if source != "bilibili":
            qq_mem = self._load_json(QQ_MEMORY_FILE, [])
            cands += qq_mem
        if source:
            cands = [m for m in cands if m.get("source") == source]
        if user_id is not None:
            cands = [m for m in cands if m.get("user_id") == str(user_id)]
        cands = [self._normalize_memory_entry(m) for m in cands if self._match_memory_type(m, memory_types)]
        cands = [m for m in cands if "embedding" in m]
        if not cands:
            return []
        qe = await self._get_embedding(query_text)
        if not qe:
            return []
        scored = [(self._cosine_similarity(qe, m["embedding"]), m) for m in cands]
        scored.sort(reverse=True)
        return [(s, m) for s, m in scored[:limit] if s > score_threshold]

    async def _search_memories(self, query_text, limit=5, source=None, memory_types=None, user_id=None, score_threshold=0.5):
        """语义搜索，返回格式化的文本列表"""
        raw = await self._search_memories_raw(query_text, limit=limit, source=source, memory_types=memory_types, user_id=user_id, score_threshold=score_threshold)
        results = []
        for s, m in raw:
            tag = f"[{m.get('source', '?')}]" if not source else ""
            type_tag = f"[{self._memory_type_label(m.get('memory_type', '?'))}]"
            results.append(f"{tag}{type_tag}{m['text']}")
        return results

    # ══════════════════════════════════════
    #  压缩
    # ══════════════════════════════════════
    async def _compress_oid_memory(self, oid):
        """同一评论区（oid）记忆太多时压缩旧记录"""
        oid_str = str(oid)
        oid_mems = [m for m in self._memory if m.get("oid") == oid_str and self._match_memory_type(m, {"chat"})]
        if len(oid_mems) <= OID_COMPRESS_THRESHOLD:
            return
        logger.info(f"[BiliBot] 🗜️ 评论区 {oid} 记忆达 {len(oid_mems)} 条，压缩...")
        oid_mems.sort(key=lambda x: x.get("time", ""))
        old = oid_mems[:-OID_KEEP_RECENT]
        old_texts = "\n".join([m["text"] for m in old])
        prompt = (
            f"请用150字以内总结以下同一视频评论区下的所有对话要点。\n"
            f"保留：关键话题、不同用户的观点、重要信息。\n"
            f"去掉：重复的寒暄、无意义的回复。\n\n"
            f"{old_texts[:4000]}\n\n直接输出总结，不加前缀。"
        )
        try:
            summary = await self._llm_call(prompt, max_tokens=300)
            if not summary:
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            emb = await self._get_embedding(summary)
            comp = {
                "rpid": f"oid_compressed_{int(datetime.now().timestamp())}",
                "thread_id": f"oid_summary:{oid_str}",
                "oid": oid_str,
                "user_id": "summary",
                "time": now,
                "text": f"[评论区总结] {summary}",
                "source": "bilibili",
                "memory_type": "user_summary",
            }
            # 保留视频元数据（从被压缩的记录中提取）
            for m in old:
                if m.get("bvid"):
                    comp["bvid"] = m["bvid"]
                    break
            for m in old:
                if m.get("video_title"):
                    comp["video_title"] = m["video_title"]
                    break
            if emb:
                comp["embedding"] = emb
            old_rpids = {m["rpid"] for m in old}
            self._memory = [m for m in self._memory if m.get("rpid") not in old_rpids]
            self._save_memory_entry(comp)
            logger.info(f"[BiliBot] 🗜️ 评论区 {oid} 压缩：{len(old)} 条 → 1 条总结")
        except Exception as e:
            logger.error(f"[BiliBot] 评论区压缩失败：{e}")

    async def _compress_thread_memory(self, thread_id):
        thread_mems = [m for m in self._memory if m.get("thread_id") == str(thread_id) and self._match_memory_type(m, {"chat"})]
        if len(thread_mems) <= THREAD_COMPRESS_THRESHOLD:
            return
        thread_mems.sort(key=lambda x: x.get("time", ""))
        keep_recent = 3
        old = thread_mems[:-keep_recent]
        old_texts = "\n".join([m["text"] for m in old])
        prompt = f"请用80字以内总结以下同一评论线下的对话要点，保留关键信息和话题走向：\n\n{old_texts[:3000]}\n\n直接输出总结，不加前缀。"
        try:
            summary = await self._llm_call(prompt, max_tokens=200)
            if not summary:
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            emb = await self._get_embedding(summary)
            # 保留oid字段
            old_oid = old[0].get("oid", "")
            comp = {
                "rpid": f"thread_compressed_{int(datetime.now().timestamp())}",
                "thread_id": str(thread_id),
                "user_id": old[0].get("user_id", ""),
                "time": now,
                "text": f"[评论线总结] {summary}",
                "source": "bilibili",
                "memory_type": "chat",
            }
            if old_oid:
                comp["oid"] = str(old_oid)
            # 保留视频元数据
            for m in old:
                if m.get("bvid"):
                    comp["bvid"] = m["bvid"]
                    break
            for m in old:
                if m.get("video_title"):
                    comp["video_title"] = m["video_title"]
                    break
            if emb:
                comp["embedding"] = emb
            old_rpids = {m["rpid"] for m in old}
            self._memory = [m for m in self._memory if m.get("rpid") not in old_rpids]
            self._save_memory_entry(comp)
            logger.info(f"[BiliBot] 🗜️ 评论线 {thread_id} 压缩：{len(old)} 条 → 1 条总结")
        except Exception as e:
            logger.error(f"[BiliBot] 评论线压缩失败：{e}")

    async def _compress_user_memory(self, user_id, username):
        um = [m for m in self._memory if m.get("user_id") == str(user_id) and self._match_memory_type(m, {"chat"})]
        if len(um) <= USER_MEMORY_COMPRESS_THRESHOLD:
            return
        logger.info(f"[BiliBot] 🗜️ {username} 记忆达 {len(um)} 条，压缩...")
        um.sort(key=lambda x: x.get("time", ""))
        old = um[:-USER_MEMORY_KEEP_RECENT]
        old_texts = "\n".join([m["text"] for m in old])
        prompt = (
            f'请根据以下与用户"{username}"的历史互动，完成：\n'
            f"1. 总结（100字以内）\n2. 3-5个标签\n3. 提取用户个人信息\n\n"
            f'历史：\n{old_texts[:3000]}\n\nJSON格式：{{"summary":"","tags":[],"user_facts":[]}}'
        )
        try:
            text = await self._llm_call(prompt, max_tokens=400)
            if not text:
                return
            text = self._repair_llm_json(text)
            try:
                result = json.loads(text)
            except Exception:
                result = {"summary": text[:100], "tags": [], "user_facts": []}
            self._update_user_profile(user_id, impression=result.get("summary") or None, new_facts=result.get("user_facts") or None, new_tags=result.get("tags") or None)
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            emb = await self._get_embedding(result.get("summary", ""))
            comp = {
                "rpid": f"compressed_{int(datetime.now().timestamp())}",
                "thread_id": "compressed", "user_id": str(user_id),
                "time": now, "text": f"[记忆压缩] {result.get('summary', '')}",
                "source": "bilibili", "memory_type": "user_summary",
            }
            # 保留元数据（用户可能在多个视频下互动，取最近的）
            for m in reversed(old):
                if m.get("oid"):
                    comp["oid"] = str(m["oid"])
                    break
            for m in reversed(old):
                if m.get("bvid"):
                    comp["bvid"] = m["bvid"]
                    break
            for m in reversed(old):
                if m.get("video_title"):
                    comp["video_title"] = m["video_title"]
                    break
            if emb:
                comp["embedding"] = emb
            old_rpids = {m["rpid"] for m in old}
            self._memory = [m for m in self._memory if m.get("rpid") not in old_rpids]
            self._save_memory_entry(comp)
            logger.info(f"[BiliBot] 🗜️ 压缩完成：{len(old)} 条 → 1 条")
        except Exception as e:
            logger.error(f"[BiliBot] 记忆压缩失败：{e}")

    # ══════════════════════════════════════
    #  上下文构建（分层优先级）
    # ══════════════════════════════════════
    async def _build_memory_context(self, thread_id, user_id, query_text, oid=0, comment_type=1):
        """
        三层优先级：
        第一层（主上下文）：永久记忆 + 视频/动态内容 + 评论区对话(oid) + 当前评论线
        第二层（认人）：用户画像/印象
        第三层（联想）：全局相关记忆（高阈值）
        """
        parts = []
        bot_mid = self.config.get("DEDE_USER_ID", "")

        # ── 第一层：主上下文 ──

        # 1.1 永久记忆（自我认知）
        perm = self._load_json(PERMANENT_MEMORY_FILE, [])
        if perm:
            parts.append("【Bot的自我认知】\n" + "\n".join([f"[{p.get('time', '?')}] {p['text']}" for p in perm[-20:]]))

        # 1.2 视频/动态内容
        bvid = ""
        if comment_type == 1 and oid:
            vc, cache_entry = await self._get_video_context(oid, comment_type)
            if vc:
                parts.append(vc)
            if cache_entry:
                bvid = cache_entry.get("bvid", "")
                # UP主画像（知道这个UP主是谁）
                up_mid = str(cache_entry.get("owner_mid", ""))
                if up_mid and up_mid != bot_mid:
                    up_profile = self._get_user_profile_context(up_mid)
                    if up_profile:
                        parts.append(up_profile.replace("【对该用户的了解】", "【该视频UP主的了解】"))
            # 1.2.1 调取与该视频相关的历史记忆（按bvid匹配，排除当前oid避免重复）
            if bvid:
                bvid_mems = self._get_bvid_memories(bvid, exclude_oid=oid)
                if bvid_mems:
                    parts.append("【以前关于这个视频的记忆】\n" + "\n".join(bvid_mems[-10:]))
        elif comment_type in (11, 17) and oid:
            dc = await self._get_dynamic_context(oid, comment_type=comment_type)
            if dc:
                parts.append(dc)

        # 1.3 当前评论线（最直接的对话上下文）
        td = self._get_thread_memories(thread_id)
        if td:
            parts.append("【当前评论线对话】\n" + "\n".join(td[-10:]))

        # 1.4 本评论区其他对话（同oid，排除当前thread，不限用户）
        if oid:
            oid_mems = self._get_oid_memories(oid, exclude_thread_id=thread_id)
            if oid_mems:
                # 最多取最近15条，避免太长
                recent_oid = oid_mems[-15:]
                parts.append("【本评论区其他对话】\n" + "\n".join(recent_oid))

        # ── 第二层：认人 ──

        # 2.1 当前用户画像（印象+标签+事实，不拉聊天记录）
        upc = self._get_user_profile_context(user_id)
        if upc:
            parts.append(upc)

        # ── 第三层：联想（让模型自行判断相关性） ──

        # 3.1 全局语义搜索（排除本oid的记忆，避免重复；带元数据让模型判断）
        global_mems = await self._search_global_relevant(query_text, current_oid=oid, limit=5)
        if global_mems:
            parts.append(
                "【以下是从记忆中调取的可能相关内容，每条标注了时间和来源。\n"
                "这些是次要参考，不是当前对话的一部分。\n"
                "请自行判断是否与当前话题有关，无关的忽略即可。】\n"
                + "\n".join(global_mems)
            )

        return "\n\n".join(parts) if parts else ""

    async def _search_global_relevant(self, query_text, current_oid=0, limit=5):
        """全局语义搜索，排除当前 oid，返回带元数据的格式化结果。
        不做硬阈值截断，让模型根据上下文自行判断相关性。"""
        current_oid_str = str(current_oid) if current_oid else ""
        cands = [
            m for m in self._memory
            if "embedding" in m
            and (not current_oid_str or m.get("oid", "") != current_oid_str)
        ]
        if not cands:
            return []
        qe = await self._get_embedding(query_text)
        if not qe:
            return []
        scored = [(self._cosine_similarity(qe, m["embedding"]), m) for m in cands]
        scored.sort(reverse=True)
        # 取 top N，但最低要有基本的语义相关（0.5 以下基本是噪声）
        results = []
        for s, m in scored[:limit]:
            if s < 0.5:
                break
            results.append(self._format_memory_with_meta(m))
        return results

    @staticmethod
    def _format_memory_with_meta(m):
        """给记忆条目附加元数据标签：类型、时间、来源。"""
        parts = []
        # 类型
        mtype = m.get("memory_type", "chat")
        type_labels = {"chat": "交流", "video": "视频", "dynamic": "动态", "user_summary": "总结"}
        parts.append(f"[{type_labels.get(mtype, mtype)}]")
        # 来源
        source = m.get("source", "")
        if source and source != "bilibili":
            parts.append(f"[来源:{source}]")
        # 时间
        t = m.get("time", "")
        if t:
            parts.append(f"[{t}]")
        # 视频标题（如果有）
        vt = m.get("video_title", "")
        if vt:
            parts.append(f"[视频:《{vt}》]")
        prefix = "".join(parts)
        return f"{prefix} {m.get('text', '')}"
