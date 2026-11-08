"""The version AstrBot shows (metadata.yaml) and the one the code uses agree."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PluginVersionTests(unittest.TestCase):
    def test_metadata_and_code_versions_match(self):
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        config = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
        meta_version = re.search(r"^version:\s*(\S+)", metadata, re.M).group(1)
        code_version = re.search(r'^PLUGIN_VERSION = "([^"]+)"', config, re.M).group(1)
        self.assertEqual(meta_version, code_version)


if __name__ == "__main__":
    unittest.main()
