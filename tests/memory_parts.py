"""原 ``MemoryMixin`` 的样子：测试里的假插件继承 ``MemoryParts``，就挂上记忆的几个对象。

真插件在 ``core/assembly.py`` 里建好 ``memory_entry``、``memories``、``seen_videos``、``memory_startup``、
``memory_writer``、``memory_recall``、``memory_compress``、``reply_context``；假插件没走装配，第一次用到时现建。
假插件类上写了同名方法的（``_save_self_memory_record`` 这类），建对象时挂到对象上（见 ``parts.preferring``）；
看过视频和压缩搬过去改了名，旧名写在 ``SEEN_ALIASES``、``COMPRESS_ALIASES``。

``_memory`` 留成转到 ``memories.records`` 的属性：测试里大量 ``bot._memory = [...]``，不逐个改。
"""
from functools import lru_cache
from importlib import import_module

from base_parts import base_parts
from parts import part, preferring

SEEN_ALIASES = {
    "_seen_video_bvids": "bvids",
    "_has_seen_video": "has_seen",
    "_seen_video_record": "record",
    "_mark_video_seen": "mark",
}
COMPRESS_ALIASES = {
    "_compress_oid_memory": "compress_oid",
    "_compress_thread_memory": "compress_thread",
    "_compress_user_memory": "compress_user",
}


@lru_cache(maxsize=None)
def memory_parts(package="core"):
    def lazy(module, name):
        # 有的测试在单独的包名下只加载几个模块，用到时才导入
        load = lambda: getattr(import_module(f"{package}.{module}"), name)  # noqa: E731
        load.lazy = True
        return load

    def build_memories(bot):
        return getattr(import_module(f"{package}.domain.memory"), "memories_for")(bot)

    class MemoryParts(base_parts(package)):
        memory_entry = preferring(lazy("domain._memory_entry", "MemoryEntry"))
        memories = part(build_memories)
        seen_videos = preferring(lazy("domain.seen_videos", "SeenVideos"), aliases=SEEN_ALIASES)
        memory_startup = preferring(lazy("features.memory_startup", "MemoryStartup"))
        memory_writer = preferring(lazy("features.memory_writer", "MemoryWriter"))
        memory_recall = preferring(lazy("features.memory_recall", "MemoryRecall"))
        memory_compress = preferring(lazy("features.memory_compress", "MemoryCompress"), aliases=COMPRESS_ALIASES)
        reply_context = preferring(lazy("features.reply_context", "ReplyContext"))

        @property
        def _memory(self):
            return self.memories.records

        @_memory.setter
        def _memory(self, records):
            self.memories.records = records

    return MemoryParts


def __getattr__(name):
    # 用到时才导入插件代码：导入本文件时假的 astrbot 可能还没装上
    if name == "MemoryParts":
        return memory_parts()
    raise AttributeError(name)
