"""读记忆：按评论线、评论区、视频、用户取，语义搜索，全局联想，以及拼进提示词时的格式。

隔离规则在 ``_memory_visible_to``：读的人在哪个域（``reader_scope``）就只看得见那个域能读的记忆；
全局联想不把别人的私人对话和总结套到当前用户身上。所有读法都过它。
"""
import re
from collections import OrderedDict

from ..config import MAX_SEMANTIC_RESULTS
from ..domain._memory_tags import UP, USER, VIDEO, tag
from ..infra.host_view import HostView
from ..security import readable_scopes


class MemoryRecall(HostView):
    HOST_API = frozenset({"memories", "memory_entry", "vision"})

    @staticmethod
    def _reader_scope_for_channel(channel):
        return {
            "private": "bili_dm",
            "live": "bili_live",
            "qq_private": "qq_private",
            "qq_group": "qq_group",
        }.get(str(channel or "comment").strip().lower(), "bili_comment")

    def _memory_visible_to(self, memory, reader_scope, user_id=""):
        allowed = {scope.value for scope in readable_scopes(reader_scope)}
        memory_scope = str(memory.get("scope") or self.memory_entry._prepare_memory_entry(memory).get("scope"))
        if memory_scope not in allowed:
            return False
        # 全局联想不得把另一位用户的个人对话/总结套到当前用户身上。
        if user_id and self.memory_entry._match_memory_type(memory, {"chat", "live", "user_summary"}):
            remembered_uid = str(memory.get("user_id") or "")
            if remembered_uid not in {"", "self", "summary", str(user_id)}:
                return False
        return True

    # ══════════════════════════════════════
    #  检索
    # ══════════════════════════════════════
    async def _get_thread_memories(self, thread_id, reader_scope=None):
        """当前评论线（reply chain）的对话，返回结构化记录。

        给了 ``reader_scope`` 就再按域过滤一遍：线程编号本来就按通道分开，这是第二道防线。
        """
        docs = await self.memories.find(
            thread_id=thread_id, types={"chat"},
            where=None if reader_scope is None else (lambda m: self._memory_visible_to(m, reader_scope)),
        )
        docs.sort(key=lambda x: x.get("time", ""))
        return docs

    async def _get_oid_memories(self, oid, exclude_thread_id=None, reader_scope=None):
        """同一评论区（oid）下的所有对话记忆，不限用户。排除当前 thread 避免重复。

        给了 ``reader_scope`` 就按域过滤（不按用户过滤：这里本来就是看别人的公开对话）。
        """
        docs = await self.memories.find(
            oid=oid, types={"chat", "user_summary"},
            where=lambda m: (
                (exclude_thread_id is None or m.get("thread_id") != str(exclude_thread_id))
                and (reader_scope is None or self._memory_visible_to(m, reader_scope))
            ),
        )
        docs.sort(key=lambda x: x.get("time", ""))
        return docs

    @staticmethod
    def _format_conversation_turn(m):
        """将一条记忆格式化为清晰的对话轮次，标注uid、时间、视频来源"""
        uid = m.get("user_id", "?")
        name = m.get("username", "?")
        t = m.get("time", "?")
        text = m.get("text", "")
        # 视频来源标注
        video_tag = ""
        vt = m.get("video_title", "")
        bv = m.get("bvid", "")
        if vt and bv:
            video_tag = f" [视频《{vt}》({bv})]"
        elif bv:
            video_tag = f" [{bv}]"
        # 旧格式兼容：从text中提取内容
        match = re.search(r'说：(.+?)\s*\|\s*Bot回复：(.+)$', text)
        if match:
            user_said = match.group(1).strip()
            bot_said = match.group(2).strip()
            return f"[{t}]{video_tag} {name}(uid:{uid})：{user_said}\n[{t}] 我：{bot_said}"
        return f"[{t}]{video_tag} {text}"

    @staticmethod
    def _format_oid_memories_grouped(docs):
        """将评论区记忆按用户分组格式化，防止窜台，标注视频来源"""
        groups = OrderedDict()
        # 提取这组记忆的视频来源（取第一条有信息的）
        video_label = ""
        for m in docs:
            vt = m.get("video_title", "")
            bv = m.get("bvid", "")
            if vt:
                video_label = f"《{vt}》({bv})" if bv else f"《{vt}》"
                break
            elif bv:
                video_label = bv
                break
        for m in docs:
            uid = m.get("user_id", "?")
            name = m.get("username", "?")
            key = f"{name}(uid:{uid})"
            if key not in groups:
                groups[key] = []
            t = m.get("time", "?")
            text = m.get("text", "")
            match = re.search(r'说：(.+?)\s*\|\s*Bot回复：(.+)$', text)
            if match:
                groups[key].append(f"  [{t}] {name}：{match.group(1).strip()}")
                groups[key].append(f"  [{t}] 我：{match.group(2).strip()}")
            else:
                groups[key].append(f"  [{t}] {text}")
        lines = []
        header_suffix = f" （视频：{video_label}）" if video_label else ""
        for user_key, turns in groups.items():
            lines.append(f"── 与{user_key}的对话{header_suffix} ──")
            lines.extend(turns)
        return lines

    async def _get_bvid_memories(self, bvid, exclude_oid=None, reader_scope="bili_comment", user_id=""):
        """按bvid调取所有与该视频相关的历史记忆（主动看视频、以前的评论区互动等）。
        排除当前oid避免和评论区记忆重复。"""
        exclude_oid_str = str(exclude_oid) if exclude_oid else ""
        docs = await self.memories.find(
            tag=tag(VIDEO, bvid),
            where=lambda m: (
                (not exclude_oid_str or m.get("oid", "") != exclude_oid_str)
                and self._memory_visible_to(m, reader_scope, user_id)
            ),
        )
        docs.sort(key=lambda x: x.get("time", ""))
        return [self._format_memory_with_meta(m) for m in docs]

    async def _get_user_elsewhere_memories(self, user_id, query_text, *, exclude_thread_id="", exclude_oid="",
                                           reader_scope="bili_comment", limit=3):
        """这个人在别的评论线、评论区（看得见的话还有私信）聊过的，按和这次说的话的意思排前 ``limit`` 条。

        按 ``用户:UID`` 标签找；当前评论线和评论区另有专门的块，这里排除掉。返回记录。
        """
        exclude_thread_id, exclude_oid = str(exclude_thread_id or ""), str(exclude_oid or "")
        cands = await self.memories.find(
            tag=tag(USER, user_id), has_vector=True, types={"chat", "user_summary"},
            where=lambda m: (
                (not exclude_thread_id or m.get("thread_id") != exclude_thread_id)
                and (not exclude_oid or m.get("oid") != exclude_oid)
                and self._memory_visible_to(m, reader_scope, user_id)
            ),
        )
        return await self._rank_by_meaning(query_text, cands, limit=limit, threshold=0.5)

    async def _get_up_video_memories(self, up_mid, *, exclude_bvid="", reader_scope="bili_comment", limit=3):
        """看过这位 UP 的哪些视频（按 ``UP:UID`` 标签找，最近的 ``limit`` 条，不含当前视频）。返回记录。"""
        exclude = tag(VIDEO, exclude_bvid) if exclude_bvid else ""
        return await self.memories.find(
            tag=tag(UP, up_mid), types={"video"}, newest_first=True, limit=limit,
            where=lambda m: (not exclude or exclude not in (m.get("tags") or ()))
            and self._memory_visible_to(m, reader_scope),
        )

    async def _rank_by_meaning(self, query_text, cands, *, limit, threshold):
        """按和 ``query_text`` 的意思（乘召回权重）排，返回分数过 ``threshold`` 的前 ``limit`` 条记录。"""
        cands = [m for m in cands if "embedding" in m]
        if not cands:
            return []
        qe = await self.vision._get_embedding(query_text)
        if not qe:
            return []
        scored = [
            (self.vision._cosine_similarity(qe, m["embedding"]) * self.memory_entry._memory_recall_weight(m), m)
            for m in cands
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [m for s, m in scored[:limit] if s > threshold]

    async def _get_user_semantic_memories(self, user_id, query_text, memory_types=None, reader_scope="bili_comment"):
        um = await self.memories.find(
            user_id=user_id, has_vector=True, types=memory_types or {"chat", "user_summary"},
            where=lambda m: self._memory_visible_to(m, reader_scope, user_id),
        )
        if not um:
            return []
        qe = await self.vision._get_embedding(query_text)
        if not qe:
            return []
        scored = [
            (
                self.vision._cosine_similarity(qe, m["embedding"])
                * self.memory_entry._memory_recall_weight(m),
                m["text"],
            )
            for m in um
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [t for s, t in scored[:MAX_SEMANTIC_RESULTS] if s > 0.6]

    async def _search_memories_raw(self, query_text, limit=5, source=None, memory_types=None, user_id=None, score_threshold=0.5, *, reader_scope, tag=None):
        """底层语义搜索：返回 [(score, record), ...]

        ``reader_scope`` 必须写：读的人在哪个域，就只能找到那个域看得见的记忆。
        管理员通道才写 ``"admin"``。``tag`` 先按一个标签缩小范围。
        """
        entry = self.memory_entry
        cands = await self.memories.find(
            source=source or None, user_id=user_id, types=set(memory_types) if memory_types else None, tag=tag,
            where=lambda m: self._memory_visible_to(m, reader_scope, str(user_id or "")),
        )
        cands = [entry._normalize_memory_entry(m) for m in cands]
        cands = [m for m in cands if "embedding" in m]
        if not cands:
            return []
        qe = await self.vision._get_embedding(query_text)
        if not qe:
            return []
        scored = [
            (
                self.vision._cosine_similarity(qe, m["embedding"])
                * entry._memory_recall_weight(m),
                m,
            )
            for m in cands
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [(s, m) for s, m in scored[:limit] if s > score_threshold]

    async def _search_memories(self, query_text, limit=5, source=None, memory_types=None, user_id=None, score_threshold=0.5, *, reader_scope, tag=None):
        """语义搜索，返回格式化的文本列表"""
        raw = await self._search_memories_raw(query_text, limit=limit, source=source, memory_types=memory_types, user_id=user_id, score_threshold=score_threshold, reader_scope=reader_scope, tag=tag)
        return [self._format_search_hit(m, show_source=not source) for _s, m in raw]

    async def _recent_memories(self, *, tag, memory_types=None, limit=5, reader_scope):
        """挂着 ``tag`` 的最近几条（不按意思排），格式和 ``_search_memories`` 一样。"""
        docs = await self.memories.find(
            tag=tag, types=set(memory_types) if memory_types else None, newest_first=True, limit=limit,
            where=lambda m: self._memory_visible_to(m, reader_scope),
        )
        return [self._format_search_hit(m) for m in docs]

    def _format_search_hit(self, m, show_source=True):
        source_tag = f"[{m.get('source', '?')}]" if show_source else ""
        type_tag = f"[{self.memory_entry._memory_type_label(m.get('memory_type', '?'))}]"
        return f"{source_tag}{type_tag}{m['text']}"

    async def _search_global_relevant(self, query_text, current_oid=0, limit=5, reader_scope="bili_comment", user_id="",
                                      exclude_rpids=()):
        """全局语义搜索，排除当前 oid 和 ``exclude_rpids``（别的块已经放了的），返回带元数据的格式化结果。
        不做硬阈值截断，让模型根据上下文自行判断相关性。"""
        current_oid_str = str(current_oid) if current_oid else ""
        exclude_rpids = {str(r) for r in exclude_rpids}
        cands = await self.memories.find(
            has_vector=True,
            where=lambda m: (
                (not current_oid_str or m.get("oid", "") != current_oid_str)
                and str(m.get("rpid") or "") not in exclude_rpids
                and self._memory_visible_to(m, reader_scope, user_id)
            ),
        )
        if not cands:
            return []
        qe = await self.vision._get_embedding(query_text)
        if not qe:
            return []
        scored = [
            (
                self.vision._cosine_similarity(qe, m["embedding"])
                * self.memory_entry._memory_recall_weight(m),
                m,
            )
            for m in cands
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        # 取 top N，但最低要有基本的语义相关（0.5 以下基本是噪声）
        results = []
        for s, m in scored[:limit]:
            if s < 0.5:
                break
            results.append(self._format_memory_with_meta(m))
        return results

    @staticmethod
    def _format_memory_with_meta(m):
        """给记忆条目附加元数据标签：类型、级别、时间、来源。"""
        parts = []
        # 类型
        mtype = m.get("memory_type", "chat")
        type_labels = {"chat": "交流", "video": "视频", "dynamic": "动态", "live": "直播", "user_summary": "总结"}
        parts.append(f"[{type_labels.get(mtype, mtype)}]")
        # 级别
        level = m.get("level")
        if level:
            level_labels = {"today": "今日", "recent": "近期", "long_term": "长期"}
            parts.append(f"[{level_labels.get(level, level)}]")
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
        # 分区（如果有）
        tn = m.get("tname", "")
        if tn:
            parts.append(f"[分区:{tn}]")
        # 链接（番剧用 ep 链接，普通视频用 bvid 链接）
        ep_id = m.get("ep_id", "")
        bvid = m.get("bvid", "")
        if ep_id:
            parts.append(f"[链接:https://www.bilibili.com/bangumi/play/ep{ep_id}]")
        elif bvid:
            parts.append(f"[链接:https://www.bilibili.com/video/{bvid}]")
        prefix = "".join(parts)
        return f"{prefix} {m.get('text', '')}"
