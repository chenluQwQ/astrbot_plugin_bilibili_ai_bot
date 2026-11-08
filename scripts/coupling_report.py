"""Print the coupling numbers tracked by docs/refactor-steps.md.

Run from the repository root:

    python scripts/coupling_report.py          # human-readable table
    python scripts/coupling_report.py --json   # machine-readable totals

Every refactor PR pastes this output into its description; the totals must
never go up.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("core",)
EXTRA_FILES = ("main.py",)
PLUGIN_CLASS = "BiliBiliBot"

HASATTR_SELF = re.compile(r"\bhasattr\(\s*self\s*,")
GETATTR_SELF_PRIVATE = re.compile(r"""\bgetattr\(\s*self\s*,\s*["']_""")
CONFIG_GET = re.compile(r"""\bconfig\.get\(\s*["'][A-Z0-9_]+["']""")
JSON_IO = re.compile(r"\bself\._(?:load|save)_json\(")
PLUGIN_PRIVATE = re.compile(r"\bplugin\._[A-Za-z]\w*")


def source_files() -> list[Path]:
    files = [ROOT / name for name in EXTRA_FILES if (ROOT / name).exists()]
    for directory in SOURCE_DIRS:
        files.extend(
            path for path in sorted((ROOT / directory).rglob("*.py"))
            if "__pycache__" not in path.parts
        )
    return files


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def mixin_classes(trees: dict[str, ast.Module]) -> dict[str, list[ast.ClassDef]]:
    """Classes whose methods end up on the plugin object."""
    found: dict[str, list[ast.ClassDef]] = defaultdict(list)
    for name, tree in trees.items():
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and (
                node.name.endswith("Mixin") or node.name == PLUGIN_CLASS
            ):
                found[name].append(node)
    return found


def plugin_mixin_count(trees: dict[str, ast.Module]) -> int:
    for tree in trees.values():
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == PLUGIN_CLASS:
                return sum(
                    1 for base in node.bases
                    if isinstance(base, ast.Name) and base.id.endswith("Mixin")
                )
    return 0


def cross_module_calls(trees: dict[str, ast.Module]) -> tuple[Counter, Counter, dict]:
    """Count ``self.method`` uses that resolve to a method in another file."""
    classes = mixin_classes(trees)
    owner: dict[str, str] = {}
    for name, nodes in classes.items():
        for cls in nodes:
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner.setdefault(item.name, name)
    outgoing: Counter = Counter()
    incoming: Counter = Counter()
    deps: dict[str, set[str]] = defaultdict(set)
    for name in classes:
        for node in ast.walk(trees[name]):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in owner
                and owner[node.attr] != name
            ):
                outgoing[name] += 1
                incoming[owner[node.attr]] += 1
                deps[name].add(owner[node.attr])
    return outgoing, incoming, deps


def tangles(deps: dict[str, set[str]]) -> list[list[str]]:
    """Groups of modules that reach each other through calls (strongly connected, size > 1)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    groups: list[list[str]] = []
    counter = [0]

    def visit(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for target in deps.get(node, ()):
            if target not in index:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], index[target])
        if low[node] == index[node]:
            group = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                group.append(member)
                if member == node:
                    break
            if len(group) > 1:
                groups.append(sorted(group))

    for node in sorted(set(deps) | {t for targets in deps.values() for t in targets}):
        if node not in index:
            visit(node)
    return sorted(groups, key=len, reverse=True)


def data_file_owners(sources: dict[str, str]) -> dict[str, list[str]]:
    """Which modules mention each ``*_FILE`` constant from core/config.py."""
    config = sources.get("core/config.py", "")
    constants = sorted(set(re.findall(r"^([A-Z_]+_FILE)\s*=", config, re.M)))
    owners = {}
    for constant in constants:
        pattern = re.compile(rf"\b{constant}\b")
        owners[constant] = sorted(
            name for name, text in sources.items()
            if name != "core/config.py" and pattern.search(text)
        )
    return owners


def collect() -> dict:
    sources = {rel(path): path.read_text(encoding="utf-8") for path in source_files()}
    trees = {name: ast.parse(text) for name, text in sources.items()}
    outgoing, incoming, deps = cross_module_calls(trees)
    owners = data_file_owners(sources)
    groups = tangles(deps)
    mutual = sorted(
        {tuple(sorted((a, b))) for a, targets in deps.items() for b in targets if a in deps.get(b, ())}
    )
    per_file = {
        name: {
            "calls_out": outgoing[name],
            "called_by_others": incoming[name],
            "depends_on": len(deps[name]),
            "hasattr_self": len(HASATTR_SELF.findall(text)),
            "getattr_self_private": len(GETATTR_SELF_PRIVATE.findall(text)),
            "config_get": len(CONFIG_GET.findall(text)),
            "json_io": len(JSON_IO.findall(text)),
            "lines": text.count("\n") + 1,
        }
        for name, text in sources.items()
    }
    config_keys = set()
    for text in sources.values():
        config_keys.update(re.findall(r"""\bconfig\.get\(\s*["']([A-Z0-9_]+)["']""", text))
    plugin_private = set()
    for name, text in sources.items():
        if name.startswith(("core/web/", "core/tools/")):
            plugin_private.update(PLUGIN_PRIVATE.findall(text))
    totals = {
        "plugin_mixins": plugin_mixin_count(trees),
        "cross_module_calls": sum(outgoing.values()),
        "hasattr_self": sum(item["hasattr_self"] for item in per_file.values()),
        "getattr_self_private": sum(item["getattr_self_private"] for item in per_file.values()),
        "config_get": sum(item["config_get"] for item in per_file.values()),
        "distinct_config_keys": len(config_keys),
        "json_io": sum(item["json_io"] for item in per_file.values()),
        "webui_tools_private_access": len(plugin_private),
        "shared_data_files": sum(1 for users in owners.values() if len(users) > 1),
        "main_py_lines": per_file.get("main.py", {}).get("lines", 0),
        "modules_in_call_cycles": sum(len(group) for group in groups),
        "largest_call_cycle": max((len(group) for group in groups), default=0),
        "mutual_call_pairs": len(mutual),
    }
    return {
        "totals": totals, "files": per_file, "data_files": owners,
        "tangles": groups, "mutual": mutual, "deps": {k: sorted(v) for k, v in deps.items()},
    }


def print_table(report: dict) -> None:
    print("## 总数\n")
    print("| 指标 | 数量 |")
    print("| --- | --- |")
    for key, value in report["totals"].items():
        print(f"| {key} | {value} |")
    print("\n## 按文件（只列有跨模块调用或兜底写法的）\n")
    print("| 文件 | 调别人 | 被别人调 | 依赖文件数 | hasattr(self | getattr(self,'_ | config.get | json 读写 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    rows = sorted(
        report["files"].items(),
        key=lambda item: -(item[1]["calls_out"] + item[1]["called_by_others"]),
    )
    for name, item in rows:
        if not any(item[key] for key in ("calls_out", "called_by_others", "hasattr_self", "getattr_self_private")):
            continue
        print(
            f"| `{name}` | {item['calls_out']} | {item['called_by_others']} | {item['depends_on']} "
            f"| {item['hasattr_self']} | {item['getattr_self_private']} | {item['config_get']} | {item['json_io']} |"
        )
    print("\n## 互相调用、绕成圈的模块\n")
    for group in report["tangles"]:
        print(f"- {len(group)} 个：" + "、".join(f"`{name}`" for name in group))
    print(f"\n两两互相调用的：{len(report['mutual'])} 对")
    for a, b in report["mutual"]:
        print(f"- `{a}` ⇄ `{b}`")
    print("\n## 被多个模块读写的数据文件\n")
    for constant, users in report["data_files"].items():
        if len(users) > 1:
            print(f"- `{constant}`：{', '.join(users)}")


def main(argv: list[str]) -> int:
    report = collect()
    if "--json" in argv:
        print(json.dumps(report["totals"], ensure_ascii=False, indent=2))
    else:
        print_table(report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
