"""过渡期的模块靠 ``HOST_API`` 向插件对象要东西：没列的名字运行时才炸，这里提前查出来。"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"


def _plugin_attributes():
    """插件对象上有的名字：main.py 和各 mixin 定义的方法、类属性、赋过值的 self 属性，装配时挂上去的。"""
    known = {"context", "config", "settings"}
    for path in [ROOT / "main.py", *CORE.glob("*.py"), *CORE.rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and path.parent == CORE or (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and path.name == "main.py"
            ):
                known.add(node.name)
            elif isinstance(node, ast.Assign) and path.parent == CORE:
                known.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Name):
                if node.value.id == "self" and (path.parent == CORE or path.name == "main.py"):
                    known.add(node.attr)
                elif node.value.id == "bot" and path == CORE / "assembly.py":
                    known.add(node.attr)  # 装配时挂到插件对象上的
            elif (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                  and isinstance(node.value, ast.Attribute) and node.value.attr == "_host"):
                known.add(node.attr)  # 分支写到插件对象上的状态
    return known


def _host_views():
    for path in CORE.rglob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ClassDef) and any(getattr(b, "id", "") == "HostView" for b in node.bases):
                yield path, node


class HostViewDependencyTests(unittest.TestCase):
    def test_every_used_name_is_declared_and_exists_on_the_plugin(self):
        plugin = _plugin_attributes()
        views = list(_host_views())
        self.assertGreater(len(views), 10)
        for path, cls in views:
            own, api = set(), set()
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    own.add(item.name)
                elif isinstance(item, ast.Assign):
                    for target in item.targets:
                        own.add(getattr(target, "id", ""))
                        if getattr(target, "id", "") == "HOST_API":
                            api = {c.value for c in ast.walk(item.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
            used, stored = set(), set()
            for node in ast.walk(cls):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
                    (stored if isinstance(node.ctx, ast.Store) else used).add(node.attr)
            with self.subTest(cls=cls.name):
                self.assertEqual(sorted(used - api - own - stored - {"_host"}), [], f"{path.name}：用了但没写进 HOST_API")
                self.assertEqual(sorted(api - plugin), [], f"{path.name}：HOST_API 里的名字插件上没有")


if __name__ == "__main__":
    unittest.main()
