"""Count direct PLAN_DIR references in Python source, shared by the allowlist guards.

A reference is an ``ast.Name`` or ``ast.Attribute`` whose identifier is
``PLAN_DIR`` or ``plan_dir``, an ``ast.arg`` / ``ast.keyword`` named
``plan_dir``, or an ``ast.alias`` importing either name (a re-export binding
such as ``from .paths import PLAN_DIR``). Comments, docstrings and string
literals (including the ``PLAN_DIR`` environment-variable name) are not
references, so prose mentions never count against a module's ceiling.
"""

import ast

_REFERENCE_NAMES = ("PLAN_DIR", "plan_dir")


def reference_counts(source: str, rel: str) -> dict[str, int]:
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        raise ValueError(f"cannot parse {rel}: {exc}") from exc
    counts = dict.fromkeys(_REFERENCE_NAMES, 0)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            ident = node.id
        elif isinstance(node, ast.Attribute):
            ident = node.attr
        elif isinstance(node, (ast.arg, ast.keyword)):
            ident = node.arg
        elif isinstance(node, ast.alias):
            ident = node.asname or node.name
        else:
            continue
        if ident in counts:
            counts[ident] += 1
    return counts


def count_references(source: str, rel: str) -> int:
    return sum(reference_counts(source, rel).values())
