"""评论和私信回复按阶段拆成多个模块后，测试要替换的时钟、随机数分散在这些模块里。"""
import core.features._reply_consequences  # noqa: F401
import core.features._reply_writing  # noqa: F401
import core.features.comment_context  # noqa: F401
import core.features.comment_reply  # noqa: F401
import core.features._private_lookup  # noqa: F401
import core.features._private_safety  # noqa: F401
import core.features.private_poll  # noqa: F401
import core.features.private_reply  # noqa: F401
from proactive_modules import patched_in_modules

MODULES = (
    "core.features._reply_gate", "core.features._reply_writing", "core.features._reply_consequences",
    "core.features._abuse_alert", "core.features.comment_reply", "core.features.comment_context",
    "core.features._private_safety", "core.features._private_lookup", "core.features.private_poll",
    "core.features.private_reply", "core.domain.private_inbox",
)


def patched_in_reply_modules(**replacements):
    return patched_in_modules(MODULES, **replacements)
