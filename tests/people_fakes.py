"""假插件自己写了原 ``AffectionMixin`` 的旧名字（``_is_owner``、``_get_today_mood``、``_block_user``……）时用。

代码现在走 ``bot.roles.is_owner``、``bot.mood.today`` 这样的对象；假插件挂 ``roles = part(LegacyRoles)``，
新名字转回自己身上的旧名字，原来的写法和替换（``bot._is_owner = lambda _: False``）照样生效。
"""
from parts import part


class _Forward:
    NAMES: dict = {}

    def __init__(self, bot):
        self._bot = bot

    def __getattr__(self, name):
        if name in self.NAMES:
            return getattr(self._bot, self.NAMES[name])
        raise AttributeError(name)


class LegacyRoles(_Forward):
    NAMES = {"is_owner": "_is_owner", "always_reply": "_is_reply_whitelisted", "never_block": "_is_block_whitelisted"}


class LegacyMood(_Forward):
    NAMES = {"today": "_get_today_mood", "festival": "_get_festival_prompt", "settle": "_settle_today_mood"}


class LegacyProfiles(_Forward):
    NAMES = {
        "context": "_get_user_profile_context", "update": "_update_user_profile",
        "link_video": "_link_video_to_user_profile", "record_interaction": "_record_relationship_interaction",
    }


class LegacySecurityLog(_Forward):
    NAMES = {"record": "_log_security_event"}


class LegacyUsers(_Forward):
    NAMES = {"_block_user": "_block_user", "_unblock_user": "_unblock_user"}



class FakeProfileCandidates:
    """画像候选：假插件写了 ``_note_profile_candidate`` 就转给它（快照用来记调用），否则只记在 ``notes``。"""

    def __init__(self, bot):
        self._bot = bot
        self.notes = []

    def note(self, uid, scope, impression="", facts=(), username=""):
        hook = getattr(self._bot, "_note_profile_candidate", None)
        if hook is not None:
            return hook(uid, scope, impression, list(facts))
        self.notes.append((str(uid), scope, impression, list(facts)))
        return 0


class FallbackRoles:
    """没挂 ``roles`` 的假插件用：写了 ``_is_owner`` 就问它，没写就谁都不是主人。"""

    def __init__(self, bot):
        self._bot = bot

    def is_owner(self, uid):
        check = getattr(self._bot, "_is_owner", None)
        return bool(check(uid)) if check is not None else False
