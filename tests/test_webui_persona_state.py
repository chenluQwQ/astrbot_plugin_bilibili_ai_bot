"""The WebUI status card shows the mood the bot actually uses, not a separate engine's."""

import types
import unittest
from parts import part
from people_fakes import LegacyMood

from test_webui_interest import _install_astrbot_stub

_install_astrbot_stub()

from core.lifecycle.now_doing import NowDoing
from core.web import status


class _Plugin:
    def __init__(self, mood):
        self.config = {"AUTONOMOUS_ACTIVITY_LEVEL": 62, "SLEEP_START": 0, "SLEEP_END": 0}
        self.mood = types.SimpleNamespace(today=lambda: mood)
        # 旧版本这里有一个独立的精力引擎，心情永远是 calm；有它也不该被读
        self.layered_runtime = types.SimpleNamespace(is_open=True)

    mood = part(LegacyMood)

    def _get_today_mood(self):
        return self.mood.today()


class PersonaStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_shows_todays_settled_mood(self):
        result = await status.handle_get_persona_state(_Plugin(("😏 有点皮", "偶尔多一点调侃。")))
        data = result["data"]
        self.assertEqual(data["mood"], "有点皮")
        self.assertEqual(data["energy"], 62)
        self.assertEqual(data["current_mode"], "active")
        self.assertEqual(data["doing"], "")  # 没有 now_doing 也不报错

    async def test_says_what_the_bot_is_doing(self):
        plugin = _Plugin(("😊 开心", ""))
        plugin.now_doing = NowDoing()
        plugin.now_doing.report("bangumi", "看第 3 话", title="示例番剧")
        data = (await status.handle_get_persona_state(plugin))["data"]
        self.assertEqual(data["doing"], "正在追番：看第 3 话；内容：《示例番剧》")
        self.assertEqual((data["sleep_start"], data["sleep_end"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
