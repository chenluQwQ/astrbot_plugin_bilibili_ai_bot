"""B站接口层的快照：每个方法在三种响应下发了什么请求、返回了什么。

分组搬进 ``core/infra/bili/`` 时用它确认没搬错。

Regenerate after an intended behaviour change with:
    BILIBOT_UPDATE_SNAPSHOTS=1 python -m pytest tests/test_bili_api_snapshot.py
"""
import json
import os
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from bili_parts import OWNERS, BiliParts

SNAPSHOT_FILE = Path(__file__).parent / "snapshots" / "bili_api.json"

OK = {"code": 0, "message": "0", "data": {
    "uname": "柠", "mid": 1, "level_info": {"current_level": 6}, "refresh": True, "timestamp": 1,
    "url": "https://passport.example/x?SESSDATA=s&bili_jct=j&DedeUserID=1", "qrcode_key": "k", "code": 0,
    "refresh_token": "rt", "wbi_img": {"img_url": "https://i/" + "a1b2" * 8 + ".png", "sub_url": "https://i/" + "c3d4" * 8 + ".png"},
    "b_3": "b3", "b_4": "b4", "aid": 11, "cid": 22, "bvid": "BV1", "title": "标题", "desc": "简介",
    "owner": {"mid": 3, "name": "UP"}, "stat": {"view": 9}, "tname": "动画", "pic": "p", "duration": 60,
    "pubdate": 100, "pages": [{"cid": 22}], "subtitle": {"subtitles": []},
    "replies": [{"content": {"message": "热评"}, "member": {"uname": "甲"}, "like": 5}],
    "list": [{"mid": 5, "uname": "乙"}], "result": [], "items": [], "info": {"face": "f"},
    "name": "UP", "sign": "签名", "follower": 7, "image_url": "https://i/up.png", "image_width": 1,
    "image_height": 1, "dyn_id_str": "77", "dynamic_id": 77, "is_favorite": False, "folders": [],
}}
FAIL = {"code": -101, "message": "账号未登录", "data": None}


class Config(dict):
    def save_config(self):
        pass


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.cookies = {}

    async def json(self, content_type=None):
        return self.payload

    async def text(self):
        return json.dumps(self.payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Probe(BiliParts):
    def __init__(self, payload):
        self.config = Config(SESSDATA="sess", BILI_JCT="jct", DEDE_USER_ID="1", REFRESH_TOKEN="rt", OWNER_MID="2")
        self.payload = payload
        self.calls = []

    def _respond(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload, FakeResponse(self.payload)

    async def _http_get(self, url, headers=None, params=None, **kwargs):
        self.calls.append(["GET", url, params, bool(headers)])
        return self._respond()

    async def _http_post(self, url, headers=None, data=None, **kwargs):
        self.calls.append(["POST", url, data if not hasattr(data, "add_field") else "form", bool(headers)])
        return self._respond()

    async def _http_get_text(self, url, headers=None, params=None, **kwargs):
        self.calls.append(["GET_TEXT", url.split("correspond/1/")[0], params])
        if isinstance(self.payload, Exception):
            raise self.payload
        return '<div id="1-name">csrf</div>', None


class FakeSession:
    def __init__(self, probe):
        self.probe = probe

    def _request(self, method, url, kwargs):
        data = kwargs.get("data")
        self.probe.calls.append([
            method, url, kwargs.get("params"), kwargs.get("json"),
            data if isinstance(data, dict) else None,
        ])
        if isinstance(self.probe.payload, Exception):
            raise self.probe.payload
        return FakeResponse(self.probe.payload)

    def get(self, url, **kwargs):
        return self._request("SESSION_GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._request("SESSION_POST", url, kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# 方法名 → 调用参数。新增接口方法时要加进来（test_every_method_is_covered 会提醒）。
CALLS = {
    "_ensure_buvid": (), "check_cookie": (), "check_need_refresh": (), "generate_qr": (),
    "poll_qr_status": ("k",), "refresh_cookie": (), "_get_wbi_keys": (),
    "sign_wbi_params": ({"a": 1},), "_wbi_get": ("https://api/x", {"a": 1},),
    "_qr_login_generate": (), "_qr_login_poll": ("k",),
    "_send_reply": (11, 22, 1, "回复"), "_send_comment": (11, "评论"),
    "_send_bili_private_message": (2, "你好"),
    "_send_bili_private_video_share": (2, {
        "bvid": "BV1", "aid": 11, "cid": 22, "title": "标题", "pic": "p", "desc": "简介",
        "owner": {"mid": 3, "name": "UP", "face": "f"},
    }),
    "get_followings": (), "_oid_to_bvid": (11,), "_get_video_info": ("BV1",),
    "_get_video_subtitles": ("BV1", 22), "_get_video_tags": ("BV1",),
    "_get_hot_comments": (11,), "_get_video_oid": ("BV1",),
    "_like_video": (11,), "_coin_video": (11,), "_fav_video": (11,), "_follow_user": (3,),
    "_post_dynamic_text": ("动态",), "_repost_video": (11, "转发语"), "_repost_dynamic": ("900", "转发语"), "_get_up_latest_video": (3,),
    "search_bilibili_videos": ("关键词",), "search_bilibili_users": ("关键词",),
    "get_up_info": (3,), "get_up_recent_videos": (3,), "get_up_recent_dynamics": (3,),
    "get_following_updates": (), "get_following_live": (),
}
# 只算不发请求、或要真文件的，不进快照。
NOT_RECORDED = {"_generate_correspond_path", "_get_mixin_key", "_strip_at_prefix",
                "_send_bili_private_payload", "_upload_image_to_bilibili", "_post_dynamic_with_image",
                "_get_comment_images"}  # 评论图片在 test_vision_search_snapshot 录
RESPONSES = {"ok": OK, "fail": FAIL, "network": ConnectionError("断网")}


def _plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=lambda o: f"<{type(o).__name__}>"))


class BiliApiSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_matches_snapshot(self):
        with patch("time.time", return_value=1_700_000_000.0), patch(
            "uuid.uuid4", return_value="00000000-0000-0000-0000-000000000000"
        ), patch(
            "core.network.aiohttp.ClientSession", side_effect=lambda **_: self.session()
        ):
            self.current = None
            results = {}
            for name, args in CALLS.items():
                for label, payload in RESPONSES.items():
                    probe = Probe(payload)
                    self.current = probe
                    try:
                        # 转发的 upload_id 带一个随机数，只在这里钉住，免得动到别的接口的随机。
                        with (patch("random.randint", return_value=1234) if name in ("_repost_video", "_repost_dynamic") else nullcontext()):
                            outcome = {"result": await getattr(getattr(probe, OWNERS[name]), name)(*args)}
                    except Exception as exc:
                        outcome = {"raises": type(exc).__name__}
                    results[f"{name}/{label}"] = _plain({"calls": probe.calls, **outcome})
        if os.environ.get("BILIBOT_UPDATE_SNAPSHOTS") or not SNAPSHOT_FILE.exists():
            SNAPSHOT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(results, json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")))

    def session(self):
        return FakeSession(self.current)

    def test_every_method_is_covered(self):
        methods = set(OWNERS)
        self.assertEqual(methods - set(CALLS) - NOT_RECORDED, set())


if __name__ == "__main__":
    unittest.main()
