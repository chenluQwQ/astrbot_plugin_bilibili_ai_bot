"""发动态的写作和配图：NovelAI 只给真人触发、模型可以选择不发、联网查资料有上限。"""
from memory_parts import MemoryParts
import asyncio
import base64
import io
import json
import types
import unittest
from parts import part
from schedule_parts import ScheduleParts
from people_fakes import LegacyMood
import zipfile

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from core.domain.dynamic_log import DynamicLog
from core.storage.activity_log import ActivityLog
from core.features.dynamic_post import DynamicPost
from core.infra.image_gen import ImageGen
from core.settings import Settings
from itself import itself


def _image_gen(config, cls=ImageGen):
    return cls(types.SimpleNamespace(config=config, settings=Settings(config)))


class NovelAIImageTests(unittest.TestCase):
    def test_json_response_is_decoded(self):
        expected = b"\x89PNG\r\n\x1a\nimage"
        body = json.dumps({"images": [{"image": base64.b64encode(expected).decode("ascii")}]}).encode("utf-8")
        self.assertEqual(ImageGen._decode_novelai_image(body, "application/json"), expected)

    def test_zip_response_is_decoded(self):
        expected = b"\x89PNG\r\n\x1a\nzip-image"
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("image_0.png", expected)
        self.assertEqual(ImageGen._decode_novelai_image(stream.getvalue(), "application/zip"), expected)

    class Recording(ImageGen):
        received = None

        async def _generate_novelai_image(self, prompt, api_key, base_url, model):
            type(self).received = (api_key, base_url, model)
            return "generated.png"

    CONFIG = {"IMAGE_GEN_BACKEND": "novelai", "IMAGE_GEN_API_KEY": "token"}

    def test_not_called_by_automatic_dynamic_task(self):
        self.Recording.received = None
        self.assertIsNone(asyncio.run(_image_gen(self.CONFIG, self.Recording).generate("test", human_initiated=False)))
        self.assertIsNone(self.Recording.received)

    def test_available_for_manual_dynamic_command(self):
        result = asyncio.run(_image_gen(self.CONFIG, self.Recording).generate("test", human_initiated=True))
        self.assertEqual(result, "generated.png")
        self.assertEqual(self.Recording.received, ("token", "https://image.novelai.net", "nai-diffusion-4-5-full"))


class WriterHost(MemoryParts):
    schedule_rules = ScheduleParts.schedule_rules
    daily_plan = ScheduleParts.daily_plan
    models = itself
    web_search = itself

    def __init__(self, replies, config=None):
        self.config = dict(config or {})
        self.settings = Settings(self.config)
        self.replies = replies
        self.prompts = []
        self.queries = []
        self._memory = []
        self.dynamic_log = DynamicLog(ActivityLog("dynamic", file=types.SimpleNamespace(read=lambda default: default)))

    def _load_json(self, _path, default=None):
        return default

    mood = part(LegacyMood)

    def _get_today_mood(self):
        return "平静", ""

    def _autonomous_limit_max(self, _kind):
        return 0

    def _autonomous_plan_for_today(self):
        return {}

    async def _get_system_prompt(self):
        return "测试人设"

    async def _web_search(self, query):
        self.queries.append(query)
        return "某作品第二季定于十月播出"

    async def _generation_llm_call(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        return self.replies.pop(0)


class DynamicWritingTests(unittest.TestCase):
    def _write(self, replies, config=None, **kwargs):
        host = WriterHost(replies, config)
        return host, asyncio.run(DynamicPost(host)._write(**kwargs))

    def test_automatic_dynamic_can_explicitly_choose_silence(self):
        host, result = self._write(['{"decision":"skip","text":"","need_image":false,"image_prompt":""}'], human_initiated=False)
        self.assertEqual(result["decision"], "skip")
        self.assertIn("定时时刻本身不是发布理由", host.prompts[0])
        self.assertIn("近期没有足够具体", host.prompts[0])

    def test_can_search_before_posting(self):
        host, result = self._write([
            '{"decision":"search","text":"","search_query":"某作品 第二季","image":"none","image_prompt":""}',
            '{"decision":"post","text":"第二季十月见，先把第一季结尾再看一遍。","search_query":"","image":"none","image_prompt":""}',
        ], {"ENABLE_WEB_SEARCH": True, "WEB_SEARCH_API_KEY": "k"})
        self.assertEqual(result["decision"], "post")
        self.assertEqual(host.queries, ["某作品 第二季"])
        self.assertIn("某作品第二季定于十月播出", host.prompts[1])

    def test_search_rounds_are_capped(self):
        search = '{"decision":"search","text":"","search_query":"再查一次","image":"none","image_prompt":""}'
        host, result = self._write([search, search], {
            "ENABLE_WEB_SEARCH": True, "WEB_SEARCH_API_KEY": "k", "DYNAMIC_WEB_SEARCH_MAX": 1,
        })
        self.assertIsNone(result)
        self.assertEqual(len(host.queries), 1)
        self.assertIn("本次不能再搜索", host.prompts[1])

    def test_without_web_search_never_offers_search(self):
        host, result = self._write(['{"decision":"search","text":"","search_query":"x","image":"none","image_prompt":""}'])
        self.assertIsNone(result)
        self.assertEqual(host.queries, [])
        self.assertNotIn("联网查资料", host.prompts[0])

    def test_pool_idea_offers_cover_and_custom_prompt(self):
        idea = {"id": 7, "key": "BV1xx", "kind": "分享", "source": "proactive", "title": "测试视频",
                "up_name": "UP", "pic": "https://i0.hdslb.com/a.jpg", "score": 8.0, "review": "好看",
                "angle": "结尾反转早有伏笔", "summary": "讲了猫", "created_at": 0}
        host, result = self._write([
            '{"decision":"post","text":"这期结尾的反转第一分钟就埋了。","search_query":"","image":"video_cover","image_prompt":"","idea":7}',
        ], {"DYNAMIC_PROMPT": "自定义风格：短句"}, after_round=True, ideas=[idea], fresh=(7,))
        self.assertEqual(result["image"], "video_cover")
        self.assertEqual(result["idea"], 7)
        self.assertIn("#7（刚看完，想发成：分享）视频（有封面）", host.prompts[0])
        self.assertIn("结尾反转早有伏笔", host.prompts[0])
        self.assertIn("自定义风格：短句", host.prompts[0])
        self.assertNotIn("B站动态的感觉", host.prompts[0])
        self.assertIn("不能写别人能查证的具体真事", host.prompts[0])

    def test_scheduled_dynamic_respects_post_chance(self):
        host = WriterHost([], {"DYNAMIC_POST_CHANCE": 0})
        self.assertFalse(DynamicPost(host)._admitted(False, False, False))
        self.assertEqual(host.prompts, [])


if __name__ == "__main__":
    unittest.main()
