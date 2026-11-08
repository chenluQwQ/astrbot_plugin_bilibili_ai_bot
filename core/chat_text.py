"""Helpers for text that commands send back to chat platforms."""

CHAT_CHUNK_LIMIT = 1500


def split_for_chat(text, limit=CHAT_CHUNK_LIMIT):
    """Split long command output into several messages at line breaks.

    Lines are never cut, so a review or comment always arrives whole; a single
    line longer than ``limit`` is sent as its own message.
    """
    chunks, current = [], ""
    for line in str(text or "").split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


_SENTENCE_ENDS = "。！？!?；;\n…"
_CLAUSE_ENDS = "，,、：: "


def clip(text, limit):
    """Shorten context that only the model reads, keeping at most ``limit`` characters.

    Cuts after the last sentence end inside the limit, else after the last
    clause break, and only cuts mid-phrase when neither is past half the limit.
    A shortened text ends with "…" so the model knows it is not the whole thing.
    Text people read in chat is never clipped; it is split with ``split_for_chat``.
    """
    text = str(text or "")
    if len(text) <= limit:
        return text
    head = text[:limit]
    for ends in (_SENTENCE_ENDS, _CLAUSE_ENDS):
        cut = max(head.rfind(ch) for ch in ends)
        if cut + 1 >= limit // 2:
            return head[:cut + 1].rstrip() + "…"
    return head + "…"
