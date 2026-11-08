"""评论、私信进提示词前的检查：恶意关键词、注入特征（命中记安全日志）、包标签。"""
from __future__ import annotations

import re

from ..config import BLOCK_KEYWORDS, INJECTION_PATTERNS


def has_block_keyword(text) -> bool:
    return any(kw in text for kw in BLOCK_KEYWORDS)


def wrap_user_content(content) -> str:
    return f"<user_comment>\n{content}\n</user_comment>"


class InboundGuard:
    def __init__(self, security_log):
        self._security_log = security_log

    def sanitize(self, content, username, mid):
        content = (content or "")[:1000]
        content = re.sub(r'[​-‏ - ⁠-⁯﻿]', '', content)
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                self._security_log.record("injection_attempt", mid, username, content, f"匹配模式: {pattern}")
                return content, True, f"疑似注入: {pattern[:30]}"
        if has_block_keyword(content):
            return content, True, "恶意关键词"
        return content, False, ""
