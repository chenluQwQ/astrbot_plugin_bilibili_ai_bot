"""测试里的假插件要挂 ``dynamic_post`` 这类长期对象时用：``dynamic_post = part(DynamicPost)``。

真插件在 ``core/assembly.py`` 里一次建好；假插件没走装配，第一次用到时才建（``DynamicPost(bot)``），
之后缓存在实例上。测试也可以直接 ``bot.dynamic_post = 假的`` 覆盖。
"""


class part:
    def __init__(self, cls):
        self._cls = cls

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, bot, owner=None):
        if bot is None:
            return self
        built = bot.__dict__[self._name] = self._cls(bot)
        return built


def preferring(cls, *, inherited=False, aliases=None):
    """像 ``part(cls)``，但假插件类上写了 ``cls`` 也有的同名方法时，建好后挂到对象上替掉。

    原来这些方法在 mixin 上，假插件类上写一个同名方法就能替掉；现在搬到对象上，用这个保住原来的写法。
    ``cls`` 也可以是带 ``lazy = True`` 的无参函数，第一次用到时才导入类。``inherited=True`` 时父类上的方法也算
    （给只为了收下 ``bot`` 参数而包了一层的类用）。搬过去改了名的写在 ``aliases``（旧名 → 对象上的新名），
    假插件类上写的旧名方法挂到新名上。
    """
    aliases = dict(aliases or {})

    def build(bot):
        real = cls() if getattr(cls, "lazy", False) else cls
        built = real(bot)
        own = set().union(*(vars(k) for k in real.__mro__[:-1])) if inherited else vars(real)
        for klass in type(bot).__mro__:
            if klass.__module__.split(".")[0] in {"core", "builtins"} or klass.__module__.endswith("_parts"):
                continue
            for attr in vars(klass):
                target = aliases.get(attr, attr)
                if not attr.startswith("__") and target in own and target not in vars(built):
                    setattr(built, target, getattr(bot, attr))
        return built

    return part(build)
