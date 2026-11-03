"""在子进程运行图库故障测试，避免 AstrBot 测试替身污染其他模块。"""
import os
from pathlib import Path
import subprocess
import sys


def test_gallery_retention_sync_selection_and_emotes():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/isolated_gallery"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "BILIBOT_ISOLATED_GALLERY": "1"},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
