"""夜里过一遍备忘录：清掉关掉一天以上的，不想看了的让模型自己丢掉。

睡着时由主干的例行检查（``core/activities.py`` 的 ``ROUTINES``）调，一天一次。
输出严格 JSON；整份不合格就一条都不丢，某一条编号不对只跳过那一条。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from astrbot.api import logger
from ..infra.host_view import HostView
from ..security.redact import wrap_untrusted

_REASON_LIMIT = 40


def parse_memo_review(raw, pending_ids):
    """``{"drop":[{"id":编号,"reason":"…"}]}`` → [(编号, 原因)]；格式不对抛 ``ValueError``。"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw or "").strip(), flags=re.I | re.S)
    value = json.loads(text)
    if not isinstance(value, dict) or set(value) != {"drop"} or not isinstance(value["drop"], list):
        raise ValueError("memo_review_schema_mismatch")
    drops = []
    for item in value["drop"]:
        if not isinstance(item, dict) or set(item) - {"id", "reason"}:
            continue
        memo_id, reason = item.get("id"), item.get("reason", "")
        if isinstance(memo_id, bool) or not isinstance(memo_id, int) or memo_id not in pending_ids:
            continue
        if not isinstance(reason, str) or len(reason) > _REASON_LIMIT:
            continue
        if memo_id not in [kept for kept, _ in drops]:
            drops.append((memo_id, reason.strip() or "不想看了"))
    return drops


class MemoReview(HostView):
    HOST_API = frozenset({"memo_tool", "models", "settings"})

    _reviewed_on = ""

    async def maybe_review(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._reviewed_on == today or not self.memo_tool.enabled():
            return
        self._reviewed_on = today
        memos = self.memo_tool._memos()
        memos.release_stale(today)
        pending = memos.pending()
        purged = memos.purge_closed()
        if purged:
            logger.info(f"[BiliBot] 📝 清掉了 {purged} 条做完或关掉的备忘")
        if not pending:
            return
        lines = []
        for item in pending:
            who = "、".join(p.get("name") or p.get("uid", "") for p in item["recommenders"]) or "自己"
            lines.append(f"#{item['id']} [{item['kind']}] {item['target']}｜推荐：{who}｜{item['note']}")
        prompt = (
            "睡前翻一下备忘录。下面是还没去看的，都是之前记下想看的。"
            "现在已经不想看、觉得没意思、或者明显是别人刷的，可以丢掉；想留着的不用动，一条都不丢也可以。\n"
            + wrap_untrusted("\n".join(lines), "memo_list")
            + '\n只输出一个 JSON 对象：{"drop":[{"id":编号,"reason":"' + f"不超过{_REASON_LIMIT}字" + '"}]}，'
            "不丢就是 {\"drop\":[]}。"
        )
        try:
            raw = await self.models._llm_call(prompt, system_prompt=await self.models._get_system_prompt(),
                                              max_tokens=300)
            drops = parse_memo_review(raw, {item["id"] for item in pending})
        except Exception as exc:
            logger.warning(f"[BiliBot] 夜里过备忘录没成，今天一条都不丢：{exc}")
            return
        for memo_id, reason in drops:
            memos.drop(memo_id, f"夜里自己丢的：{reason}")
        logger.info(f"[BiliBot] 📝 夜里过了一遍备忘录：{len(pending)} 条里丢了 {len(drops)} 条")
