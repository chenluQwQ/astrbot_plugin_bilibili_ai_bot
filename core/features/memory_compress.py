"""记忆压缩：同一条评论线、同一个评论区、同一个人（按域）的聊天攒多了，
问模型写一条总结换掉旧的几条。每次回复完调一次。

数够不够 → 冷却中吗 → 问模型 → 在记忆写锁里把旧的换成总结（``memories.editing()``）。
失败一小时内不重试。总结记着从哪几条来的（``derived_from_rpids``），删原文时连带作废。
"""
from __future__ import annotations

import json
from datetime import datetime

from astrbot.api import logger

from ..config import (
    OID_COMPRESS_THRESHOLD, OID_KEEP_RECENT, THREAD_COMPRESS_THRESHOLD,
    USER_MEMORY_COMPRESS_THRESHOLD, USER_MEMORY_KEEP_RECENT,
)
from ..infra.host_view import HostView
from ..infra.text import repair_llm_json


class MemoryCompress(HostView):
    HOST_API = frozenset({
        "memories", "memory_entry", "models", "vision",
    })

    def __init__(self, host):
        super().__init__(host)
        # 压缩失败的冷却时间，键是 oid_/thread_/user_ 开头的
        self._compress_cooldowns = {}

    async def compress_oid(self, oid):
        """同一评论区（oid）记忆太多时压缩旧记录"""
        oid_str = str(oid)
        oid_mems = await self.memories.find(oid=oid_str, types={"chat"})
        if len(oid_mems) <= OID_COMPRESS_THRESHOLD:
            return
        cooldowns = getattr(self, "_compress_cooldowns", {})
        import time as _time
        cooldown_key = f"oid_{oid_str}"
        if cooldown_key in cooldowns and _time.time() - cooldowns[cooldown_key] < 3600:
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
            summary = await getattr(self.models, "_summary_llm_call", self.models._llm_call)(prompt, max_tokens=300)
            if not summary:
                cooldowns[cooldown_key] = _time.time()
                self._compress_cooldowns = cooldowns
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            emb = await self.vision._get_embedding(summary)
            comp = {
                "rpid": self.memory_entry._derived_memory_id("oid_compressed", old),
                "thread_id": f"oid_summary:{oid_str}",
                "oid": oid_str,
                "user_id": "summary",
                "time": now,
                "text": f"[评论区总结] {summary}",
                "source": "bilibili",
                "memory_type": "user_summary",
                "summary_kind": "oid",
                "derived_from_rpids": sorted({str(m["rpid"]) for m in old}),
                "level": "long_term", "evaluation_pending": True, "promoted_at": now,
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
            await self.memories.replace({m["rpid"] for m in old}, comp)
            logger.info(f"[BiliBot] 🗜️ 评论区 {oid} 压缩：{len(old)} 条 → 1 条总结")
        except Exception as e:
            cooldowns[cooldown_key] = _time.time()
            self._compress_cooldowns = cooldowns
            logger.error(f"[BiliBot] 评论区压缩失败（1小时后重试）：{e}")

    async def compress_thread(self, thread_id):
        thread_mems = await self.memories.find(thread_id=thread_id, types={"chat"})
        if len(thread_mems) <= THREAD_COMPRESS_THRESHOLD:
            return
        cooldowns = getattr(self, "_compress_cooldowns", {})
        import time as _time
        cooldown_key = f"thread_{thread_id}"
        if cooldown_key in cooldowns and _time.time() - cooldowns[cooldown_key] < 3600:
            return
        thread_mems.sort(key=lambda x: x.get("time", ""))
        keep_recent = 3
        old = thread_mems[:-keep_recent]
        old_texts = "\n".join([m["text"] for m in old])
        prompt = f"请用80字以内总结以下同一评论线下的对话要点，保留关键信息和话题走向：\n\n{old_texts[:3000]}\n\n直接输出总结，不加前缀。"
        try:
            summary = await getattr(self.models, "_summary_llm_call", self.models._llm_call)(prompt, max_tokens=200)
            if not summary:
                cooldowns[cooldown_key] = _time.time()
                self._compress_cooldowns = cooldowns
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            emb = await self.vision._get_embedding(summary)
            # 保留oid字段
            old_oid = old[0].get("oid", "")
            comp = {
                "rpid": self.memory_entry._derived_memory_id("thread_compressed", old),
                "thread_id": str(thread_id),
                "user_id": old[0].get("user_id", ""),
                "time": now,
                "text": f"[评论线总结] {summary}",
                "source": "bilibili",
                "memory_type": "chat",
                "summary_kind": "thread",
                "derived_from_rpids": sorted({str(m["rpid"]) for m in old}),
                "level": "long_term", "evaluation_pending": True, "promoted_at": now,
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
            await self.memories.replace({m["rpid"] for m in old}, comp)
            logger.info(f"[BiliBot] 🗜️ 评论线 {thread_id} 压缩：{len(old)} 条 → 1 条总结")
        except Exception as e:
            cooldowns[cooldown_key] = _time.time()
            self._compress_cooldowns = cooldowns
            logger.error(f"[BiliBot] 评论线压缩失败（1小时后重试）：{e}")

    async def compress_user(self, user_id, username, memory_scope="bili_comment"):
        """按用户且按记忆域压缩，禁止把评论、私信和直播揉进同一份总结。"""
        um = await self.memories.find(
            user_id=user_id, types={"chat"},
            where=lambda m: str(m.get("scope") or self.memory_entry._prepare_memory_entry(m).get("scope")) == str(memory_scope),
        )
        if len(um) <= USER_MEMORY_COMPRESS_THRESHOLD:
            return
        # 冷却机制：压缩失败后1小时内不重试，避免反复浪费 Token
        cooldowns = getattr(self, "_compress_cooldowns", {})
        import time as _time
        cooldown_key = f"user_{user_id}"
        if cooldown_key in cooldowns and _time.time() - cooldowns[cooldown_key] < 3600:
            return
        logger.info(f"[BiliBot] 🗜️ {username} 记忆达 {len(um)} 条，压缩...")
        um.sort(key=lambda x: x.get("time", ""))
        old = um[:-USER_MEMORY_KEEP_RECENT]
        old_texts = "\n".join([m["text"] for m in old])
        prompt = (
            f'请把以下与用户"{username}"的历史互动总结成一段（100字以内）。\n\n'
            f'历史：\n{old_texts[:3000]}\n\nJSON格式：{{"summary":""}}'
        )
        try:
            text = await getattr(self.models, "_summary_llm_call", self.models._llm_call)(prompt, max_tokens=400)
            if not text:
                cooldowns[cooldown_key] = _time.time()
                self._compress_cooldowns = cooldowns
                return
            text = repair_llm_json(text)
            try:
                result = json.loads(text)
            except Exception:
                result = {"summary": text[:100]}
            # 画像由每晚清算整理（core/features/profile_digest.py），压缩只换记忆，不碰画像
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            emb = await self.vision._get_embedding(result.get("summary", ""))
            comp = {
                "rpid": self.memory_entry._derived_memory_id("compressed", old),
                "thread_id": f"compressed:{memory_scope}", "user_id": str(user_id),
                "time": now, "text": f"[记忆压缩] {result.get('summary', '')}",
                "source": "bilibili", "memory_type": "user_summary",
                "scope": str(memory_scope),
                "summary_kind": "user",
                "derived_from_rpids": sorted({str(m["rpid"]) for m in old}),
                "level": "long_term", "evaluation_pending": True, "promoted_at": now,
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
            await self.memories.replace({m["rpid"] for m in old}, comp)
            logger.info(f"[BiliBot] 🗜️ 压缩完成：{len(old)} 条 → 1 条")
        except Exception as e:
            cooldowns[cooldown_key] = _time.time()
            self._compress_cooldowns = cooldowns
            logger.error(f"[BiliBot] 记忆压缩失败（{username}，1小时后重试）：{e}")
