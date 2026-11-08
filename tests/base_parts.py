"""原 ``UtilsMixin`` 的样子：测试里的假插件继承 ``BaseParts``，就挂上 json 读写、B站请求、子进程这些底层对象。

真插件在 ``core/assembly.py`` 里建好 ``json_store``、``bili_http``、``processes``、``environment``、``cache_files``、``activity_logs``；
假插件没走装配，第一次用到时现建。假插件类上写了同名方法的（``_load_json``、``_run_process`` 这类），
建对象时挂到对象上（见 ``parts.preferring``）。``BiliParts`` 继承它，大多数测试辅助就都带上了。
"""
from functools import lru_cache
from importlib import import_module

from parts import part, preferring

# 原来 mixin 上的名字 → 现在在哪个对象上
OWNERS = {
    "_load_json": "json_store",
    "_save_json": "json_store",
    "_append_json_list": "json_store",
    "_has_cookie": "bili_http",
    "_headers": "bili_http",
    "_http_get": "bili_http",
    "_http_post": "bili_http",
    "_http_get_text": "bili_http",
    "_find_command": "processes",
    "_run_process": "processes",
    "_get_environment_status": "environment",
    "_log_environment_warnings": "environment",
    "_ensure_data_dir": "cache_files",
    "_cache_stats": "cache_files",
    "_purge_cache": "cache_files",
    "_cleanup_temp_files": "cache_files",
}


@lru_cache(maxsize=None)
def base_parts(package="core"):
    def lazy(module, name):
        # 有的测试在单独的包名下只加载几个模块，用到时才导入
        def load():
            real = getattr(import_module(f"{package}.{module}"), name)
            if name in {"JsonStore", "Processes"}:
                # 这两个没有构造参数，包一层收下 bot
                return type(name, (real,), {"__init__": lambda self, bot: None})
            return real
        load.lazy = True
        return load

    class BaseParts:
        settings = getattr(import_module(f"{package}.settings"), "SettingsAccessor")()
        json_store = preferring(lazy("storage.json_store", "JsonStore"), inherited=True)
        bili_http = preferring(lazy("infra.bili.http", "BiliHttp"))
        processes = preferring(lazy("infra.process", "Processes"), inherited=True)
        environment = preferring(lazy("infra.environment", "Environment"))
        cache_files = preferring(lazy("infra.cache_files", "CacheFiles"))
        activity_logs = part(lambda bot: _host_activity_logs(bot, package))
        processed_keys = part(lambda bot: _host_processed_keys(bot, package))
        people = part(lambda bot: _host_people(bot, package))
        memory_docs = part(lambda bot: _host_memory_docs(bot, package))
        states = part(lambda bot: _host_states(bot, package))

    return BaseParts


def __getattr__(name):
    # 用到时才导入插件代码：导入本文件时假的 astrbot 可能还没装上
    if name == "BaseParts":
        return base_parts()
    raise AttributeError(name)


def _host_activity_logs(bot, package="core"):
    # 用到时才导入：导入本文件时假的 astrbot 可能还没装上
    from activity_parts import HostActivityLogs
    return HostActivityLogs(bot, package)


def _host_processed_keys(bot, package="core"):
    from activity_parts import HostProcessedKeys
    return HostProcessedKeys(bot, package)


def _host_people(bot, package="core"):
    from activity_parts import HostPeople
    return HostPeople(bot, package)


def _host_memory_docs(bot, package="core"):
    from activity_parts import HostMemoryDocs
    return HostMemoryDocs(bot, package)


def _host_states(bot, package="core"):
    from activity_parts import HostStates
    return HostStates(bot, package)


def attach_base(fake):
    """``SimpleNamespace`` 做的假插件：把直接挂在上面的旧名字（``_load_json`` 这些）分到对应的对象上。"""
    from types import SimpleNamespace
    for owner in set(OWNERS.values()):
        if owner not in vars(fake):
            setattr(fake, owner, SimpleNamespace())
    for name, owner in OWNERS.items():
        if name in vars(fake):
            setattr(getattr(fake, owner), name, vars(fake)[name])
    if "activity_logs" not in vars(fake):
        fake.activity_logs = _host_activity_logs(fake)
    if "processed_keys" not in vars(fake):
        fake.processed_keys = _host_processed_keys(fake)
    if "people" not in vars(fake):
        fake.people = _host_people(fake)
    if "memory_docs" not in vars(fake):
        fake.memory_docs = _host_memory_docs(fake)
    if "states" not in vars(fake):
        fake.states = _host_states(fake)
    return fake
