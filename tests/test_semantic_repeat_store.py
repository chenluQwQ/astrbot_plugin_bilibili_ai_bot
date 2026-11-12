"""语义去重库：老用户文件里的字符串指纹不能让评论轮询报错。"""
import types
import re
import unittest

# 先导入它：它会装上假的 astrbot 模块。
import test_autonomous_ranges  # noqa: F401
from reply_parts import ReplyParts
from core.video_evaluation import VIDEO_EVALUATION_SCHEMA_PROMPT
from core.infra.vision import Vision


from core.domain.replied_content import RepliedContent
from reply_owners import HostFile
from core.storage.processed_keys import ContentEntries

class StoreBot(ReplyParts):
    def __init__(self, store):
        self.config = {"REPLY_SIMILARITY_PERCENT": 90}
        self.store = store
        # 这个假插件不管读哪个文件都给同一份，只挂语义去重库。
        self.replied_content = RepliedContent(ContentEntries(file=HostFile(self, "replied_content_keys.json")))
        self.vision = types.SimpleNamespace(_get_embedding=self._get_embedding, _cosine_similarity=Vision._cosine_similarity)

    def _load_json(self, path, default):
        return self.store

    def _save_json(self, path, data):
        self.store = data

    async def _get_embedding(self, text):
        return [1.0, 0.0]


class SemanticRepeatStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_string_entries_do_not_crash(self):
        bot = StoreBot(["123:456:abc", "789:012:def"])
        self.assertEqual(await bot.comment_reply._is_semantically_repeated("你好"), (False, [1.0, 0.0]))

    async def test_entries_after_legacy_strings_still_match(self):
        bot = StoreBot(["123:456:abc", {"text": "你好", "embedding": [1.0, 0.0]}])
        repeated, _ = await bot.comment_reply._is_semantically_repeated("你好呀")
        self.assertTrue(repeated)

    async def test_recording_cleans_legacy_strings(self):
        bot = StoreBot(["123:456:abc"])
        await bot.comment_reply._record_replied_content("你好", [1.0, 0.0])
        self.assertEqual([it["text"] for it in bot.store], ["你好"])

    async def test_non_list_file_is_treated_as_empty(self):
        bot = StoreBot({"oops": 1})
        self.assertEqual(await bot.comment_reply._is_semantically_repeated("你好"), (False, [1.0, 0.0]))


class EvaluationPromptTests(unittest.TestCase):
    def test_prompt_asks_for_less_than_the_validator_enforces(self):
        # 提示词要的字数比校验上限短一截，模型写超一点也不至于整条作废
        for field, limit in (("score_reason", 120), ("review", 180), ("recommend_reason", 80), ("dynamic_angle", 80)):
            asked = re.search(rf"{field} [^，；]*?(\d+)字以内", VIDEO_EVALUATION_SCHEMA_PROMPT)
            self.assertIsNotNone(asked, field)
            self.assertLessEqual(int(asked.group(1)) * 3, limit * 2, field)


if __name__ == "__main__":
    unittest.main()
