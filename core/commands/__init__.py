"""QQ 端 ``/bili…`` 命令的实现。

AstrBot 只认插件主模块里带装饰器的方法，所以 ``main.py`` 留着装饰器（命令名、管理员限制、
说明文字），方法体只有一句：把 ``BotApp`` 和事件交给这里对应的函数。每个函数是异步生成器，
产出要发回去的消息。

按组分在 ``account``（登录、启停、绑定）、``overview``（状态、计划、分区、帮助）、
``activities``（手动触发和解析）、``memory``、``people``（好感、黑名单）、``persona``（性格、
兴趣）、``logs``、``upkeep``（开关、清理）、``qq_context``（QQ 对话前注入B站侧上下文）。
和 WebUI、工具一样，只经 ``BotApp``（``core/app.py``）的公开方法用插件。
"""
from . import account, activities, logs, memory, overview, people, persona, qq_context, upkeep  # noqa: F401
