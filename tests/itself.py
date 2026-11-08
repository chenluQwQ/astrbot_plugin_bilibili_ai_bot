"""假插件自己写了被搬走的方法时用：``web_search = itself``、``models = itself``。

mixin 拆掉以前 ``_web_search``、``_llm_call`` 这些直接在插件上；代码现在走 ``bot.web_search``、
``bot.models`` 这样的对象，假插件把这些名字指回自己，原来的替换写法照样生效。
"""

itself = property(lambda self: self)
