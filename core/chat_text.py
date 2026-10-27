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
