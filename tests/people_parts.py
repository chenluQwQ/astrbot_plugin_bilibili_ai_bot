"""原 ``AffectionMixin`` 的样子：测试里的假插件继承 ``PeopleParts``，就挂上真的身份判断、输入检查和用户接口。

真插件在 ``core/assembly.py`` 里建好 ``roles``、``inbound_guard``、``bili_users``；假插件没走装配，
第一次用到时现建（见 ``parts.py``）。好感、画像、心情、安全日志原来 mixin 上也只是转发，假插件照旧自己挂。
假插件自己写了 ``_is_owner``、``_get_today_mood`` 这些旧名字的，用 ``people_fakes.py``。
"""
from parts import part


def _roles(bot):
    from core.security.roles import Roles
    return Roles(lambda: bot.settings)


def _inbound_guard(bot):
    from core.security.inbound import InboundGuard
    return InboundGuard(bot.security_log)


def _bili_users(bot):
    from core.infra.bili.users import BiliUsers
    return BiliUsers(bot)


class PeopleParts:
    roles = part(_roles)
    inbound_guard = part(_inbound_guard)
    bili_users = part(_bili_users)
