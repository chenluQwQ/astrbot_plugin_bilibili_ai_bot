"""特别关注巡视：挨个看特别关注的 UP 有没有新视频，有就看。

看一个视频的流程和主动看片共用（``_watch_one_video``），差别写在 ``SPECIAL_FOLLOW_WATCH``。
"""
from __future__ import annotations

import asyncio
import random
import traceback

from astrbot.api import logger

from ..infra.host_view import HostView
from ._watch_round import SPECIAL_FOLLOW_WATCH, _WatchRound


class SpecialFollow(HostView):
    HOST_API = frozenset({"memory_docs", 
        "activity_logs", "bili_users", "json_store", "now_doing", "owner_recommend", "processed_keys", "seen_videos", "settings",
        "watch_video",
    })

    async def run(self):
        self.now_doing.report("special_follow", "正在看有没有新视频")
        try:
            await self._patrol()
        except asyncio.CancelledError:
            logger.info("[BiliBot] 特别关注任务被取消")
            raise
        except Exception as e:
            logger.error(f"[BiliBot] 特别关注任务异常: {e}\n{traceback.format_exc()}")
        finally:
            self.now_doing.finish("special_follow")

    async def _patrol(self):
        special_mids = self.settings.get("PROACTIVE_FOLLOW_UIDS")
        if not special_mids:
            logger.info("[BiliBot] 特别关注列表为空，跳过")
            return

        watch_log = self.activity_logs.watch.recent()
        # 推荐给主人和主动看片共用今天的推荐账本和上限
        recommend_ledger, owner_recommend_count = self.owner_recommend._load_owner_recommendation_state(watch_log)
        watch_round = _WatchRound(
            policy=SPECIAL_FOLLOW_WATCH,
            watch_log=watch_log,
            watched_bvids=await self.seen_videos.bvids(),
            commented_videos=self.processed_keys.commented_video.all(),
            external_memory=self.memory_docs.external_memory.read({}),
            daily_comment_limit=max(0, int(self.settings.get("PROACTIVE_COMMENT_DAILY_LIMIT") or 0)),
            per_video_comment=self.watch_video._proactive_per_video_comment(),
            recommend_ledger=recommend_ledger,
            owner_recommend_count=owner_recommend_count,
        )

        logger.info(f"[BiliBot] ⭐ 特别关注巡视开始，共 {len(special_mids)} 个UP主")

        watch_round.comment_count = self.watch_video._today_proactive_comment_count(
            watch_log, self.activity_logs.proactive.recent()
        )

        for mid in special_mids:
            video = await self.bili_users._get_up_latest_video(mid)
            if not video:
                logger.info(f"[BiliBot] ⭐ UP主 {mid} 无最新视频，跳过")
                continue
            bvid = video["bvid"]
            if bvid in watch_round.watched_bvids:
                logger.info(f"[BiliBot] ⭐ 已看过 {video.get('up_name', '')} 的《{video['title']}》，跳过")
                continue
            if str(video.get("up_mid", "")) == self.settings.get("DEDE_USER_ID"):
                continue

            watch_reservation = await self.watch_video._reserve_proactive_watch(
                bvid, "special_follow"
            )
            if not watch_reservation.success:
                continue
            self.now_doing.report("special_follow", "正在分析视频", title=video.get("title", ""), up_name=video.get("up_name", ""))
            logger.info(f"[BiliBot] ⭐ 特关看视频：{video.get('up_name', '')} - {video['title']}")
            if not await self.watch_video._watch_one_video(watch_round, video):
                continue

            if watch_round.watch_count < len(special_mids):
                wait = random.randint(30, 90)
                logger.info(f"[BiliBot] ⭐ 等待 {wait} 秒...")
                await asyncio.sleep(wait)

        logger.info(f"[BiliBot] ⭐ 特别关注巡视完成！看了 {watch_round.watch_count} 个视频")
