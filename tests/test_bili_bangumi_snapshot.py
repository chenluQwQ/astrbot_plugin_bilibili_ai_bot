"""番剧接口和拉黑的快照：每个方法在正常、业务失败、断网、坏响应下发了什么、返回了什么。

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_bili_bangumi_snapshot.py
"""
from base_parts import BaseParts
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bili_api_snapshot import FAIL, OK, Probe, _plain

from core.infra.bili.users import BiliUsers
from core.infra.bili.bangumi import BiliBangumi

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "bili_bangumi.json"

EP = {"ep_id": 5, "id": 5, "share_copy": "第1话", "long_title": "开始", "title": "1", "cid": 8, "aid": 9,
      "bvid": "BV_EP", "duration": 1200000, "badge": "", "pub_time": 100}
DAY = {"date": "10-30", "day_of_week": 5, "episodes": [
    {"season_id": 1, "episode_id": 5, "title": "番", "pub_index": "第1话", "pub_ts": 100, "cover": "c"},
]}
ITEM = {"season_id": 1, "media_id": 2, "title": "<em>番</em>", "org_title": "原名", "rating": "9.9分",
        "stat": {"view": 3, "follow": 4}, "new_ep": {"index_show": "全12话", "id": 5}, "cover": "c",
        "media_score": {"score": 9.9, "user_count": 7}, "desc": "简介", "ep_size": 12,
        "url": "https://b/1", "badge": "独家", "progress": "看到第1话", "follow_status": 2,
        "season_type": 1, "season_type_name": "番剧", "is_finish": 0}
SEASON = {"season_id": 1, "media_id": 2, "title": "番", "evaluate": "简介", "cover": "c",
          "rating": {"score": 9.9, "count": 7}, "stat": {"views": 3, "favorites": 4},
          "episodes": [EP, "坏数据"], "areas": [{"name": "日本"}], "styles": ["热血"], "total": 12,
          "new_ep": {"desc": "全12话"}, "publish": {"pub_time": "2026"}, "type": 1}

# 方法名 → (参数, 正常时的响应)
CALLS = {
    "search_bilibili_bangumi": (("关键词",), {"code": 0, "data": {"result": [ITEM]}}),
    "get_bangumi_detail": ((1,), {"code": 0, "result": SEASON}),
    "get_bangumi_trending": ((), {"code": 0, "data": {"list": [ITEM]}}),
    "get_bangumi_timeline": ((), {"code": 0, "result": [DAY]}),
    "_get_bangumi_timeline_v1": ((1, 2), {"code": 0, "result": [DAY]}),
    "_get_bangumi_timeline_v2": ((1, 2), {"code": 0, "data": {"timeline": [DAY]}}),
    "_follow_bangumi": ((1,), {"code": 0}),
    "_get_followed_bangumi": ((), {"code": 0, "data": {"list": [ITEM, "坏数据"]}}),
    "_block_user": ((3,), {"code": 0}),
}
EXTRA_CALLS = {
    "detail_by_episode": ("get_bangumi_detail", (), {"ep_id": 5}),
    "detail_without_id": ("get_bangumi_detail", (), {}),
    "trending_other_type": ("get_bangumi_trending", (4,), {}),
}


class BangumiProbe(Probe, BaseParts):
    async def _block_user(self, mid):
        return await BiliUsers(self)._block_user(mid)

    async def _http_get_text(self, url, headers=None, params=None, **kwargs):
        self.calls.append(["GET_TEXT", url, params])
        if isinstance(self.payload, Exception):
            raise self.payload
        return (self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)), None


async def _outcome(payload, name, args, kwargs=None):
    probe = BangumiProbe(payload)
    try:
        outcome = {"result": await getattr(BiliBangumi(probe) if hasattr(BiliBangumi, name) else probe, name)(*args, **(kwargs or {}))}
    except Exception as exc:
        outcome = {"raises": type(exc).__name__}
    return _plain({"calls": probe.calls, **outcome})


class BangumiSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_matches_snapshot(self):
        results = {}
        with patch("time.time", return_value=1_700_000_000.0):
            for name, (args, ok) in CALLS.items():
                for label, payload in (("ok", ok), ("fail", FAIL), ("network", ConnectionError("断网")), ("garbage", "不是JSON")):
                    results[f"{name}/{label}"] = await _outcome(payload, name, args)
            for label, (name, args, kwargs) in EXTRA_CALLS.items():
                results[label] = await _outcome(CALLS[name][1], name, args, kwargs)
        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS") or not SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(results, json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
