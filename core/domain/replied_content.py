"""语义去重库：只由这里写（存储见 ``storage/processed_keys.py``）。存回复过的评论原话和它的向量，最多 80 条。

算向量要调模型，那是流程的事；这里只存、只比。早期文件里存的是字符串指纹，导入和读的时候都滤掉。
"""
from __future__ import annotations

from datetime import datetime

from ..storage.processed_keys import ContentEntries


class RepliedContent:
    def __init__(self, entries: ContentEntries):
        self._entries = entries

    def items(self) -> list[dict]:
        return self._entries.items()

    def has_similar(self, emb, threshold: float, cosine) -> bool:
        for it in self.items():
            e = it.get("embedding")
            if e and len(e) == len(emb) and cosine(emb, e) >= threshold:
                return True
        return False

    def record(self, text: str, emb) -> None:
        self._entries.append({"text": text[:100], "embedding": emb, "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
