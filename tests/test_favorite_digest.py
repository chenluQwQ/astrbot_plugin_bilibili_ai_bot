"""issue #51 每周用收藏夹修正画像：选人和轮流游标、只看新收藏、成功才记看到哪、一周一次、格式不对整批不写。"""
import asyncio
import json
import types
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from core.domain.affection import Affection
from core.features import favorite_digest
from core.features.favorite_digest import FavoriteDigest, parse_favorite_digest
from core.settings import Settings

MONDAY = datetime(2026, 11, 9, 3, 0)


class _Db:
    is_open = True

    def __init__(self, data=None):
        self.data = dict(data or {})

    async def kv_get(self, key, default=None):
        return self.data.get(key, default)

    async def kv_set(self, key, value, ttl=None):
        self.data[key] = value


class _File:
    def __init__(self, data):
        self.data = data

    def read(self, default=None):
        return self.data

    def write(self, value):
        self.data = value

    def update(self, change, default=None):
        result = change(self.data)
        self.data = self.data if result is None else result
        return self.data


class _Profiles:
    def __init__(self):
        self.profiles, self.updates, self.drops = {}, [], []

    def get(self, uid):
        return self.profiles.get(uid)

    def update(self, uid, **kwargs):
        self.updates.append((uid, kwargs))

    def drop_tags(self, uid, tags):
        self.drops.append((uid, list(tags)))


def _video(bvid, fav_time):
    return {"bvid": bvid, "title": f"视频{bvid}", "up_name": "某UP", "fav_time": fav_time}


def make(favorites, scores, reply="", **config):
    """``favorites``: {uid: [视频]}，每人一个收藏夹，编号 = uid。"""
    videos = types.SimpleNamespace(
        _get_public_fav_folders=AsyncMock(side_effect=lambda uid, limit=3: [{"id": uid, "title": "默认"}] if uid in favorites else []),
        _get_fav_folder_videos=AsyncMock(side_effect=lambda media_id, ps=20: list(favorites.get(media_id, []))),
    )
    llm = AsyncMock(return_value=reply)
    host = types.SimpleNamespace(
        settings=Settings({"ENABLE_FAVORITE_PROFILE": True, "FAVORITE_PROFILE_DAY": 0, "OWNER_MID": "1",
                           "DEDE_USER_ID": "999", **config}),
        affection=Affection(_File(dict(scores)), lambda uid: uid == "1"),
        bili_videos=videos, user_profiles=_Profiles(),
        models=types.SimpleNamespace(_summary_llm_call=llm),
        layered_runtime=types.SimpleNamespace(db=_Db()),
    )
    return FavoriteDigest(host), host, llm


def users(*items):
    return json.dumps({"users": [{"id": i, "impression": "", "tags": t, "drop_tags": d} for i, t, d in items]},
                      ensure_ascii=False)


class ParseTests(unittest.TestCase):
    def test_strict_and_drops_only_known_tags(self):
        notes = parse_favorite_digest(users(("1", ["手工"], ["游戏"])), {"1"}, {"1": ["游戏"]})
        self.assertEqual(notes["1"], {"impression": "", "tags": ["手工"], "drop_tags": ["游戏"]})
        for bad in (users(("1", [], ["没有的标签"])), users(("2", [], [])), '{"users": [{"id": "1"}]}', "不是 JSON"):
            with self.assertRaises(ValueError):
                parse_favorite_digest(bad, {"1"}, {"1": ["游戏"]})


class PickTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_then_close_friends_in_turn_skipping_those_without_new_favorites(self):
        favorites = {"1": [_video("BVo", 10)], "11": [_video("BVa", 5)], "12": [], "13": [_video("BVc", 5)],
                     "14": [_video("BVd", 5)], "15": [_video("BVe", 5)]}
        scores = {"11": 60, "12": 60, "13": 60, "14": 40, "15": 60, "16": 5}
        digest, host, _ = make(favorites, scores, FAVORITE_PROFILE_FRIENDS_PER_WEEK=2)
        picked, cursor = await digest._pick(host.layered_runtime.db)
        self.assertEqual([uid for uid, _v, _m in picked], ["1", "11", "13"])  # 12 没新收藏、14 好感不够
        self.assertEqual(cursor, "13")
        host.layered_runtime.db.data["favorite_digest:cursor"] = cursor
        picked, cursor = await digest._pick(host.layered_runtime.db)
        self.assertEqual([uid for uid, _v, _m in picked], ["1", "15", "11"])  # 接着往后，轮完从头
        self.assertEqual(cursor, "11")

    async def test_friend_level_setting_widens_the_circle(self):
        digest, host, _ = make({"14": [_video("BVd", 5)]}, {"14": 40}, FAVORITE_PROFILE_MIN_LEVEL="friend")
        picked, _ = await digest._pick(host.layered_runtime.db)
        self.assertEqual([uid for uid, _v, _m in picked], ["14"])


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_writes_tags_and_remembers_where_it_stopped_only_on_success(self):
        favorites = {"1": [_video("BVo", 10), _video("BVp", 20)]}
        digest, host, llm = make(favorites, {}, reply="坏掉的")
        db = host.layered_runtime.db
        self.assertEqual(await digest.run(db), 0)
        self.assertNotIn("favorite_digest:seen:1:1", db.data)

        host.user_profiles.profiles["1"] = {"username": "主人", "tags": ["游戏"], "tag_scopes": {}}
        llm.return_value = users(("1", ["手工"], ["游戏"]))
        self.assertEqual(await digest.run(db), 1)
        self.assertEqual(host.user_profiles.drops, [("1", ["游戏"])])
        self.assertEqual(host.user_profiles.updates[0][1]["new_tags"], ["手工"])
        self.assertEqual(db.data["favorite_digest:seen:1:1"], 20)
        prompt = llm.await_args.args[0]
        self.assertIn("视频BVp", prompt)
        self.assertIn("trust=\"untrusted\"", prompt)

        llm.reset_mock()
        self.assertEqual(await digest.run(db), 0)  # 没有新收藏：不调模型
        llm.assert_not_awaited()

    async def test_runs_once_on_its_day(self):
        digest, host, _ = make({}, {})
        with patch.object(digest, "run", AsyncMock(return_value=0)) as run, patch.object(favorite_digest, "logger"):
            await digest.maybe_run(MONDAY.replace(day=10))  # 周二
            run.assert_not_awaited()
            await digest.maybe_run(MONDAY)
            await digest.maybe_run(MONDAY.replace(hour=4))
            self.assertEqual(run.await_count, 1)
            host.layered_runtime.db.is_open = False
            await digest.maybe_run(MONDAY.replace(day=16))
            self.assertEqual(run.await_count, 1)


if __name__ == "__main__":
    unittest.main()


class DropTagsTests(unittest.TestCase):
    def test_drop_tags_keeps_the_rest_and_their_scopes(self):
        from core.domain.user_profiles import UserProfiles
        file = _File({"1": {"tags": ["游戏", "手工"], "tag_scopes": {"游戏": "bili_comment", "手工": "bili_dm"}}})
        profiles = UserProfiles(file)
        self.assertEqual(profiles.drop_tags("1", ["游戏", "没有的"]), 1)
        self.assertEqual(file.data["1"]["tags"], ["手工"])
        self.assertEqual(file.data["1"]["tag_scopes"], {"手工": "bili_dm"})
        self.assertEqual(profiles.drop_tags("2", ["游戏"]), 0)
