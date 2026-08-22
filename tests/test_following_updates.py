import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _load_bilibili_module():
    package_name = "following_updates_core"
    package = sys.modules.setdefault(package_name, types.ModuleType(package_name))
    package.__path__ = [str(ROOT / "core")]

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    api.logger = _Logger()
    astrbot.api = api

    config = types.ModuleType(f"{package_name}.config")
    for name in (
        "BILI_COOKIE_CONFIRM_URL", "BILI_COOKIE_INFO_URL", "BILI_COOKIE_REFRESH_URL",
        "BILI_DYNAMIC_IMAGE_URL", "BILI_DYNAMIC_TEXT_URL", "BILI_NAV_URL",
        "BILI_PRIVATE_MSG_SEND_URL", "BILI_QR_GENERATE_URL", "BILI_QR_POLL_URL",
        "BILI_REPLY_URL", "BILI_UPLOAD_IMAGE_URL",
    ):
        setattr(config, name, "https://example.test")
    config.BILI_RSA_PUBLIC_KEY = ""
    config.MIXIN_KEY_ENC_TAB = []
    config.USER_AGENT = "test"
    sys.modules[config.__name__] = config

    module_name = f"{package_name}.bilibili"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "core" / "bilibili.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FollowingUpdatesTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_bilibili_module()

    async def test_numeric_string_timestamp_and_limit_are_accepted(self):
        today_ts = str(int(datetime.now().timestamp()))

        class Client(self.module.BilibiliAPIMixin):
            async def _http_get(self, *_args, **_kwargs):
                return {
                    "code": 0,
                    "data": {
                        "items": [{
                            "id_str": "1",
                            "type": "DYNAMIC_TYPE_AV",
                            "modules": {
                                "module_author": {
                                    "pub_ts": today_ts,
                                    "pub_time": "刚刚",
                                    "name": "测试UP",
                                    "mid": 123,
                                },
                                "module_dynamic": {
                                    "desc": {"text": "测试动态"},
                                    "major": {},
                                },
                            },
                        }],
                    },
                }, None

        results = await Client().get_following_updates(limit="20")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["up_name"], "测试UP")

    async def test_bad_timestamp_does_not_drop_the_whole_feed(self):
        class Client(self.module.BilibiliAPIMixin):
            async def _http_get(self, *_args, **_kwargs):
                return {
                    "code": 0,
                    "data": {
                        "items": [{
                            "id_str": "2",
                            "type": "DYNAMIC_TYPE_WORD",
                            "modules": {
                                "module_author": {
                                    "pub_ts": "not-a-timestamp",
                                    "pub_time": "刚刚",
                                    "name": "异常时间UP",
                                    "mid": 456,
                                },
                                "module_dynamic": {
                                    "desc": {"text": "仍应返回"},
                                    "major": {},
                                },
                            },
                        }],
                    },
                }, None

        results = await Client().get_following_updates(limit="bad")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["text"], "仍应返回")


if __name__ == "__main__":
    unittest.main()
