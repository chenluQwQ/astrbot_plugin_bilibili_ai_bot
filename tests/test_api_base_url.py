import unittest

from test_autonomous_ranges import _install_astrbot_stub  # noqa: F401  (installs the AstrBot stub)
from core.utils import UtilsMixin


class NormalizeOpenAIBaseUrlTest(unittest.TestCase):
    def normalize(self, value):
        return UtilsMixin._normalize_openai_base_url(value)

    def test_bare_host_gets_v1(self):
        self.assertEqual(self.normalize("https://example.com"), "https://example.com/v1")
        self.assertEqual(self.normalize("https://example.com/"), "https://example.com/v1")
        self.assertEqual(self.normalize("example.com:8000"), "example.com:8000/v1")

    def test_v1_is_not_duplicated(self):
        self.assertEqual(self.normalize("https://example.com/v1/"), "https://example.com/v1")
        self.assertEqual(self.normalize("https://example.com/V1"), "https://example.com/V1")

    def test_other_paths_are_kept(self):
        self.assertEqual(self.normalize("https://example.com/api/v3"), "https://example.com/api/v3")
        self.assertEqual(self.normalize("https://example.com/compatible-mode/v1"), "https://example.com/compatible-mode/v1")
        self.assertEqual(self.normalize("https://example.com/v1beta/openai/"), "https://example.com/v1beta/openai")

    def test_empty(self):
        self.assertEqual(self.normalize(""), "")
        self.assertEqual(self.normalize(None), "")


if __name__ == "__main__":
    unittest.main()
