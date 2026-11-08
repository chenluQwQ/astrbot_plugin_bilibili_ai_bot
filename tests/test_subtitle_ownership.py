"""字幕归属：B站偶尔返回别的视频的字幕。地址都来自 2026-11-01 的实测结果。"""
import types
import unittest

from bili_parts import attach_bili_groups
from unittest.mock import AsyncMock

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from core.infra.bili.videos import BiliVideos, _own_subtitle_tracks

AID, CID = 832013736, 1297366664
PREFIX = "https://aisubtitle.hdslb.com/bfs/ai_subtitle/prod/"
OWN = {"lan": "ai-zh", "subtitle_url": PREFIX + "8320137361297366664d0324aa4d9011dbc65420e86c76448f6"}
OTHER = {"lan": "ai-zh", "subtitle_url": PREFIX + "11329937500056526271482358697db7284d1862fe5924d7b06a45302b"}
HASH_ONLY = {"lan": "ai-en", "subtitle_url": PREFIX + "3586e0d03cde8e909afa930bef571b46"}  # 实际运行时见过的格式
NO_URL = {"lan": "ai-zh", "subtitle_url": ""}
HUMAN = {"lan": "zh-Hans", "subtitle_url": "https://aisubtitle.hdslb.com/bfs/subtitle/a17fb0aca6beb35b7ce7036ae811bd77b78461a5.json"}


class OwnTracksTests(unittest.TestCase):
    def test_keeps_own_auto_subtitle(self):
        self.assertEqual(_own_subtitle_tracks([OWN], AID, CID), [OWN])

    def test_drops_auto_subtitle_of_another_video(self):
        self.assertEqual(_own_subtitle_tracks([OTHER], AID, CID), [])

    def test_hash_only_auto_subtitle_cannot_be_checked_and_is_kept(self):
        self.assertEqual(_own_subtitle_tracks([HASH_ONLY, OTHER], AID, CID), [HASH_ONLY])

    def test_drops_track_without_url(self):
        self.assertEqual(_own_subtitle_tracks([NO_URL], AID, CID), [])

    def test_human_subtitle_cannot_be_checked_and_is_kept(self):
        self.assertEqual(_own_subtitle_tracks([HUMAN], AID, CID), [HUMAN])

    def test_missing_list(self):
        self.assertEqual(_own_subtitle_tracks(None, AID, CID), [])


def _api(player, subtitle_body=None, aid=AID):
    host = types.SimpleNamespace(
        _wbi_get=AsyncMock(return_value=(player, None)),
        _http_get=AsyncMock(side_effect=lambda url, params=None: (
            ({"code": 0, "data": {"aid": aid}} if "web-interface/view" in url else {"body": subtitle_body or []}), None
        )),
    )
    return BiliVideos(attach_bili_groups(host)), host


def _player(*tracks):
    return {"code": 0, "data": {"subtitle": {"subtitles": list(tracks)}}}


class FetchSubtitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_request_uses_aid_and_cid(self):
        api, host = _api(_player(OWN), [{"content": "立刻修复"}])
        self.assertEqual(await api._get_video_subtitles("BV1", CID), "立刻修复")
        host.bili_wbi._wbi_get.assert_awaited_once_with("https://api.bilibili.com/x/player/wbi/v2", {"aid": AID, "cid": CID})

    async def test_mismatched_subtitle_is_never_downloaded(self):
        api, host = _api(_player(OTHER), [{"content": "煎饼果子"}])
        self.assertEqual(await api._get_video_subtitles("BV1", CID), "")
        self.assertEqual([call.args[0] for call in host.bili_http._http_get.await_args_list],
                         ["https://api.bilibili.com/x/web-interface/view"])

    async def test_own_track_is_picked_over_mismatched_one(self):
        api, _ = _api(_player(OTHER, OWN), [{"content": "立刻修复"}])
        self.assertEqual(await api._get_video_subtitles("BV1", CID), "立刻修复")

    async def test_unknown_aid_or_api_error_returns_nothing(self):
        api, host = _api(_player(OWN), aid=0)
        self.assertEqual(await api._get_video_subtitles("BV1", CID), "")
        host.bili_wbi._wbi_get.assert_not_awaited()
        api, _ = _api({"code": -352, "data": None})
        self.assertEqual(await api._get_video_subtitles("BV1", CID), "")


if __name__ == "__main__":
    unittest.main()
