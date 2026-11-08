"""回复前拼记忆上下文，三层优先级（评论回复场景）：

  第一层（主上下文）：永久记忆 + 视频/动态内容 + 当前评论线(thread) + 本评论区其他对话(oid)
  第二层（认识这人）：用户画像/印象 + 这人在别处聊过的（按 ``用户:UID`` 标签，最多 3 条）
  第三层（相关调取）：全局语义搜索（高阈值，不相关不注入）

视频评论区另有「看过这位 UP 的其他视频」（按 ``UP:UID`` 标签，最近 3 条）。

读记忆都经 ``memory_recall``，按通道定的 ``reader_scope`` 过滤。
"""
from ..domain.self_memory import format_numbered
from ..infra.host_view import HostView


class ReplyContext(HostView):
    HOST_API = frozenset({"states", 
        "dynamic_context", "json_store", "memory_recall", "settings", "user_profiles",
        "video_context", "video_watch",
    })

    async def _build_memory_context(self, thread_id, user_id, query_text, oid=0, comment_type=1, channel="comment"):
        """
        三层优先级：
        第一层（主上下文）：永久记忆 + 视频/动态内容 + 评论区对话(oid) + 当前评论线
        第二层（认人）：用户画像/印象 + 这人在别处聊过的
        第三层（联想）：全局相关记忆（高阈值）
        """
        parts = []
        shown_rpids = set()  # 前面块里放过的，全局联想不再重复
        bot_mid = self.settings.get("DEDE_USER_ID")
        recall = self.memory_recall
        reader_scope = recall._reader_scope_for_channel(channel)

        # ── 第一层：主上下文 ──

        # 1.1 永久记忆（自我认知）
        perm = format_numbered(self.states.permanent_memory.read([]), reader_scope)
        if perm:
            parts.append("【Bot的自我认知】\n" + perm)

        # 1.2 视频/动态内容
        bvid = ""
        if comment_type == 1 and oid:
            if channel == "comment":
                vc, cache_entry = await self.video_watch._get_comment_video_memory_context(oid)
            else:
                vc, cache_entry = await self.video_context._get_video_context(oid, comment_type)
            if vc:
                parts.append(vc)
            if cache_entry:
                bvid = cache_entry.get("bvid", "")
                # UP主画像（知道这个UP主是谁）
                up_mid = str(cache_entry.get("owner_mid", ""))
                if up_mid and up_mid != bot_mid:
                    up_profile = self.user_profiles.context(up_mid, reader_scope)
                    if up_profile:
                        parts.append(up_profile.replace("【对该用户的了解】", "【该视频UP主的了解】"))
                    up_videos = await recall._get_up_video_memories(
                        up_mid, exclude_bvid=bvid, reader_scope=reader_scope)
                    if up_videos:
                        shown_rpids.update(str(m.get("rpid") or "") for m in up_videos)
                        parts.append("【看过这位UP主的其他视频】\n" + "\n".join(
                            recall._format_memory_with_meta(m) for m in up_videos))
            # 1.2.1 调取与该视频相关的历史记忆（按bvid匹配，排除当前oid避免重复）
            if bvid:
                bvid_mems = await recall._get_bvid_memories(
                    bvid,
                    exclude_oid=oid,
                    reader_scope=reader_scope,
                    user_id=user_id,
                )
                if bvid_mems:
                    parts.append("【以前关于这个视频的记忆】\n" + "\n".join(bvid_mems[-10:]))
        elif comment_type in (11, 17) and oid:
            dc = await self.dynamic_context._get_dynamic_context(oid, comment_type=comment_type)
            if dc:
                parts.append(dc)

        # 1.3 当前评论线（最直接的对话上下文）
        td = await recall._get_thread_memories(thread_id, reader_scope)
        if td:
            formatted = [recall._format_conversation_turn(m) for m in td[-10:]]
            if str(thread_id).startswith("private:"):
                parts.append(
                    "【当前私信对话上下文】以下是你和这位用户此前的一对一私信，"
                    "按时间顺序排列：\n" + "\n".join(formatted)
                )
            else:
                parts.append(
                    "【当前评论线上下文】以下是你和这位用户在同一评论线里的历史对话，"
                    "按时间顺序排列：\n" + "\n".join(formatted)
                )

        # 1.4 本评论区其他对话（同oid，排除当前thread，不限用户）
        if oid:
            oid_mems = await recall._get_oid_memories(oid, exclude_thread_id=thread_id, reader_scope=reader_scope)
            if oid_mems:
                # 最多取最近15条，按用户分组避免窜台
                recent_oid = oid_mems[-15:]
                grouped = recall._format_oid_memories_grouped(recent_oid)
                parts.append(
                    "【本评论区其他用户的对话】以下是同一评论区里你和其他用户的交流记录，"
                    "注意区分不同用户（各自标注了uid），不要把不同人的对话混淆：\n"
                    + "\n".join(grouped)
                )

        # ── 第二层：认人 ──

        # 2.1 当前用户画像（印象+标签+事实，不拉聊天记录）
        upc = self.user_profiles.context(user_id, reader_scope)
        if upc:
            parts.append(upc)

        # 2.2 这人在别的地方聊过的（当前评论线、评论区上面已经有了）
        elsewhere = await recall._get_user_elsewhere_memories(
            user_id, query_text, exclude_thread_id=thread_id, exclude_oid=oid, reader_scope=reader_scope)
        if elsewhere:
            shown_rpids.update(str(m.get("rpid") or "") for m in elsewhere)
            parts.append(
                "【这位用户以前在别处和你聊过的】按和这次说的话的相关程度排，不是当前对话：\n"
                + "\n".join(recall._format_memory_with_meta(m) for m in elsewhere)
            )

        # ── 第三层：联想（让模型自行判断相关性） ──

        # 3.1 全局语义搜索（排除本oid的记忆，避免重复；带元数据让模型判断）
        global_mems = await recall._search_global_relevant(
            query_text,
            current_oid=oid,
            limit=5,
            reader_scope=reader_scope,
            user_id=user_id,
            exclude_rpids=shown_rpids,
        )
        if global_mems:
            parts.append(
                "【以下是从记忆中调取的可能相关内容，每条标注了时间和来源。\n"
                "这些是次要参考，不是当前对话的一部分。\n"
                "请自行判断是否与当前话题有关，无关的忽略即可。】\n"
                + "\n".join(global_mems)
            )

        return "\n\n".join(parts) if parts else ""
