"""Dependency rules: branches don't call each other, domain modules don't call each other, everything hands off through the trunk.

| 谁 | 不可以 import |
| --- | --- |
| lifecycle/（主干） | 除 lifecycle、storage、settings 以外的任何 core 模块 |
| features/（分支） | 别的分支、主干（handoffs 除外）、mixin |
| domain/（状态和能力） | 别的 domain 模块、分支、主干、mixin |
| infra/、security/、storage/ | 分支、主干、domain、mixin |

Names starting with ``_`` inside features/ or domain/ are shared helpers
(value types, common code), not branches or modules of their own, and may be
imported by their siblings.

All mixins are gone (8-3c); ``test_no_mixin_is_left`` keeps it that way, and
the call-tangle budget in ``test_old_code_tangle_does_not_grow`` stays at zero.
"""

import ast
import importlib.util
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"

# Lower these as the tangle shrinks; never raise them.
MAX_MODULES_IN_CALL_CYCLES = 0
MAX_MUTUAL_CALL_PAIRS = 0


def mixin_modules():
    """Top-level core modules whose classes are mixed into the plugin object."""
    names = set()
    for path in CORE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.ClassDef) and node.name.endswith("Mixin")
            for node in tree.body
        ):
            names.add(path.stem)
    return names


def imported_core_modules(path):
    """Every import in ``path`` as a tuple of names below ``core``.

    ``from ..storage.json_file import JsonFile`` gives both
    ``("storage", "json_file")`` and ``("storage", "json_file", "JsonFile")``.
    """
    package = path.relative_to(ROOT).with_suffix("").parts[:-1]
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(package[: len(package) - node.level + 1])
            else:
                base = []
            module = base + (node.module.split(".") if node.module else [])
            candidates = [module] + [module + [alias.name] for alias in node.names]
        elif isinstance(node, ast.Import):
            candidates = [alias.name.split(".") for alias in node.names]
        else:
            continue
        for parts in candidates:
            if len(parts) >= 2 and parts[0] == "core":
                found.add(tuple(parts[1:]))
    return found


def is_shared_helper(name):
    return name.startswith("_")


class ImportDirectionTests(unittest.TestCase):
    def setUp(self):
        self.mixins = mixin_modules()

    def test_no_mixin_is_left(self):
        # 8-3c 把 mixin 全拆完了；别再加回来，新代码按上面的分层放。
        self.assertEqual(self.mixins, set())

    def files(self, layer):
        return sorted((CORE / layer).rglob("*.py"))

    def check(self, layer, forbidden):
        for path in self.files(layer):
            own = path.relative_to(CORE).with_suffix("").parts
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                bad = sorted(".".join(target) for target in imported_core_modules(path) if forbidden(own, target))
                self.assertEqual(bad, [])

    def test_lifecycle_only_uses_itself_storage_and_settings(self):
        self.check("lifecycle", lambda own, target: target[0] not in {"lifecycle", "storage", "settings"})

    def test_features_do_not_use_each_other_or_the_backbone(self):
        def forbidden(own, target):
            if target[0] in self.mixins:
                return True
            if target[0] == "lifecycle":
                return target[:2] != ("lifecycle", "handoffs")
            if target[0] == "features" and len(target) >= 2 and own[0] == "features":
                return target[1] not in {own[1], "__init__"} and not is_shared_helper(target[1])
            return False
        self.check("features", forbidden)

    def test_domain_modules_do_not_use_each_other(self):
        def forbidden(own, target):
            if target[0] in self.mixins or target[0] in {"features", "lifecycle"}:
                return True
            if target[0] == "domain" and len(target) >= 2:
                return target[1] not in {own[1], "__init__"} and not is_shared_helper(target[1])
            return False
        self.check("domain", forbidden)

    def test_lower_layers_know_no_business_code(self):
        for layer in ("infra", "security", "storage"):
            self.check(layer, lambda own, target: target[0] in self.mixins or target[0] in {"domain", "features", "lifecycle"})


class CallCycleBudgetTests(unittest.TestCase):
    def test_old_code_tangle_does_not_grow(self):
        spec = importlib.util.spec_from_file_location("coupling_report", ROOT / "scripts" / "coupling_report.py")
        report = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(report)
        totals = report.collect()["totals"]
        self.assertLessEqual(totals["modules_in_call_cycles"], MAX_MODULES_IN_CALL_CYCLES)
        self.assertLessEqual(totals["mutual_call_pairs"], MAX_MUTUAL_CALL_PAIRS)


class PublicSurfaceTests(unittest.TestCase):
    """WebUI、工具、QQ 命令只经 ``core/app.py`` 用插件（第 8 步）；直接碰的只有配置（``config``、``settings``）。"""

    PRIVATE_ACCESS = re.compile(
        r"""\bplugin(?:_instance)?\.(?!(?:config|settings)\b)\w|(?:get|has)attr\(\s*(?:plugin(?:_instance)?|app)\s*,|\bapp\._"""
    )

    def test_webui_tools_and_commands_use_only_the_app(self):
        paths = [path for layer in ("tools", "web", "commands") for path in (CORE / layer).glob("*.py")]
        self.assertTrue(paths, "core/tools/、core/web/、core/commands/ 都没找到")
        for path in paths:
            name = path.relative_to(CORE).as_posix()
            text = path.read_text(encoding="utf-8")
            hits = [
                f"{name}:{number}: {line.strip()}"
                for number, line in enumerate(text.splitlines(), 1)
                if self.PRIVATE_ACCESS.search(line)
            ]
            with self.subTest(file=name):
                self.assertEqual(hits, [], "改用 app_of(plugin) 上的方法")


if __name__ == "__main__":
    unittest.main()
