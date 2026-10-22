"""Detector for frozen-pin assertions in test source.

Flags assertions that pin a cumulative artifact's exact bytes or contents
(sha256 literals, exact enumerations, exact counts), which break whenever a
later sibling story legitimately grows that artifact. Pure text analysis: no
file or directory knowledge.
"""

import ast
import re
from dataclasses import dataclass

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CUMULATIVE_NAMES = {"__all__", "EXPORTS", "TOOLS"}
_CUMULATIVE_SUFFIXES = ("_EXPORTS", "_TOOLS", "_ALL")
_EXACT_OPS = (ast.Eq, ast.NotEq)
_LITERAL_COLLECTIONS = (ast.List, ast.Tuple, ast.Set)


@dataclass(frozen=True)
class Violation:
    line: int
    rule: str
    detail: str


def _is_cumulative_name(node: ast.expr) -> bool:
    if not isinstance(node, ast.Name):
        return False
    return node.id in _CUMULATIVE_NAMES or node.id.endswith(_CUMULATIVE_SUFFIXES)


def _is_len_of_cumulative_name(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
        and len(node.args) == 1
        and _is_cumulative_name(node.args[0])
    )


def _docstring_constants(tree: ast.AST) -> set[int]:
    """Ids of docstring Constant nodes; prose is not a pin, so they are skipped."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _check_constant(node: ast.Constant, docstrings: set[int]) -> Violation | None:
    if id(node) in docstrings:
        return None
    if isinstance(node.value, str) and _SHA256_RE.fullmatch(node.value):
        return Violation(
            node.lineno, "sha256_literal", "sha256 literal pins exact bytes"
        )
    return None


def _check_compare(node: ast.Compare) -> Violation | None:
    if len(node.ops) != 1 or not isinstance(node.ops[0], _EXACT_OPS):
        return None
    right = node.comparators[0]
    if _is_cumulative_name(node.left) and isinstance(right, _LITERAL_COLLECTIONS):
        return Violation(
            node.lineno, "exact_enumeration", f"exact contents of {node.left.id}"
        )
    if _is_len_of_cumulative_name(node.left) and isinstance(right, ast.Constant):
        return Violation(
            node.lineno, "exact_count", "exact count of a cumulative artifact"
        )
    return None


def scan_source(source: str) -> list[Violation]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    docstrings = _docstring_constants(tree)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            violation = _check_constant(node, docstrings)
        elif isinstance(node, ast.Compare):
            violation = _check_compare(node)
        else:
            continue
        if violation:
            violations.append(violation)
    return sorted(violations, key=lambda v: v.line)
