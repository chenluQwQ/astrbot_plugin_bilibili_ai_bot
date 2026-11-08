"""回复评论、私信时记备忘录的 ``bili_memo`` 工具，评论和私信共用。

存储和规则在 ``domain/memos.py``；这里只管谁能记（好感为负的不记）和给模型的说明。
记下来的东西都来自别人的话，以后放进提示词时要包成不可信资料。
"""
from ..domain._memory_tags import UP, VIDEO, tag
from ..domain.memos import KINDS, NOTE_LIMIT, TARGET_LIMIT, WHENS, MemoRejected, Memos, memo_key
from ..infra.host_view import HostView
from ..infra.tool_loop import ToolMiss

MEMO_TOOL_DESCRIPTION = (
    "记一笔备忘：自己感兴趣、想之后去看的视频、番剧、UP主或别的事。别人推荐不等于要记，不想记就不用；"
    "记了也不用在回复里承诺什么时候去看"
)
MEMO_TOOL_PARAMETERS = {
    "type": "object", "required": ["kind", "target", "when"],
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS), "description": "视频 video / 番剧 bangumi / UP主 up / 别的 other"},
        "target": {"type": "string", "description": f"视频填 BV 号，番剧填番名，UP主填昵称或 UID，别的写一句；最多{TARGET_LIMIT}字"},
        "note": {"type": "string", "description": f"为什么想看，最多{NOTE_LIMIT}字"},
        "when": {"type": "string", "enum": list(WHENS), "description": "今天 today / 明天 tomorrow / 以后再说 someday"},
    },
}


class MemoTool(HostView):
    HOST_API = frozenset({"affection", "memories", "roles", "seen_videos", "settings"})

    def enabled(self):
        return bool(self.settings.get("ENABLE_MEMO"))

    def _memos(self):
        pool = getattr(self._host, "memos", None)
        if pool is None:
            pool = self._host.memos = Memos.in_memory()
        try:
            pool.max_pending = max(1, int(self.settings.get("MEMO_MAX_PENDING")))
        except (TypeError, ValueError):
            pass
        return pool

    async def record(self, *, kind, target, note, when, channel, uid, name):
        """记下来，返回给模型的一句话；不记的时候抛 ``ToolMiss`` 说明原因。"""
        uid = str(uid or "")
        is_owner = bool(uid) and self.roles.is_owner(uid)
        if uid and not is_owner and self.affection.score(uid) < 0:
            raise ToolMiss("这个人推荐的不记。")
        if kind == "video" and (bvid := memo_key("video", str(target or ""))) and await self.seen_videos.has_seen(bvid):
            raise ToolMiss("这个视频已经看过了，不用再记。")
        try:
            memo_id, created = self._memos().add(
                kind=kind, target=target, note=note or "", when=when,
                source="owner" if is_owner else channel, from_uid=uid, from_name=name, unlimited=is_owner,
            )
        except MemoRejected as exc:
            raise ToolMiss(str(exc))
        answer = f"记下了（备忘 #{memo_id}）。" if created else f"已经记过了（备忘 #{memo_id}），这次把推荐人也记上了。"
        if kind == "up":
            answer += await self._watched_from_up(str(target or "").strip())
        return answer

    async def _watched_from_up(self, target):
        """「你看过他 N 个视频」：UID 按 ``UP:`` 标签数；昵称先从看过的视频记忆里认出 UID。认不出 UID 就不说。"""
        uid = target if target.isdigit() else ""
        if not uid and target:
            wanted = target.casefold()
            known = await self.memories.find(
                types={"video"}, newest_first=True, limit=1,
                where=lambda m: str(m.get("owner_name") or "").strip().casefold() == wanted and m.get("owner_mid"))
            uid = str(known[0]["owner_mid"]) if known else ""
        if not uid:
            return ""
        watched = await self.memories.find(tag=tag(UP, uid), types={"video"})
        videos = {t for m in watched for t in (m.get("tags") or ()) if t.startswith(f"{VIDEO}:")}
        return f"你以前看过这位UP主 {len(videos)} 个视频。" if videos else "你还没看过这位UP主的视频。"
