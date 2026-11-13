"""Count direct PLAN_DIR references in Python source, shared by the allowlist guards.

A reference is an ``ast.Name`` or ``ast.Attribute`` whose identifier is
``PLAN_DIR`` or ``plan_dir``, or an ``ast.arg`` / ``ast.keyword`` named
``plan_dir``. Comments, docstrings and string literals (including the
``PLAN_DIR`` environment-variable name) are not references, so prose
mentions never count against a module's ceiling.
"""

import ast

_REFERENCE_NAMES = frozenset({"PLAN_DIR", "plan_dir"})


def count_references(source: str, rel: str) -> int:
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        raise ValueError(f"cannot parse {rel}: {exc}") from exc
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            ident = node.id
        elif isinstance(node, ast.Attribute):
            ident = node.attr
        elif isinstance(node, (ast.arg, ast.keyword)):
            ident = node.arg
        else:
            continue
        if ident in _REFERENCE_NAMES:
            count += 1
    return count
