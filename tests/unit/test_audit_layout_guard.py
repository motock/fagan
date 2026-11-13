"""Guard ADR 0012 decision 10: only the audit family knows the audit layout.

After the audit stream moved behind :mod:`pipeline.audit` and its file log,
the record layout - the ``<plan>.audit.jsonl`` file name, the path builders,
the chain reader and the appender - belongs to the audit family alone. Every
other module that knows where a record lives is a second copy of that
knowledge, and a second copy is what drifts: the day one module keeps
appending where the verifier no longer reads, the chain stops verifying and
nothing says why.

The guard scans the ``.py`` files under ``pipeline/``, ``app/`` and
``scripts/`` and fails for any file outside the audit family that touches the
layout. It is the same shape as ``tests/unit/test_plan_dir_allowlist.py``: a
pure ``_measure(root)`` and ``_problems(found, allowed)`` over a temporary
tree, plus one test over the real tree.

Two kinds of text are not a reader and are not counted:

* prose - a docstring or a comment that *names* the layout (``<PLAN_DIR>/.audit.host.jsonl``)
  documents the seam, it does not open the file;
* a private helper a module declares for itself - ``_tail`` in
  ``pipeline/setup/services.py`` trims a command's output, and
  ``pipeline/store_sqlite_audit.py`` has its own ``_tail`` over the table. Only
  the audit family's own tail reader and appender count, so they are matched
  where they are reached *through* the family.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

#: The modules allowed to know the audit layout.
ALLOWED = frozenset(
    {
        "pipeline/audit.py",
        "pipeline/audit_file.py",
        "pipeline/audit_anchor.py",
        "pipeline/decision_records.py",
        "pipeline/audit_chain.py",
        "pipeline/persistence.py",
    }
)

#: The roots that are scanned; ``tests/`` and ``docs/`` are not.
SCANNED_ROOTS = ("pipeline", "app", "scripts")

#: The patterns that mean "this module knows where the audit records live".
#:
#: The last two name the audit family's private tail reader and appender, and
#: only where they are reached *through* the family (``audit._tail(path)``). A
#: module's own ``_tail`` helper - ``pipeline/setup/services.py`` trimming a
#: command's output, ``pipeline/store_sqlite_audit.py`` reading the table - is
#: that module's business, not the audit layout, so it is not counted.
PATTERNS = (
    re.compile(r"\.audit\.jsonl"),
    re.compile(r"\.audit\.host\.jsonl"),
    re.compile(r"\baudit_path\("),
    re.compile(r"\bhost_audit_path\("),
    re.compile(r"\b_verify_chain_at\b"),
    re.compile(r"(?:audit|audit_file|audit_anchor)\._tail\b"),
    re.compile(r"(?:audit|audit_file|audit_anchor)\._append_at\b"),
)


def _prose_lines(text: str) -> set[int]:
    """Return the line numbers of *text* that are prose, not code.

    A docstring is a bare string expression; its lines are documentation even
    when they name a file the module must not touch.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover - a file that does not parse
        return set()
    prose: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            prose.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return prose


def _measure(root: Path) -> dict[str, int]:
    """Return how many code lines of each file touch the audit layout."""
    found: dict[str, int] = {}
    for name in SCANNED_ROOTS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            prose = _prose_lines(text)
            count = 0
            for number, line in enumerate(text.splitlines(), 1):
                if number in prose or line.lstrip().startswith("#"):
                    continue
                if any(pattern.search(line) for pattern in PATTERNS):
                    count += 1
            if count:
                found[path.relative_to(root).as_posix()] = count
    return found


def _problems(found: dict[str, int], allowed: frozenset[str]) -> list[str]:
    """Return one problem per file outside *allowed* that touches the layout."""
    return [
        f"audit layout used outside the audit family: {rel}"
        for rel in sorted(found)
        if rel not in allowed
    ]


def _tree(root: Path, relative: str, text: str) -> Path:
    """Write *text* to *relative* under *root*, creating its directory."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# The scanner
# --------------------------------------------------------------------------


def test_should_fail_on_a_new_file_that_names_the_audit_file(tmp_path: Path) -> None:
    _tree(tmp_path, "pipeline/other.py", 'STREAM = "alpha.audit.jsonl"\n')
    assert _problems(_measure(tmp_path), ALLOWED) == [
        "audit layout used outside the audit family: pipeline/other.py"
    ]


def test_should_pass_for_a_file_in_the_allowed_set(tmp_path: Path) -> None:
    _tree(tmp_path, "pipeline/audit.py", 'STREAM = "alpha.audit.jsonl"\n')
    assert _problems(_measure(tmp_path), ALLOWED) == []


def test_should_fail_on_a_new_file_that_calls_a_path_builder(tmp_path: Path) -> None:
    _tree(tmp_path, "pipeline/other.py", "from pipeline import audit\np = audit.audit_path('a')\n")
    assert _measure(tmp_path) == {"pipeline/other.py": 1}


def test_should_fail_on_a_new_file_in_app_or_scripts(tmp_path: Path) -> None:
    _tree(tmp_path, "app/other.py", 'STREAM = ".audit.host.jsonl"\n')
    _tree(tmp_path, "scripts/other.py", 'STREAM = ".audit.host.jsonl"\n')
    assert _problems(_measure(tmp_path), ALLOWED) == [
        "audit layout used outside the audit family: app/other.py",
        "audit layout used outside the audit family: scripts/other.py",
    ]


def test_should_ignore_non_python_files(tmp_path: Path) -> None:
    _tree(tmp_path, "pipeline/other.md", "The stream is `alpha.audit.jsonl`.\n")
    _tree(tmp_path, "pipeline/other.sh", 'STREAM="alpha.audit.jsonl"\n')
    assert _measure(tmp_path) == {}


def test_should_not_scan_tests_or_docs(tmp_path: Path) -> None:
    _tree(tmp_path, "tests/test_other.py", 'STREAM = "alpha.audit.jsonl"\n')
    _tree(tmp_path, "docs/other.py", 'STREAM = "alpha.audit.jsonl"\n')
    assert _measure(tmp_path) == {}


def test_should_not_count_a_docstring_that_names_the_layout(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        "pipeline/other.py",
        '"""The host chain lives at ``<PLAN_DIR>/.audit.host.jsonl``."""\n',
    )
    assert _measure(tmp_path) == {}


def test_should_not_count_a_comment_that_names_the_layout(tmp_path: Path) -> None:
    _tree(tmp_path, "pipeline/other.py", "# writes alpha.audit.jsonl\nX = 1\n")
    assert _measure(tmp_path) == {}


def test_should_not_count_a_module_s_own_tail_helper(tmp_path: Path) -> None:
    """``_tail`` in another module is that module's helper, not the audit layout."""
    _tree(
        tmp_path,
        "pipeline/store_sqlite_audit.py",
        "def _tail(self, namespace, chain):\n    return self._tail(namespace, chain)\n",
    )
    assert _measure(tmp_path) == {}


def test_should_count_the_audit_family_s_tail_reader(tmp_path: Path) -> None:
    _tree(tmp_path, "pipeline/other.py", "tail = audit._tail(path)\n")
    assert _problems(_measure(tmp_path), ALLOWED) == [
        "audit layout used outside the audit family: pipeline/other.py"
    ]


def test_should_pass_on_the_real_tree() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    problems = _problems(_measure(repo_root), ALLOWED)
    assert problems == [], "\n".join(problems)
