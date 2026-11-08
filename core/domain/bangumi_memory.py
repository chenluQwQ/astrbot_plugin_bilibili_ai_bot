"""番剧记忆：每部番看过哪几集、每集的评分和感想。

``bangumi_memory.json`` 只由这里读写。
"""
from __future__ import annotations

from datetime import datetime

from astrbot.api import logger

from ..storage.json_file import JsonFile


def sort_episodes(episodes):
    """按集号排序（集号可能是 "1" "2" "PV" 等）。"""
    def _key(episode):
        index = episode.get("ep_index", "")
        try:
            return (0, int(index))
        except (ValueError, TypeError):
            return (1, index)
    return sorted(episodes, key=_key)


def find_missing_episodes(episodes):
    """检测缺失集号（只对纯数字集号生效）。"""
    indices = []
    for episode in episodes:
        try:
            indices.append(int(episode.get("ep_index", "")))
        except (ValueError, TypeError):
            continue
    if len(indices) < 2:
        return []
    return sorted(set(range(min(indices), max(indices) + 1)) - set(indices))


class BangumiMemory:
    def __init__(self, file: JsonFile):
        self._file = file

    def all(self) -> dict:
        return self._file.read({})

    def watched_ep_ids(self, season_id) -> set:
        record = self.all().get(str(season_id))
        if not record:
            return set()
        # 感想只留最近 50 集，看过哪些集要单独记全，不然长番前面的集会被当成没看过
        return set(record.get("watched_ep_ids", [])) | {e.get("ep_id") for e in record.get("episodes", []) if e.get("ep_id")}

    def mark_completed(self, season_id) -> bool:
        """标记为已看完，选番时排除。没有这部番的记录时什么也不做，返回 False。"""
        sid = str(season_id)
        if sid not in self.all():
            return False

        def mark(memory):
            memory[sid]["completed"] = True
        self._file.update(mark, {})
        return True

    def save_episode(self, season_id, season_title, ep_info, score, mood, review, comment, description) -> None:
        """保存单集记忆。同一集再看一次会覆盖旧记录。"""
        watched_indices = []

        def put(memory):
            sid = str(season_id)
            if sid not in memory:
                memory[sid] = {"title": season_title, "season_id": season_id, "total_watched": 0, "last_ep_index": "", "last_score": 0, "episodes": []}
            record = memory[sid]
            ep_id = ep_info.get("ep_id", 0)
            if ep_id:
                record["episodes"] = [e for e in record["episodes"] if e.get("ep_id") != ep_id]
            record["episodes"].append({
                "ep_id": ep_id,
                "ep_index": ep_info.get("ep_index", ""),
                "title": ep_info.get("title", ""),
                "score": score, "mood": mood,
                "review": review[:100], "comment": comment[:60],
                "description": description[:200],
                "watched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            record["episodes"] = sort_episodes(record["episodes"])
            watched_ids = set(record.get("watched_ep_ids", [])) | {e.get("ep_id") for e in record["episodes"] if e.get("ep_id")}
            record["watched_ep_ids"] = sorted(watched_ids)
            record["total_watched"] = max(len(record["episodes"]), len(watched_ids))
            record["last_ep_index"] = ep_info.get("ep_index", "")
            record["last_score"] = score
            # 已看集号列表（方便展示）
            watched_indices[:] = [e.get("ep_index", "") for e in record["episodes"] if e.get("ep_index")]
            record["watched_eps"] = list(watched_indices)
            if len(record["episodes"]) > 50:
                record["episodes"] = record["episodes"][-50:]

        try:
            self._file.update(put, {})
            logger.info(f"[BiliBot] 🎬 番剧记忆已保存: 《{season_title}》第{ep_info.get('ep_index', '?')}话 (已看:{','.join(watched_indices)})")
        except Exception as e:
            logger.error(f"[BiliBot] 保存番剧记忆失败: {e}")
