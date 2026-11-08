"""主动看片搬成多个模块后，测试要替换的时钟、随机数、路径常量分散在这些模块里。"""
import sys
from contextlib import contextmanager
from unittest.mock import patch

MODULES = (
    "core.proactive", "core.domain.video_discovery", "core.features.special_follow", "core.features.proactive_browse",
    "core.features._watch_video", "core.features._owner_recommend", "core.features._video_evaluate",
    "core.features._outward_actions", "core.domain.taste", "core.domain.video_discovery",
    "core.infra.bili.video_pools",
    # 活动日志的文件路径：测试用的假日志每次现查 core.config（tests/activity_parts.py）
    "core.config",
)


def patched_in_proactive_modules(**replacements):
    return patched_in_modules(MODULES, **replacements)


@contextmanager
def patched_in_modules(modules, **replacements):
    """在每个用到这个名字的模块里都替换，没用到的跳过。"""
    active = []
    try:
        for name in modules:
            module = sys.modules.get(name)
            for attr, value in replacements.items():
                if module is not None and hasattr(module, attr):
                    patcher = patch.object(module, attr, value)
                    patcher.start()
                    active.append(patcher)
        yield
    finally:
        for patcher in reversed(active):
            patcher.stop()
