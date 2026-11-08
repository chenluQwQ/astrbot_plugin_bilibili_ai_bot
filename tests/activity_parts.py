"""测试里的假插件挂活动日志和去重账本（``BaseParts`` 和 ``attach_base`` 已经挂上，一般不用自己写）。

真插件的活动日志在 sqlite 里（``core/storage/activity_log.py``）；假插件这边每种日志走 json 那条退路，
读写落到假插件自己的 ``json_store._load_json`` / ``_save_json``，键还是原来的文件路径，原来按文件断言的测试照旧。
路径每次现查 ``config``，测试改了那边的常量也跟着走。
"""
from importlib import import_module

# 种类 → config 里的常量名
PATH_NAMES = {
    "watch": "WATCH_LOG_FILE", "reply": "REPLY_LOG_FILE", "proactive": "PROACTIVE_LOG_FILE",
    "dynamic": "DYNAMIC_LOG_FILE", "bangumi": "BANGUMI_WATCH_LOG_FILE", "dynamic_watch": "DYNAMIC_WATCH_LOG_FILE",
    "security": "SECURITY_LOG_FILE", "trigger": "PROACTIVE_TRIGGER_LOG_FILE",
    "comment_context": "COMMENT_CONTEXT_LOG_FILE", "video_inspection": "VIDEO_INSPECTION_LOG_FILE",
}


class _ConfigPath:
    """``HostFile`` 那样读写假插件的 json，路径每次现查 config。"""

    def __init__(self, bot, config, name):
        self.bot = bot
        self._config = config
        self._name = name

    def read(self, default=None):
        return self.bot.json_store._load_json(getattr(self._config, self._name), default)

    @property
    def path(self):
        return getattr(self._config, self._name)

    def write(self, data):
        self.bot.json_store._save_json(self.path, data)

    def locked(self):
        return _NO_LOCK

    def update(self, change, default=None):
        data = self.read(default)
        result = change(data)
        result = data if result is None else result
        self.write(result)
        return result


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NO_LOCK = _NoLock()


def host_activity_log(bot, kind, path=None, package="core"):
    """一种日志；``path`` 给了就用它（测试自己挪过文件时）。"""
    # 用到时才导入：导入本文件时假的 astrbot 可能还没装上
    from reply_owners import HostFile
    log_class = import_module(f"{package}.storage.activity_log").ActivityLog
    file = HostFile(bot, path) if path else _ConfigPath(bot, import_module(f"{package}.config"), PATH_NAMES[kind])
    return log_class(kind, file=file)


class HostActivityLogs:
    def __init__(self, bot, package="core"):
        for kind in PATH_NAMES:
            setattr(self, kind, host_activity_log(bot, kind, package=package))

    def prune(self, now=None):
        return {}

    def export_json(self):
        return []


# 去重账本：种类 → config 里的常量名
KEY_PATH_NAMES = {
    "comment": "REPLIED_FILE", "at": "REPLIED_AT_FILE", "commented_video": "COMMENTED_FILE",
    "content": "REPLIED_CONTENT_KEYS_FILE",
}


class HostProcessedKeys:
    """去重账本走 json 退路，落到假插件自己的 json 读写，路径同样每次现查 config。"""

    def __init__(self, bot, package="core"):
        module = import_module(f"{package}.storage.processed_keys")
        config = import_module(f"{package}.config")
        for kind, name in KEY_PATH_NAMES.items():
            file = _ConfigPath(bot, config, name)
            part = module.ContentEntries(file=file) if kind == "content" else module.KeySet(kind, file=file)
            setattr(self, kind, part)

    def prune(self):
        return {}

    def export_json(self):
        return []


# 人和关系：种类 → config 里的常量名
PEOPLE_PATH_NAMES = {
    "affection": "AFFECTION_FILE", "user_profiles": "USER_PROFILE_FILE", "milestones": "MILESTONE_FILE",
    "profile_reached": "PROFILE_REACHED_FILE", "profile_candidates": "PROFILE_CANDIDATES_FILE",
    "qq_bindings": "BINDING_FILE",
}


class HostPeople:
    """人和关系走 json 退路（``KeyedDoc`` 没接数据库时就是原来的文件），落到假插件自己的 json 读写。"""

    PATH_NAMES = PEOPLE_PATH_NAMES

    def __init__(self, bot, package="core"):
        module = import_module(f"{package}.storage.keyed_json")
        config = import_module(f"{package}.config")
        for kind, name in self.PATH_NAMES.items():
            setattr(self, kind, module.KeyedDoc(kind, _ConfigPath(bot, config, name)))

    def connect(self, db_path):
        raise RuntimeError("测试里的假插件不接数据库")

    def close(self):
        pass

    def export_json(self):
        return []


MEMORY_DOC_PATH_NAMES = {
    "bangumi_memory": "BANGUMI_MEMORY_FILE", "video_memory": "VIDEO_MEMORY_FILE", "external_memory": "EXTERNAL_MEMORY_FILE",
}


class HostMemoryDocs(HostPeople):
    """番剧进度、视频记忆、外部记忆，同 ``HostPeople``。"""

    PATH_NAMES = MEMORY_DOC_PATH_NAMES


# 小状态：种类 → config 里的常量名（和 core/config.py 的 STATE_FILE_NAMES 一样；有的测试用自己的假 config，没有那张表）
STATE_PATH_NAMES = {
    "schedule": "SCHEDULE_FILE", "dynamic_schedule": "DYNAMIC_SCHEDULE_FILE", "bangumi_schedule": "BANGUMI_SCHEDULE_FILE",
    "special_follow_schedule": "SPECIAL_FOLLOW_SCHEDULE_FILE", "dynamic_watch_schedule": "DYNAMIC_WATCH_SCHEDULE_FILE",
    "daily_plan": "AUTONOMOUS_PLAN_FILE", "mood": "MOOD_FILE", "personality": "PERSONALITY_FILE",
    "permanent_memory": "PERMANENT_MEMORY_FILE", "memory_sync": "MEMORY_SYNC_STATE_FILE",
    "private_messages": "PRIVATE_MESSAGE_STATE_FILE", "consolidation": "CONSOLIDATION_STATE_FILE",
    "weekly_summary": "WEEKLY_SUMMARY_FILE", "daily_summary": "DAILY_SUMMARY_FILE", "up_skip": "UP_SKIP_FILE",
    "tell_owner": "TELL_OWNER_FILE", "owner_feedback": "OWNER_FEEDBACK_FILE",
    "owner_recommend": "OWNER_RECOMMEND_LOG_FILE", "creator_activity": "CREATOR_ACTIVITY_FILE",
    "block_log": "BLOCK_LOG_FILE", "block_count": "BLOCK_COUNT_FILE",
}


class HostStates:
    """小状态走 json 退路（``WholeDoc`` 没接数据库时就是原来的文件），落到假插件自己的 json 读写。"""

    def __init__(self, bot, package="core"):
        module = import_module(f"{package}.storage.keyed_json")
        config = import_module(f"{package}.config")
        for kind, name in STATE_PATH_NAMES.items():
            setattr(self, kind, module.WholeDoc(kind, _ConfigPath(bot, config, name)))

    def connect(self, db_path):
        raise RuntimeError("测试里的假插件不接数据库")

    def close(self):
        pass

    def export_json(self):
        return []


class ListLog:
    """只活在内存里的一种日志，测试直接看 ``rows``。"""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []

    def recent(self):
        return list(self.rows)

    def append(self, entry):
        self.rows.append(entry)
        return self.recent()

    def on_day(self, day):
        return [row for row in self.rows if str(row.get("time", "")).startswith(day)]

    def days(self):
        return {str(row.get("time", ""))[:10] for row in self.rows}
