"""Mechanical judge for edits to existing test files (ov-5).

``judge`` reads ``git diff base..head`` and passes an edit to an existing test
file only when every condition holds:

(i)   per surviving test function, the number of ``assert`` statements and
      ``pytest.raises``/``pytest.warns`` blocks does not decrease;
(ii)  no skip, xfail, ``importorskip`` or ``skipif`` is added;
(iii) no expected literal on a changed assertion changes unless the brief
      names that literal;
(iv)  every removed test line references an identifier that appears on a
      removed line of a non-test file in the same diff.

A deleted or renamed test function is never a pass: it yields
``deletion_of_pin`` and approval stays with a human. Every reason is one line
naming a test, a path or a line number; no file contents are quoted.
"""

from __future__ import annotations

import ast
import difflib
import re
import subprocess
from dataclasses import dataclass, field

PASS = "pass"
FAIL = "fail"
DELETION_OF_PIN = "deletion_of_pin"

_GIT_TIMEOUT_SECONDS = 120
_REF_RE = re.compile(r"^[\w./-]+$")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_SKIP_RE = re.compile(r"mark\.(?:skip|skipif|xfail)\b|\b(?:importorskip|skip)\s*\(")
_EXPECT_ATTRS = frozenset({"raises", "warns"})


@dataclass
class Verdict:
    """The judge's outcome, the reasons behind it and the files it judged."""

    outcome: str
    reasons: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    deleted_tests: list[str] = field(default_factory=list)


class _JudgeError(Exception):
    """A git read failed or a ref is not a plain ref name; the judge fails closed."""


def judge(base_ref: str, head_ref: str, repo_dir, brief: str = "") -> Verdict:
    """Judge the existing-test edits between ``base_ref`` and ``head_ref``.

    ``brief`` is the story's brief; a changed expected literal passes only when
    the brief names it. Never raises: any unexpected failure is a ``fail``.
    """
    try:
        return _judge(base_ref, head_ref, repo_dir, brief or "")
    except _JudgeError as exc:
        return Verdict(outcome=FAIL, reasons=[str(exc)])
    except Exception as exc:  # noqa: BLE001 - the judge must never raise
        return Verdict(outcome=FAIL, reasons=[f"could not judge the diff ({type(exc).__name__})"])


def _judge(base: str, head: str, repo_dir, brief: str) -> Verdict:
    changes = _changed_files(base, head, repo_dir)
    test_changes = [(status, path) for status, path in changes if _is_test_path(path) and status != "A"]
    if not test_changes:
        return Verdict(outcome=FAIL, reasons=["no existing test file changed"])

    removed_names = _removed_names_outside_tests(base, head, repo_dir, changes)
    reasons: list[str] = []
    deleted: list[str] = []
    test_paths = sorted(path for _, path in test_changes)
    for status, path in sorted(test_changes, key=lambda change: change[1]):
        deleted.extend(_judge_test_file(base, head, repo_dir, path, status, removed_names, brief, reasons))

    deleted = sorted(set(deleted))
    if deleted:
        return Verdict(outcome=DELETION_OF_PIN, reasons=reasons, test_files=test_paths, deleted_tests=deleted)
    outcome = FAIL if reasons else PASS
    return Verdict(outcome=outcome, reasons=reasons, test_files=test_paths)


def _judge_test_file(base, head, repo_dir, path, status, removed_names, brief, reasons) -> list[str]:
    """Judge one existing test file; append its reasons; return its deleted test names."""
    before = _decode(_blob(base, path, repo_dir))
    after = None if status == "D" else _decode(_blob(head, path, repo_dir))
    if before is None or (status != "D" and after is None):
        reasons.append(f"{path}: not readable as UTF-8 text")
        return []
    try:
        before_tree = ast.parse(before, filename=path)
        after_tree = ast.parse(after or "", filename=path)
    except (SyntaxError, ValueError):
        reasons.append(f"{path}: does not parse as Python")
        return []

    before_funcs = _test_functions(before_tree)
    after_funcs = _test_functions(after_tree)
    gone = sorted(set(before_funcs) - set(after_funcs))
    for name in gone:
        reasons.append(f"{name}: test function deleted or renamed; approval stays with a human")

    for name in sorted(set(before_funcs) & set(after_funcs)):
        before_units = _assertion_units(before_funcs[name])
        after_units = _assertion_units(after_funcs[name])
        if len(after_units) < len(before_units):
            reasons.append(f"{name}: assertions {len(before_units)} -> {len(after_units)}")
        if _literals_changed_without_brief(before_units, after_units, brief):
            reasons.append(f"{name}: expected value changed on an assertion the brief does not name")

    removed, added = _line_changes(before.splitlines(), (after or "").splitlines())
    if any(_SKIP_RE.search(line) for line in added):
        reasons.append(f"{path}: added skip, xfail or importorskip")
    unexplained = [
        number for number, text in removed
        if text.strip() and not set(_IDENT_RE.findall(text)) & removed_names
    ]
    if unexplained:
        numbers = ", ".join(str(number) for number in unexplained)
        reasons.append(f"{path}: a changed line does not reference a removed name (lines {numbers})")
    return gone


def _changed_files(base: str, head: str, repo_dir) -> list[tuple[str, str]]:
    """``(status, path)`` for every file that differs between the two refs."""
    _check_ref(base)
    _check_ref(head)
    output = _git(repo_dir, "diff", "-z", "--name-status", "--no-renames", base, head)
    fields = output.decode("utf-8", "replace").split("\0")
    changes: list[tuple[str, str]] = []
    for index in range(0, len(fields) - 1, 2):
        status, path = fields[index], fields[index + 1]
        if status and path:
            changes.append((status[0], path))
    return changes


def _removed_names_outside_tests(base, head, repo_dir, changes) -> set[str]:
    """Identifiers on removed lines of the non-test files in the diff."""
    names: set[str] = set()
    for status, path in changes:
        if _is_test_path(path) or status == "A":
            continue
        before = _decode(_blob(base, path, repo_dir))
        after = "" if status == "D" else _decode(_blob(head, path, repo_dir))
        if before is None or after is None:
            continue
        removed, _ = _line_changes(before.splitlines(), after.splitlines())
        for _, text in removed:
            names.update(_IDENT_RE.findall(text))
    return names


def _test_functions(tree: ast.AST) -> dict[str, ast.AST]:
    """Test functions by name; a method is qualified by its class name."""
    found: dict[str, ast.AST] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test"):
                    found[prefix + child.name] = child
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def _assertion_units(func: ast.AST) -> list[tuple[str, list[ast.AST]]]:
    """Each assert statement and each expect block, in source order.

    Returns ``(text, nodes)`` where ``text`` identifies the unit for diffing and
    ``nodes`` are the expressions whose literals are the unit's expected values.
    An assert's message is not an expected value, so only its test is included.
    """
    units: list[tuple[int, str, list[ast.AST]]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            units.append((node.lineno, ast.unparse(node.test), [node.test]))
        elif isinstance(node, (ast.With, ast.AsyncWith)) and any(
            _is_expect_call(item.context_expr) for item in node.items
        ):
            expressions = [item.context_expr for item in node.items]
            text = "with " + ", ".join(ast.unparse(expr) for expr in expressions)
            units.append((node.lineno, text, expressions))
    return [(text, nodes) for _, text, nodes in sorted(units, key=lambda unit: unit[0])]


def _is_expect_call(expression: ast.AST) -> bool:
    return (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and expression.func.attr in _EXPECT_ATTRS
        and isinstance(expression.func.value, ast.Name)
        and expression.func.value.id == "pytest"
    )


def _literals_changed_without_brief(before_units, after_units, brief: str) -> bool:
    """True when a changed assertion moves an expected literal the brief does not name."""
    matcher = difflib.SequenceMatcher(
        None, [text for text, _ in before_units], [text for text, _ in after_units], autojunk=False
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal", "insert"):
            continue
        old = _literals([node for _, nodes in before_units[i1:i2] for node in nodes])
        new = _literals([node for _, nodes in after_units[j1:j2] for node in nodes])
        if any(not _brief_names(value, brief) for value in old ^ new):
            return True
    return False


def _literals(nodes: list[ast.AST]) -> set[tuple[str, object]]:
    found: set[tuple[str, object]] = set()
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Constant) and _is_expected_value(node.value):
                found.add((type(node.value).__name__, node.value))
    return found


def _is_expected_value(value: object) -> bool:
    """A string or number is an expected value; booleans and ``None`` are not."""
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _brief_names(literal: tuple[str, object], brief: str) -> bool:
    kind, value = literal
    if kind == "str":
        if value == "":
            return '""' in brief or "''" in brief
        return str(value) in brief
    pattern = rf"(?<![\w.]){re.escape(str(value))}(?![\w]|\.\d)"
    return re.search(pattern, brief) is not None


def _line_changes(before: list[str], after: list[str]) -> tuple[list[tuple[int, str]], list[str]]:
    """Removed ``(1-based line, text)`` pairs and added texts between two line lists."""
    removed: list[tuple[int, str]] = []
    added: list[str] = []
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend((index + 1, before[index]) for index in range(i1, i2))
        if tag in ("replace", "insert"):
            added.extend(after[j1:j2])
    return removed, added


def _is_test_path(path: str) -> bool:
    return path.startswith("tests/") and path.endswith(".py")


def _check_ref(ref: str) -> None:
    if not isinstance(ref, str) or not _REF_RE.match(ref) or ref.startswith("-"):
        raise _JudgeError("invalid git ref")


def _blob(ref: str, path: str, repo_dir) -> bytes:
    return _git(repo_dir, "show", f"{ref}:{path}")


def _git(repo_dir, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _JudgeError(f"could not read git history ({type(exc).__name__})") from exc
    return result.stdout


def _decode(data: bytes) -> str | None:
    """The file as UTF-8 text, or ``None`` for binary or undecodable content."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
