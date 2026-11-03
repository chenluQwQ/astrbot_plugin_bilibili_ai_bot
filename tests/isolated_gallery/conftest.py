"""图库替身会替换 AstrBot 模块，仅在独立测试命令中收集。"""
import os

collect_ignore_glob = [] if os.environ.get("BILIBOT_ISOLATED_GALLERY") == "1" else ["test_*.py"]
