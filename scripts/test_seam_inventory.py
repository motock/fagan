#!/usr/bin/env python3
"""Inventory of the test seams that make refactors expensive.

Measures three shortcuts (see CONTRIBUTING.md, "Test seams"):

  a) ``patch_sites``: per attribute name, test patch sites that target the
     ``pipeline.server`` module instead of the module that owns the name.
  b) ``server_reads``: per attribute name, production reads routed back
     through ``pipeline.server`` (function-level ``from .server import``,
     ``LiveRef("name")`` and ``_ServerRef("name")`` declarations).
  c) ``getsource_calls``: per test file, ``inspect.getsource`` and
     ``inspect.getsourcelines`` calls.

Uses ``ast`` only. A file that does not parse raises ``SyntaxError``; it is
never skipped, because a skipped file silently shrinks the counts.

Usage: ``test_seam_inventory.py [--json | --write]``.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "tests" / "unit" / "test_seam_baseline.json"
PRODUCTION_DIRS = ("pipeline", "app", "scripts")
SERVER_MODULE = "pipeline.server"
_REF_CLASSES = {"LiveRef", "_ServerRef"}
_GETSOURCE_NAMES = {"getsource", "getsourcelines"}
_PATCH_ATTRS = {"setattr", "delattr"}


def _parse(source: str, filename: str) -> ast.AST:
    return ast.parse(source, filename=filename)


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _server_aliases(tree: ast.AST) -> set[str]:
    """Dotted expressions that evaluate to the ``pipeline.server`` module."""
    aliases = {SERVER_MODULE}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == SERVER_MODULE and item.asname:
                    aliases.add(item.asname)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "pipeline":
            for item in node.names:
                if item.name == "server":
                    aliases.add(item.asname or "server")
    changed = True
    while changed:  # follow simple re-bindings: ``srv = p``
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _dotted(node.value) in aliases:
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in aliases:
                        aliases.add(target.id)
                        changed = True
    return aliases


def _is_patch_call(func: ast.AST) -> bool:
    if isinstance(func, ast.Attribute):
        if func.attr in _PATCH_ATTRS:
            return True
        return func.attr == "object" and _dotted(func.value) in {"patch", "mock.patch"}
    return isinstance(func, ast.Name) and func.id == "patch"


def count_patch_sites(source: str, filename: str = "<string>") -> Counter[str]:
    """Per attribute name, patch sites whose target is ``pipeline.server``."""
    tree = _parse(source, filename)
    aliases = _server_aliases(tree)
    counts: Counter[str] = Counter()
    prefix = SERVER_MODULE + "."
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_patch_call(node.func) and node.args):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if first.value.startswith(prefix):
                counts[first.value[len(prefix):].split(".")[0]] += 1
        elif (
            len(node.args) > 1
            and _dotted(first) in aliases
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            counts[node.args[1].value] += 1
    return counts


def count_server_reads(source: str, filename: str = "<string>") -> Counter[str]:
    """Per attribute name, production reads routed through ``pipeline.server``."""
    tree = _parse(source, filename)
    counts: Counter[str] = Counter()

    def visit(node: ast.AST, in_function: bool) -> None:
        if isinstance(node, ast.ImportFrom) and in_function:
            if node.module == "pipeline.server" and node.level == 0 or (
                node.module == "server" and node.level >= 1
            ):
                for item in node.names:
                    if item.name != "*":
                        counts[item.name] += 1
        elif isinstance(node, ast.Call) and node.args:
            callee = node.func
            name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
            first = node.args[0]
            if name in _REF_CLASSES and isinstance(first, ast.Constant) and isinstance(first.value, str):
                counts[first.value] += 1
        nested = in_function or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.iter_child_nodes(node):
            visit(child, nested)

    visit(tree, False)
    return counts


def count_getsource_calls(source: str, filename: str = "<string>") -> int:
    """Number of ``inspect.getsource`` / ``inspect.getsourcelines`` calls."""
    tree = _parse(source, filename)
    modules = {"inspect"}
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(i.asname for i in node.names if i.name == "inspect" and i.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "inspect" and node.level == 0:
            functions.update(
                i.asname or i.name for i in node.names if i.name in _GETSOURCE_NAMES
            )
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _GETSOURCE_NAMES:
            total += _dotted(func.value) in modules
        elif isinstance(func, ast.Name) and func.id in functions:
            total += 1
    return total


def _py_files(root: Path, repo: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts and repo in p.parents)


def measure(repo: Path = REPO_ROOT) -> dict[str, dict[str, int]]:
    patch_sites: Counter[str] = Counter()
    getsource: dict[str, int] = {}
    for path in _py_files(repo / "tests", repo):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(repo).as_posix()
        patch_sites.update(count_patch_sites(text, rel))
        calls = count_getsource_calls(text, rel)
        if calls:
            getsource[rel] = calls
    reads: Counter[str] = Counter()
    for name in PRODUCTION_DIRS:
        for path in _py_files(repo / name, repo):
            reads.update(count_server_reads(path.read_text(encoding="utf-8"), path.relative_to(repo).as_posix()))
    return {
        "patch_sites": dict(sorted(patch_sites.items())),
        "server_reads": dict(sorted(reads.items())),
        "getsource_calls": dict(sorted(getsource.items())),
    }


def _summary(data: dict[str, dict[str, int]]) -> str:
    lines = []
    for key, counts in data.items():
        lines.append(f"{key}: {len(counts)} keys, {sum(counts.values())} sites")
        for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
            lines.append(f"  {n:5d}  {name}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--json", action="store_true", help="print the three maps as JSON")
    group.add_argument("--write", action="store_true", help="rewrite the baseline file")
    args = parser.parse_args(argv)
    data = measure()
    if args.write:
        BASELINE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {BASELINE_PATH.relative_to(REPO_ROOT)}")
    elif args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(_summary(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
