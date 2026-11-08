"""追番：评价失败时不编感受、不互动，这一集下次接着看。"""
import json
import types
import unittest

from bili_parts import BiliParts
from video_parts import VideoParts
from schedule_parts import ScheduleParts
from unittest.mock import AsyncMock, patch

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from core.config import BANGUMI_MEMORY_FILE, BANGUMI_WATCH_LOG_FILE
from core.domain.bangumi_memory import BangumiMemory
from core.features.bangumi import Bangumi
from core.settings import Settings
from itself import itself


class MemoryFile:
    def __init__(self, bot):
        self.bot = bot

    def read(self, default=None):
        return self.bot.files.get(BANGUMI_MEMORY_FILE, default)

    def update(self, change, default=None):
        data = self.read(default)
        change(data)
        self.bot.files[BANGUMI_MEMORY_FILE] = data


class BangumiBot(BiliParts, VideoParts):
    now_doing = ScheduleParts.now_doing
    models = itself
    bili_bangumi = itself
    memo_tool = types.SimpleNamespace(enabled=lambda: False)  # 这里不测从第一季看起（见 test_day_focus）

    def __init__(self, evaluations):
        self.settings = Settings({
            "BANGUMI_EPISODE_COUNT": 3, "BANGUMI_CONTINUE_SCORE": 6,
            "PROACTIVE_LIKE": True, "BANGUMI_AUTO_FOLLOW": True,
        })
        self.config = {}
        self.files = {}
        self.evaluations = list(evaluations)
        self.bangumi_memory = BangumiMemory(MemoryFile(self))
        self.get_bangumi_detail = AsyncMock(return_value={
            "season_id": 7, "title": "测试番",
            "episodes": [
                {"ep_id": 101, "ep_index": "1", "title": "1", "aid": 1},
                {"ep_id": 102, "ep_index": "2", "title": "2", "aid": 2},
                {"ep_id": 103, "ep_index": "3", "title": "3", "aid": 3},
            ],
        })
        self.bili_videos._oid_to_bvid = AsyncMock(return_value="BV1")
        self.video_watch._analyze_video_text = AsyncMock(return_value="这一集讲了一个足够长的故事，主角终于和朋友和好了。")
        self._get_system_prompt = AsyncMock(return_value="")
        self._generation_llm_call = AsyncMock(side_effect=lambda *_a, **_k: self.evaluations.pop(0))
        self.memory_writer._save_self_memory_record = AsyncMock()
        self._follow_bangumi = AsyncMock()
        self.bili_videos._like_video = AsyncMock()
        self.bili_auth.check_cookie = AsyncMock(return_value=(True, "ok"))
        self.watch_video = types.SimpleNamespace(_today_proactive_comment_count=lambda *_a: 0)
        self.now_doing.report = lambda *_a, **_k: None
        self.actions = []

        async def execute(key, kind, target, handler, **_kwargs):
            if kind != "bangumi_watch":
                self.actions.append(kind)
            return types.SimpleNamespace(success=True)

        self.outward_actions = types.SimpleNamespace(_execute_proactive_action=execute)

    def _load_json(self, path, default=None):
        return self.files.get(path, default)

    def _save_json(self, path, value):
        self.files[path] = value


GOOD = json.dumps({"score": 8, "comment": "", "mood": "开心", "review": "好看", "want_continue": True}, ensure_ascii=False)


class BangumiEvaluationFailureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 集与集之间本来要等 20-60 秒。
        sleeper = patch("core.features.bangumi.asyncio.sleep", AsyncMock())
        sleeper.start()
        self.addCleanup(sleeper.stop)

    async def test_failed_episode_is_not_remembered_and_stops_the_round(self):
        bot = BangumiBot([GOOD, None, GOOD])
        await Bangumi(bot)._watch_season(season_id=7)
        # 第 1 集看完；第 2 集评价失败后就停，第 3 集没看。
        self.assertEqual(bot.video_watch._analyze_video_text.await_count, 2)
        watched = {ep["ep_id"] for ep in bot.files[BANGUMI_MEMORY_FILE]["7"]["episodes"]}
        self.assertEqual(watched, {101})
        bot.memory_writer._save_self_memory_record.assert_awaited_once()
        self.assertNotIn("没什么特别的感觉", str(bot.files))
        log = bot.files[BANGUMI_WATCH_LOG_FILE]
        self.assertEqual([entry.get("evaluation_status") for entry in log], [None, "failed"])

    async def test_failed_episode_is_watched_again_next_time(self):
        bot = BangumiBot([None])
        await Bangumi(bot)._watch_season(season_id=7)
        bot.evaluations = [GOOD, GOOD, GOOD]
        bot.video_watch._analyze_video_text.reset_mock()
        await Bangumi(bot)._watch_season(season_id=7)
        first = bot.video_watch._analyze_video_text.await_args_list[0].args[0]
        self.assertIn("第1话", first["title"])

    async def test_failed_episode_saves_why(self):
        # 空回复、格式不对都要在日志里写清楚原因，不能只剩一个「未知」。
        bot = BangumiBot([None])
        await Bangumi(bot)._watch_season(season_id=7)
        entry = bot.files[BANGUMI_WATCH_LOG_FILE][-1]
        self.assertEqual(entry["fail_reason"], "模型返回空内容")
        self.assertEqual(entry["mood"], "")

        bot = BangumiBot(['{"score": 8}'])
        await Bangumi(bot)._watch_season(season_id=7)
        reason = bot.files[BANGUMI_WATCH_LOG_FILE][-1]["fail_reason"]
        self.assertIn("评价格式校验没过", reason)
        self.assertIn("bangumi_schema_mismatch", reason)

    async def test_failed_episode_has_no_interactions(self):
        bot = BangumiBot([None])
        await Bangumi(bot)._watch_season(season_id=7)
        self.assertEqual(bot.actions, [])


if __name__ == "__main__":
    unittest.main()
