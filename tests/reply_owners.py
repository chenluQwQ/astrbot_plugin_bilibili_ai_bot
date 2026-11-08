"""测试里的假插件挂上回复相关的数据主人（含私信收件箱）；读写走假插件自己的 ``_load_json`` / ``_save_json``。"""
from core.config import (
    PRIVATE_MESSAGE_STATE_FILE, REPLIED_AT_FILE, REPLIED_CONTENT_KEYS_FILE, REPLIED_FILE, REPLY_LOG_FILE,
)
from core.domain.private_inbox import PrivateInbox
from core.domain.replied_comments import RepliedComments
from core.domain.replied_content import RepliedContent
from core.domain.reply_log import ReplyLog
from core.storage.processed_keys import ContentEntries, KeySet


class HostFile:
    """和 ``JsonFile`` 一样的读写接口，背后是假插件的 json 读写。"""

    def __init__(self, bot, path):
        self.bot = bot
        self.path = path

    def read(self, default=None):
        return self.bot.json_store._load_json(self.path, default)

    def write(self, data):
        self.bot.json_store._save_json(self.path, data)

    def update(self, change, default=None):
        data = self.read(default)
        result = change(data)
        result = data if result is None else result
        self.write(result)
        return result


def attach_reply_owners(bot, paths=None):
    """``paths``：测试自己改过文件路径时传进来（键是 config 里的常量名）。"""
    paths = {"REPLY_LOG_FILE": REPLY_LOG_FILE, "REPLIED_FILE": REPLIED_FILE, "REPLIED_AT_FILE": REPLIED_AT_FILE,
             "REPLIED_CONTENT_KEYS_FILE": REPLIED_CONTENT_KEYS_FILE,
             "PRIVATE_MESSAGE_STATE_FILE": PRIVATE_MESSAGE_STATE_FILE, **(paths or {})}
    from activity_parts import host_activity_log
    bot.reply_log = ReplyLog(host_activity_log(bot, "reply", paths["REPLY_LOG_FILE"]))
    bot.replied_comments = RepliedComments(
        KeySet("comment", file=HostFile(bot, paths["REPLIED_FILE"])), KeySet("at", file=HostFile(bot, paths["REPLIED_AT_FILE"])),
    )
    bot.replied_content = RepliedContent(ContentEntries(file=HostFile(bot, paths["REPLIED_CONTENT_KEYS_FILE"])))
    bot.private_inbox = PrivateInbox(HostFile(bot, paths["PRIVATE_MESSAGE_STATE_FILE"]))
    return bot
