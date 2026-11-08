"""每个模块的依赖清单：插件对象上「只看得见一部分」。

模块继承它，把要用的插件上的东西（``models``、``json_store`` 这些对象）列在 ``HOST_API``：这张表就是
它对外面的全部依赖，没列的名字一律 AttributeError，``tests/test_host_view_dependencies.py`` 逐个检查
表里的名字在插件上真有。没改成构造时传参：那样要动 56 个类和几乎所有测试，耦合也不会更低。
"""
from __future__ import annotations


class HostView:
    HOST_API: frozenset = frozenset()

    def __init__(self, host):
        self._host = host

    def __getattr__(self, name):
        if name in self.HOST_API:
            return getattr(self._host, name)
        raise AttributeError(name)
