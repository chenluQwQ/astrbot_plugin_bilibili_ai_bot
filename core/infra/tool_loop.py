"""一次回复里的工具调用：次数、去重、超时、结果怎么交回模型、走过的路。不懂业务。

AstrBot 的 ``max_steps`` 数的是轮数，一轮里模型可能叫好几个工具（一个接一个跑），
所以次数在这里按「次」数：超出的调用不执行，直接告诉模型次数用完了。
工具的结果来自外部，``wrap`` 由调用方传进来（通常是不可信资料标记），这里不认识标记的格式。
"""
from __future__ import annotations

import asyncio
import json
import time

from astrbot.api import logger


class ToolMiss(Exception):
    """工具没查到东西，但知道为什么（没字幕、参数不对……）：原因原样告诉模型，不算出错。

    ``answer=False``：这句话只是给模型的提示（比如「转后台了」），不算查到的资料，不进 ``answers``。
    """

    def __init__(self, detail, status="unavailable", *, answer=True):
        super().__init__(detail)
        self.detail = str(detail)
        self.status = status
        self.answer = answer


def _brief(args, limit=200):
    """记进走过的路和日志的参数：长文字截短，免得模型塞一大段进日志。"""
    return {key: (value[:limit] if isinstance(value, str) else value) for key, value in args.items()}


class ToolRun:
    """一条消息一个，跑完看 ``trace`` 就知道查了什么、结果怎样，``answers`` 是交给模型的每条资料。"""

    def __init__(self, *, max_calls, wrap=None, limits=None, timeout=30.0, timeouts=None):
        self.max_calls = max(0, int(max_calls))
        self.limits = dict(limits or {})  # 工具名 → 这条消息里最多几次
        self.timeout = float(timeout)
        self.timeouts = dict(timeouts or {})  # 工具名 → 单独的时限（秒）
        self._wrap = wrap or (lambda text, _name: text)
        self.used = 0
        self.trace = []
        self.answers = []
        self.stopped = ""
        self._requested = set()
        self._per_tool = {}

    @property
    def exhausted(self):
        return self.used >= self.max_calls

    def stop(self, detail):
        """这一轮不再查了（比如视频转了后台）：之后的调用都只拿到 ``detail``。"""
        self.stopped = str(detail)

    def resume(self):
        self.stopped = ""

    def _refuse(self, name, args, status, detail):
        self.trace.append({"tool": name, "args": _brief(args), "status": status, "seconds": 0.0})
        return detail

    def record(self, name, args, status, text, seconds=0.0, *, answer=True):
        """记一次查询；工具在别处跑完（比如后台看完视频）也从这里记。返回交给模型的文字。"""
        args = _brief(args)
        self.trace.append({"tool": name, "args": args, "status": status, "seconds": round(seconds, 2)})
        if status == "ok":
            text = self._wrap(text, name)
        if answer:
            self.answers.append({"tool": name, "args": args, "text": text})
        return text

    async def invoke(self, name, args, impl):
        """跑一次工具，返回交给模型的文字。不抛异常：失败也只是告诉模型没查到。"""
        args = {key: value for key, value in (args or {}).items() if value not in (None, "")}
        key = name + json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
        if self.stopped:
            return self._refuse(name, args, "stopped", self.stopped)
        if self.exhausted:
            return self._refuse(name, args, "budget", "查询次数用完了，请根据已有资料作最终决定。")
        if key in self._requested:
            return self._refuse(name, args, "duplicate", "这个查询刚才已经做过，请看上面的结果，不要重复调用。")
        limit = self.limits.get(name)
        if limit is not None and self._per_tool.get(name, 0) >= limit:
            return self._refuse(name, args, "limit", "这个工具在这条消息里的次数用完了，请换别的资料或作最终决定。")
        self.used += 1
        self._requested.add(key)
        self._per_tool[name] = self._per_tool.get(name, 0) + 1
        started = time.monotonic()
        answer = True
        try:
            result = await asyncio.wait_for(impl(**args), timeout=self.timeouts.get(name, self.timeout))
            status = "ok" if result else "empty"
            text = str(result) if result else "没有查到结果。"
        except ToolMiss as miss:
            status, text, answer = miss.status, miss.detail, miss.answer
        except asyncio.TimeoutError:
            status, text = "timeout", "查询超时，没有取得可靠结果。"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[BiliBot] 工具 {name} 执行失败：{exc}")
            status, text = "failed", "查询失败，没有取得可靠结果。"
        return self.record(name, args, status, text, time.monotonic() - started, answer=answer)

    def tool(self, *, name, description, parameters, impl):
        """包成 AstrBot 的 ``FunctionTool``：AstrBot 调它时先过这里的次数和去重。"""
        from astrbot.core.agent.tool import FunctionTool

        async def handler(_event, **kwargs):
            return await self.invoke(name, kwargs, impl)

        return FunctionTool(name=name, description=description, parameters=parameters, handler=handler)

    def trace_line(self):
        """「🧭 走过的路」里用的一行：查了什么、各自结果。"""
        if not self.trace:
            return "没有查询"
        return " → ".join(f"{row['tool']}({row['status']})" for row in self.trace)
