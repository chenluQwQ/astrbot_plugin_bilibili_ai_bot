"""B站原生表情：白名单、提示词、发送前清洗。"""
import unittest

from core.domain.emotes import EMOTES, emote_prompt, tidy_emotes


class TidyEmotesTests(unittest.TestCase):
    def test_keeps_the_first_listed_code_only(self):
        self.assertEqual(tidy_emotes("好耶[doge][吃瓜]"), "好耶[doge]")

    def test_drops_codes_outside_the_list(self):
        self.assertEqual(tidy_emotes("好呀[鸣潮_好呀] 下次见[原神_哇]"), "好呀 下次见")
        self.assertEqual(tidy_emotes("[鸣潮_好呀]"), "")

    def test_leaves_ordinary_brackets_alone(self):
        for text in ("见第[1]条", "[ 空格 ]不算", "版本[v2.0]", "看这里[]"):
            self.assertEqual(tidy_emotes(text), text)

    def test_plain_text_is_unchanged(self):
        self.assertEqual(tidy_emotes("今天看了一个手绘动画"), "今天看了一个手绘动画")

    def test_prompt_lists_every_code(self):
        prompt = emote_prompt()
        for code in EMOTES:
            self.assertIn(code, prompt)
        self.assertIn("最多一个", prompt)


if __name__ == "__main__":
    unittest.main()
