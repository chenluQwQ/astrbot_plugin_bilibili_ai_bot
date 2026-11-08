"""一轮看视频的规则和状态，主动看片和特别关注共用。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WatchPolicy:
    """一轮看视频的规则。

    主动看片和特别关注共用同一个「看一个视频」流程（``_watch_one_video``），
    两者的全部差别都写在这里，流程里只按这些有名字的规则判断，不再到处 ``if 特别关注``。
    """

    name: str                        # 动作 key 前缀
    follows_up: bool                 # 分数够高时关注 UP
    recommends_to_owner: bool        # 可能推荐给主人
    records_discovery: bool          # 观看日志里记来源细节、是否手动、探索/兴趣、想不想发动态
    collects_dynamic_ideas: bool     # 看完想发动态的放进动态池子（``domain/dynamic_ideas.py``）
    writes_shared_video_cache: bool  # 写评论回复共用的视频缓存，之后有人在这个视频下评论时不用重看
    tags_comment_log: bool           # 评论日志里标上 type
    fixed_source: str                # 日志和「看过」记录里的来源；空表示用视频自己的来源
    default_seen_source: str         # 视频没有来源时「看过」记录用的来源
    memory_subject: str              # 写进记忆时的主语
    icon: str
    summary_icon: str
    warn_prefix: str


PROACTIVE_WATCH = WatchPolicy(
    name="proactive", follows_up=True, recommends_to_owner=True, records_discovery=True,
    collects_dynamic_ideas=True, writes_shared_video_cache=True, tags_comment_log=False,
    fixed_source="", default_seen_source="proactive", memory_subject="Bot",
    icon="📝", summary_icon="📊", warn_prefix="",
)

# 特别关注的 UP 本来就关注了，不再关注；推荐给主人由 Bot 自己决定（和主动看片同一套判断、
# 分数线、每日上限和发送方式）。
SPECIAL_FOLLOW_WATCH = WatchPolicy(
    name="special_follow", follows_up=False, recommends_to_owner=True, records_discovery=False,
    collects_dynamic_ideas=True, writes_shared_video_cache=True, tags_comment_log=True,
    fixed_source="special_follow", default_seen_source="special_follow", memory_subject="特别关注",
    icon="⭐", summary_icon="⭐", warn_prefix="⭐ ",
)


@dataclass
class _WatchRound:
    """一轮看视频中跨视频共享的状态；这一轮按哪套规则看，见 ``policy``。"""

    policy: WatchPolicy
    watch_log: list
    watched_bvids: set
    commented_videos: set
    external_memory: dict
    daily_comment_limit: int
    per_video_comment: int
    is_manual: bool = False
    recommend_ledger: dict = field(default_factory=dict)
    owner_recommend_count: int = 0
    comment_count: int = 0
    watch_count: int = 0
    dynamic_candidates: list = field(default_factory=list)  # 这一轮放进动态池子的编号

    @property
    def action_key_prefix(self):
        return self.policy.name

    @property
    def icon(self):
        return self.policy.icon

    @property
    def summary_icon(self):
        return self.policy.summary_icon

    @property
    def warn_prefix(self):
        return self.policy.warn_prefix

    @property
    def memory_subject(self):
        return self.policy.memory_subject

    def seen_source(self, video):
        return self.policy.fixed_source or video.get("_source", self.policy.default_seen_source)

    def log_source(self, video):
        return self.policy.fixed_source or video.get("_source", "")
