"""Keep tool interfaces importable before test modules install partial AstrBot stubs.

Several legacy tests replace ``sys.modules['astrbot']`` during collection.  The
real tool submodules are therefore preloaded first, while the top-level package
and ``astrbot.api`` are replaced by a logger-only fallback so each test still
controls its own API stub and temporary data directory.
"""

import importlib
import sys
import types


_TOOL_MODULES = (
    "astrbot.core.agent.tool",
    "astrbot.core.astr_agent_context",
    "astrbot.core.agent.run_context",
)


def _install_tool_stubs():
    for name in list(sys.modules):
        if name == "astrbot" or name.startswith("astrbot."):
            sys.modules.pop(name, None)

    packages = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.agent": types.ModuleType("astrbot.core.agent"),
    }
    for name, module in packages.items():
        module.__path__ = []
        sys.modules[name] = module

    tool = types.ModuleType("astrbot.core.agent.tool")

    class FunctionTool:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    tool.FunctionTool = FunctionTool
    tool.ToolExecResult = str

    context = types.ModuleType("astrbot.core.astr_agent_context")
    context.AstrAgentContext = type("AstrAgentContext", (), {})
    run_context = types.ModuleType("astrbot.core.agent.run_context")

    class ContextWrapper:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    run_context.ContextWrapper = ContextWrapper
    sys.modules[tool.__name__] = tool
    sys.modules[context.__name__] = context
    sys.modules[run_context.__name__] = run_context


try:
    for _name in _TOOL_MODULES:
        importlib.import_module(_name)
except ModuleNotFoundError as exc:
    if not str(exc.name or "").startswith("astrbot"):
        raise
    _install_tool_stubs()
finally:
    for _name in list(sys.modules):
        if _name == "astrbot" or _name.startswith("astrbot.api"):
            sys.modules.pop(_name, None)


def _install_logger_fallback():
    """Plugin modules import ``logger`` from ``astrbot.api``; tests that need more replace these."""
    import logging

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    if not hasattr(astrbot, "__path__"):
        astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api._logger_only = True
    api.logger = logging.getLogger("astrbot")
    sys.modules["astrbot.api"] = api
    astrbot.api = api


_install_logger_fallback()
