"""B站 HTTP 层：收到风控码通知上层清 WBI 缓存，别的响应不动它。"""
import unittest
from unittest.mock import patch

from test_autonomous_ranges import _install_astrbot_stub

_install_astrbot_stub()

from base_parts import BaseParts


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, **kwargs):
        return FakeResponse(self.payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Bot(BaseParts):
    def __init__(self):
        self.config = {"SESSDATA": "s"}
        self._wbi_keys_cache = (1, "i", "s")


class RiskControlTests(unittest.IsolatedAsyncioTestCase):
    async def _get(self, bot, payload):
        with patch("core.infra.bili.http.plugin_http_session", lambda config: FakeSession(payload)):
            return (await bot.bili_http._http_get("https://api/x"))[0]

    async def test_risk_code_forgets_wbi_keys_and_still_returns(self):
        bot = Bot()
        self.assertEqual(await self._get(bot, {"code": -352}), {"code": -352})
        self.assertIsNone(bot._wbi_keys_cache)

    async def test_other_codes_keep_wbi_keys(self):
        bot = Bot()
        await self._get(bot, {"code": 0})
        self.assertIsNotNone(bot._wbi_keys_cache)


class RecentRequestTests(unittest.IsolatedAsyncioTestCase):
    """私信被 -509 频控时，日志要列出那几分钟里各接口的请求次数。"""

    async def test_requests_are_counted_by_path(self):
        from core.infra.bili import request_log

        request_log._RECENT_REQUESTS.clear()
        bot = Bot()
        with patch("core.infra.bili.http.plugin_http_session", lambda config: FakeSession({"code": 0})):
            await bot.bili_http._http_get("https://api.bilibili.com/x/v2/reply/main?oid=1")
            await bot.bili_http._http_get("https://api.bilibili.com/x/v2/reply/main?oid=2")
        request_log.note_request("POST", "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg")

        counts = request_log.recent_request_counts(300)
        self.assertEqual(counts["GET /x/v2/reply/main"], 2)
        self.assertEqual(counts["POST /web_im/v1/web_im/send_msg"], 1)
        self.assertEqual(sum(request_log.recent_request_counts(-1).values()), 0)


if __name__ == "__main__":
    unittest.main()
