"""站内写操作：评论、点赞、投币、收藏、关注，以及单独的拉黑。都经事件队列 ``execute_write_action`` 排队，
结果不确定时不让模型重试。"""
import asyncio
import random
import re

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass


# 发评论没成功时告诉模型的话（issue #62：别只说「发送失败」）
COMMENT_FAILURES = {
    "risk": "这次被B站拦下了（访问被限制，可能是风控），评论没发出去。不要重试这条。",
    "cookie": "B站登录失效了，评论没发出去，要等主人更新 Cookie。",
    "closed": "这个视频的评论区关了，发不了评论。",
    "sensitive": "评论里有B站不让发的词，没发出去；换个说法可以再发一次。",
    "error": "发评论时出错了，没确认发出去；先别重试，让主人看看日志。",
}


def comment_failure_text(reason):
    if reason in COMMENT_FAILURES:
        return COMMENT_FAILURES[reason]
    detail = f"（B站返回 {reason[5:]}）" if reason.startswith("code:") else ""
    return f"评论没发出去{detail}，先别重试，让主人看看日志。"


def build(kit):
    app = kit.app
    execute_write_action = kit.execute_write_action
    plugin = kit.plugin
    short_digest = kit.short_digest
    write_outcome_text = kit.write_outcome_text

    @dataclass
    class PostCommentTool(FunctionTool[AstrAgentContext]):
        name: str = "post_comment"
        description: str = "Post a comment on a Bilibili video. Only when user agrees or asks."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "video oid from watch_video, or a full BV id"},
                "comment_text": {"type": "string", "description": "comment content"},
            },
            "required": ["oid", "comment_text"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            try:
                oid = str(kwargs.get("oid", "") or "").strip()
                comment_text = str(kwargs.get("comment_text", "") or "").strip()
                if not comment_text:
                    return "评论内容不能为空。"
                # 模型常把 BV 号当目标传进来：先换成数字 oid，同一个视频的去重键才一致
                bvid = oid if re.fullmatch(r"BV[0-9A-Za-z]{10}", oid) else ""
                if bvid:
                    aid = await app.video_aid(oid)
                    if not aid:
                        return f"没查到视频 {oid}，评论没有发送。"
                    oid = str(aid)
                if not oid.isdigit():
                    return "oid 无效：需要 watch_video 返回的数字 oid 或完整 BV 号。"
                failure = {"reason": ""}

                sync = app.comment_syncs()

                async def send():
                    ok, failure["reason"] = await app.comment_detailed(int(oid), comment_text, sync_to_dynamic=sync)
                    return ok

                outcome = await execute_write_action(
                    f"tool_comment:{oid}:{short_digest(comment_text)}",
                    "tool_comment",
                    oid,
                    send,
                    metadata={"text": comment_text[:200]},
                )
                if sync and outcome.success and not outcome.duplicate:
                    app.comment_synced(bvid or oid, comment_text)
                return write_outcome_text(
                    outcome, f"评论成功：{comment_text}", comment_failure_text(failure["reason"]), "评论发送"
                )
            except Exception as e:
                return f"评论出错：{e}"
    @dataclass
    class LikeVideoTool(FunctionTool[AstrAgentContext]):
        name: str = "like_video"
        description: str = "Like a Bilibili video. Only when user agrees or asks."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "video oid from watch_video"},
            },
            "required": ["oid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            await asyncio.sleep(random.uniform(2, 5))
            oid = str(kwargs.get("oid", "")).strip()
            if not oid.isdigit():
                return "oid 无效：需要 watch_video 返回的数字 oid，不是 BV 号。"
            outcome = await execute_write_action(
                f"tool_like:{oid}",
                "like",
                oid,
                lambda: app.like(int(oid)),
            )
            return write_outcome_text(outcome, "点赞成功。", "点赞失败。", "点赞")
    @dataclass
    class CoinVideoTool(FunctionTool[AstrAgentContext]):
        name: str = "coin_video"
        description: str = "Give coins to a Bilibili video. Only when user agrees or asks."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "video oid from watch_video"},
                "num": {"type": "string", "description": "1 or 2", "default": "1"},
            },
            "required": ["oid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            await asyncio.sleep(random.uniform(2, 5))
            oid = str(kwargs.get("oid", "")).strip()
            if not oid.isdigit():
                return "oid 无效：需要 watch_video 返回的数字 oid，不是 BV 号。"
            try:
                num = int(str(kwargs.get("num", "1")).strip())
            except ValueError:
                num = 1
            num = 1 if num < 2 else 2
            outcome = await execute_write_action(
                f"tool_coin:{oid}:{num}",
                "coin",
                oid,
                lambda: app.coin(int(oid), num=num),
            )
            return write_outcome_text(
                outcome, f"投了{num}个币。", "投币失败。", "投币"
            )
    @dataclass
    class FavVideoTool(FunctionTool[AstrAgentContext]):
        name: str = "fav_video"
        description: str = "Favorite a Bilibili video. Only when user agrees or asks."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "oid": {"type": "string", "description": "video oid from watch_video"},
            },
            "required": ["oid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            await asyncio.sleep(random.uniform(2, 5))
            oid = str(kwargs.get("oid", "")).strip()
            if not oid.isdigit():
                return "oid 无效：需要 watch_video 返回的数字 oid，不是 BV 号。"
            outcome = await execute_write_action(
                f"tool_favorite:{oid}",
                "favorite",
                oid,
                lambda: app.favorite(int(oid)),
            )
            return write_outcome_text(outcome, "收藏成功。", "收藏失败。", "收藏")
    @dataclass
    class FollowUpTool(FunctionTool[AstrAgentContext]):
        name: str = "follow_up"
        description: str = "Follow a Bilibili UP. Only when user agrees or asks."
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "UID or name"},
            },
            "required": ["query"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            query = kwargs.get("query", "0")
            mid = query
            if not query.isdigit():
                users = await app.search_users(query, ps=1)
                if not users:
                    return f"没有找到名为「{query}」的UP主，无法关注。"
                mid = str(users[0]["mid"])
            outcome = await execute_write_action(
                f"tool_follow:{mid}",
                "follow",
                mid,
                lambda: app.follow(int(mid)),
            )
            return write_outcome_text(
                outcome, f"已关注UP主(UID:{mid})。", "关注失败。", "关注"
            )
    @dataclass
    class BlockBiliUserTool(FunctionTool[AstrAgentContext]):
        name: str = "bili_block_user"
        description: str = (
            "拉黑一个B站用户。当主人同意/要求拉黑某个B站用户时调用。"
            "Block a Bilibili user by UID. Use when the owner confirms blocking someone."
        )
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "uid": {"type": "string", "description": "要拉黑的B站用户 UID"},
                "reason": {"type": "string", "description": "拉黑原因（简短描述）", "default": "恶意评论"},
            },
            "required": ["uid"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            uid = str(kwargs.get("uid", "")).strip()
            reason = kwargs.get("reason", "恶意评论") or "恶意评论"
            if not uid or not uid.isdigit():
                return "UID 无效，请提供纯数字的B站UID。"

            # 检查是否是主人
            if app.is_owner(uid):
                return "不能拉黑主人！"

            import os, json
            from datetime import datetime as dt

            # 调用B站API拉黑
            block_outcome = await execute_write_action(
                f"tool_block:{uid}",
                "block_user",
                uid,
                lambda: app.block(int(uid)),
                metadata={"budget_exempt": True, "safety_action": True},
            )
            api_ok = block_outcome.success

            # 写入本地黑名单
            app.blocklist.add(uid, {
                "username": app.user_profiles.raw(uid).get("username", ""),
                "reason": reason,
                "time": dt.now().strftime("%Y-%m-%d %H:%M"),
            })

            if api_ok:
                logger.info(f"[BiliBot] 🚫 工具拉黑 UID:{uid}（{reason}）")
                return f"已拉黑 UID:{uid}，B站API调用成功。"
            if block_outcome.state == "unknown":
                logger.warning(f"[BiliBot] ⚠️ 工具拉黑 UID:{uid} B站API结果未知，已加本地黑名单")
                return (
                    f"⚠️ UID:{uid} 已加入本地黑名单，但B站API拉黑结果未知；"
                    "请求可能已经生效，请勿自动重试，先人工核查远端状态。"
                )
            logger.warning(f"[BiliBot] ⚠️ 工具拉黑 UID:{uid} B站API失败，已加本地黑名单")
            return f"UID:{uid} 已加入本地黑名单，但B站API调用失败（可能需要重新登录）。"
    @dataclass
    class BiliActionTool(FunctionTool[AstrAgentContext]):
        name: str = "bili_action"
        description: str = (
            "Perform one explicit Bilibili interaction: comment, like, coin, favorite, or follow. "
            "Only call when the user has agreed or directly requested the action."
        )
        parameters: dict = Field(default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["comment", "like", "coin", "favorite", "follow"],
                },
                "target": {
                    "type": "string",
                    "description": "video oid for comment/like/coin/favorite (comment also accepts a full BV id); UP UID or name for follow",
                },
                "text": {
                    "type": "string",
                    "description": "comment text; only used by comment",
                    "default": "",
                },
                "num": {
                    "type": "integer",
                    "description": "coin count, 1 or 2",
                    "default": 1,
                },
            },
            "required": ["action", "target"],
        })

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            action = str(kwargs.get("action", "") or "").strip().lower()
            target = str(kwargs.get("target", "") or "").strip()
            if not target:
                return "请提供操作目标。"
            if action == "comment":
                text = str(kwargs.get("text", "") or "").strip()
                if not text:
                    return "评论内容不能为空。"
                return await PostCommentTool().call(context, oid=target, comment_text=text)
            if action == "like":
                return await LikeVideoTool().call(context, oid=target)
            if action == "coin":
                return await CoinVideoTool().call(context, oid=target, num=str(kwargs.get("num", 1)))
            if action == "favorite":
                return await FavVideoTool().call(context, oid=target)
            if action == "follow":
                return await FollowUpTool().call(context, query=target)
            return "不支持的互动操作。"

    return BiliActionTool, BlockBiliUserTool
