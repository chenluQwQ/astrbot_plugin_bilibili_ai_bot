"""插件还要在 Python 3.12 以下加载：f-string 里不能嵌同一种引号（3.12 才允许，低版本导入时直接语法错误）。"""
import sys
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _quote(token_text):
    text = token_text.lstrip("rRbBuUfF")
    return text[:3] if text[:3] in ('"""', "'''") else text[0]


def nested_same_quotes(path):
    found, stack = [], []
    with open(path, encoding="utf-8") as handle:
        for token in tokenize.generate_tokens(handle.readline):
            if token.type == tokenize.FSTRING_START:
                if _quote(token.string) in stack:
                    found.append(token.start[0])
                stack.append(_quote(token.string))
            elif token.type == tokenize.FSTRING_END:
                stack.pop()
            elif stack and token.type == tokenize.STRING and _quote(token.string) in stack:
                found.append(token.start[0])
    return found


@unittest.skipIf(sys.version_info < (3, 12), "低版本解释器读到这种写法自己就会报语法错误")
class FStringQuoteTests(unittest.TestCase):
    def test_no_fstring_nests_its_own_quote(self):
        files = [ROOT / "main.py", *(ROOT / "core").rglob("*.py")]
        bad = {str(path.relative_to(ROOT)): lines for path in files if (lines := nested_same_quotes(path))}
        self.assertEqual(bad, {})


if __name__ == "__main__":
    unittest.main()
