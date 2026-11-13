"""Guard ADR 0012 decision 8: the global plans directory has one hard exit rule.

Two counts are taken per file, over the ``.py`` files under ``pipeline/``,
``app/`` and ``scripts/``:

* uppercase ``PLAN_DIR`` - the global binding ADR 0012 forbids - is counted in
  all three roots;
* lowercase ``plan_dir`` - an injected plan-directory parameter - is counted in
  ``pipeline/`` and ``app/`` only, so a script may keep its ``--plan-dir``
  variable.

A reference is an ``ast.Name``, ``ast.Attribute``, ``ast.arg``, ``ast.keyword``
or ``ast.alias`` (an import of the name) carrying the identifier. Prose -
comments, docstrings and the ``PLAN_DIR`` environment-variable string - is not
a reference.

``ALLOWED`` is a fixed set of (file, name) pairs, each with a ceiling and a
one-line reason. A use outside the set fails, a file over its ceiling fails,
and an entry whose file no longer uses the name fails as stale, so the set can
only shrink.
"""

from pathlib import Path

import pytest

from tests.unit._plan_dir_references import reference_counts

#: root directory -> the identifiers that root is scanned for.
_SCANNED = {
    "pipeline": ("PLAN_DIR", "plan_dir"),
    "app": ("PLAN_DIR", "plan_dir"),
    "scripts": ("PLAN_DIR",),
}

_RESIDUE = (
    "still binds the global plans directory; migration tracked by the follow-up "
    "plan store-stage-c3-plan-dir-residue"
)

#: (repo-relative file, identifier) -> (ceiling, reason).
ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("app/dashboard.py", "PLAN_DIR"): (2, _RESIDUE),
    ("pipeline/checkpoint.py", "PLAN_DIR"): (1, _RESIDUE),
    ("pipeline/cross_plan.py", "plan_dir"): (4, "file store bulk manifest loader still takes a directory"),
    ("pipeline/event_wiring.py", "PLAN_DIR"): (2, _RESIDUE),
    ("pipeline/outbox_health.py", "plan_dir"): (2, _RESIDUE),
    ("pipeline/paths.py", "PLAN_DIR"): (1, "configuration: the accessor that resolves the plans directory"),
    ("pipeline/persistence.py", "PLAN_DIR"): (5, "file store implementation"),
    ("pipeline/preflight.py", "plan_dir"): (3, "configuration check a's explicit-directory argument (R12)"),
    ("pipeline/scheduler_daemon.py", "PLAN_DIR"): (1, _RESIDUE),
    ("pipeline/server.py", "PLAN_DIR"): (1, "re-export binding only: the patch point the test suite relies on"),
    ("pipeline/store.py", "PLAN_DIR"): (11, "file store implementation"),
    ("pipeline/tick_checks.py", "PLAN_DIR"): (1, _RESIDUE),
    ("pipeline/watchers.py", "PLAN_DIR"): (1, _RESIDUE),
    ("pipeline/wedge_io.py", "PLAN_DIR"): (1, _RESIDUE),
    ("pipeline/worktree_patch.py", "PLAN_DIR"): (2, _RESIDUE),
    ("scripts/local_success_report.py", "PLAN_DIR"): (3, _RESIDUE),
    ("scripts/smoke_getting_started.py", "PLAN_DIR"): (4, "configuration smoke script (R22)"),
}


def ceiling(rel: str) -> int:
    """The total allowed uses in *rel* across both identifiers; zero if not allowed."""
    return sum(
        allowed_ceiling for (path, _), (allowed_ceiling, _) in ALLOWED.items() if path == rel
    )


def _measure(root: Path) -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for scanned_root, names in _SCANNED.items():
        base = root / scanned_root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            counts = reference_counts(path.read_text(encoding="utf-8"), rel)
            for name in names:
                if counts[name]:
                    found[(rel, name)] = counts[name]
    return found


def _problems(
    found: dict[tuple[str, str], int],
    allowed: dict[tuple[str, str], tuple[int, str]],
) -> list[str]:
    problems: list[str] = []
    for (rel, name), count in sorted(found.items()):
        entry = allowed.get((rel, name))
        if entry is None:
            problems.append(f"direct {name} use outside the allowed set: {rel}")
        elif count > entry[0]:
            problems.append(f"grew past its ceiling: {rel} {name} ({count} > {entry[0]})")
    for rel, name in sorted(allowed):
        if found.get((rel, name), 0) == 0:
            problems.append(f"stale entry, remove it: {rel} {name}")
    return problems


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree_with_references(root: Path, text: str) -> None:
    _write(root, "pipeline/a.py", text)


# -- the rule, on synthetic trees -------------------------------------------


def test_should_pass_when_every_reference_is_within_its_ceiling(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIR = 1\nx = PLAN_DIR\n")
    allowed = {("pipeline/a.py", "PLAN_DIR"): (5, "test")}
    assert _problems(_measure(tmp_path), allowed) == []


def test_should_fail_naming_a_file_outside_the_allowed_set(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIR = 1\n")
    assert _problems(_measure(tmp_path), {}) == [
        "direct PLAN_DIR use outside the allowed set: pipeline/a.py"
    ]


def test_should_fail_when_an_allowed_file_grows_past_its_ceiling(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIR = 1\n" * 6)
    allowed = {("pipeline/a.py", "PLAN_DIR"): (5, "test")}
    assert _problems(_measure(tmp_path), allowed) == [
        "grew past its ceiling: pipeline/a.py PLAN_DIR (6 > 5)"
    ]


def test_should_pass_when_an_allowed_file_shrinks_below_its_ceiling(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIR = 1\nx = PLAN_DIR\n")
    allowed = {("pipeline/a.py", "PLAN_DIR"): (5, "test")}
    assert _problems(_measure(tmp_path), allowed) == []


def test_should_fail_on_an_allowed_entry_whose_file_has_no_use_left(tmp_path):
    _tree_with_references(tmp_path, "x = 1\n")
    allowed = {("pipeline/a.py", "PLAN_DIR"): (5, "test")}
    assert _problems(_measure(tmp_path), allowed) == [
        "stale entry, remove it: pipeline/a.py PLAN_DIR"
    ]


def test_should_fail_on_an_allowed_entry_whose_file_is_missing(tmp_path):
    allowed = {("pipeline/gone.py", "PLAN_DIR"): (2, "test")}
    assert _problems(_measure(tmp_path), allowed) == [
        "stale entry, remove it: pipeline/gone.py PLAN_DIR"
    ]


def test_should_pass_a_use_in_host_state_when_host_state_is_allowed(tmp_path):
    _write(tmp_path, "pipeline/host_state.py", "x = PLAN_DIR\n")
    allowed = {("pipeline/host_state.py", "PLAN_DIR"): (1, "test")}
    assert _problems(_measure(tmp_path), allowed) == []


def test_should_fail_a_second_binding_in_server_past_its_ceiling_of_one(tmp_path):
    _write(tmp_path, "pipeline/server.py", "from .paths import PLAN_DIR\nPLAN_DIR = 2\n")
    allowed = {("pipeline/server.py", "PLAN_DIR"): (1, "re-export binding")}
    assert _problems(_measure(tmp_path), allowed) == [
        "grew past its ceiling: pipeline/server.py PLAN_DIR (2 > 1)"
    ]


def test_should_fail_a_lowercase_plan_dir_parameter_in_pipeline(tmp_path):
    _write(tmp_path, "pipeline/a.py", "def f(plan_dir):\n    return plan_dir\n")
    assert _problems(_measure(tmp_path), {}) == [
        "direct plan_dir use outside the allowed set: pipeline/a.py"
    ]


def test_should_fail_a_lowercase_plan_dir_parameter_in_app(tmp_path):
    _write(tmp_path, "app/a.py", "def f(plan_dir):\n    return plan_dir\n")
    assert _problems(_measure(tmp_path), {}) == [
        "direct plan_dir use outside the allowed set: app/a.py"
    ]


def test_should_pass_a_lowercase_plan_dir_parameter_in_scripts(tmp_path):
    _write(tmp_path, "scripts/a.py", "def main(plan_dir):\n    return plan_dir\n")
    assert _measure(tmp_path) == {}


def test_should_fail_an_uppercase_plan_dir_in_scripts(tmp_path):
    _write(tmp_path, "scripts/a.py", "x = PLAN_DIR\n")
    assert _problems(_measure(tmp_path), {}) == [
        "direct PLAN_DIR use outside the allowed set: scripts/a.py"
    ]


def test_should_fail_an_uppercase_plan_dir_in_preflight_while_lowercase_passes(tmp_path):
    _write(tmp_path, "pipeline/preflight.py", "def f(plan_dir):\n    return PLAN_DIR\n")
    allowed = {("pipeline/preflight.py", "plan_dir"): (1, "check a argument")}
    assert _problems(_measure(tmp_path), allowed) == [
        "direct PLAN_DIR use outside the allowed set: pipeline/preflight.py"
    ]


def test_should_pass_a_lowercase_plan_dir_in_preflight_when_it_is_allowed(tmp_path):
    _write(tmp_path, "pipeline/preflight.py", "def f(plan_dir):\n    return plan_dir\n")
    allowed = {("pipeline/preflight.py", "plan_dir"): (2, "check a argument")}
    assert _problems(_measure(tmp_path), allowed) == []


def test_should_ignore_non_python_files(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIR = 1\n")
    _write(tmp_path, "pipeline/a.sh", "PLAN_DIR=/tmp/x\n")
    _write(tmp_path, "pipeline/a.md", "PLAN_DIR is documented here\n")
    assert set(_measure(tmp_path)) == {("pipeline/a.py", "PLAN_DIR")}


def test_should_ignore_directories_outside_the_scanned_roots(tmp_path):
    _write(tmp_path, "docs/x.py", "PLAN_DIR = 1\n")
    _write(tmp_path, "tests/x.py", "PLAN_DIR = 1\n")
    assert _measure(tmp_path) == {}


def test_should_match_word_boundaries_only(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIRECTORY = 1\nMY_PLAN_DIR = 2\n")
    assert _measure(tmp_path) == {}


def test_should_count_each_reference_node_not_just_lines(tmp_path):
    _tree_with_references(tmp_path, "PLAN_DIR = 1 + PLAN_DIR\n")
    assert _measure(tmp_path) == {("pipeline/a.py", "PLAN_DIR"): 2}


def test_should_not_count_a_plan_dir_mention_in_a_comment(tmp_path):
    _tree_with_references(tmp_path, "# PLAN_DIR is documented here\nx = 1\n")
    assert _measure(tmp_path) == {}


def test_should_not_count_a_plan_dir_mention_in_a_docstring(tmp_path):
    _tree_with_references(tmp_path, '"""Uses PLAN_DIR internally."""\nx = 1\n')
    assert _measure(tmp_path) == {}


def test_should_not_count_the_plan_dir_environment_variable_name(tmp_path):
    _tree_with_references(tmp_path, "import os\nos.environ['PLAN_DIR']\n")
    assert _measure(tmp_path) == {}


def test_should_count_a_bare_name_reference(tmp_path):
    _tree_with_references(tmp_path, "x = PLAN_DIR\n")
    assert _measure(tmp_path) == {("pipeline/a.py", "PLAN_DIR"): 1}


def test_should_count_an_attribute_reference(tmp_path):
    _tree_with_references(tmp_path, "from pipeline import paths\nx = paths.PLAN_DIR\n")
    assert _measure(tmp_path) == {("pipeline/a.py", "PLAN_DIR"): 1}


def test_should_count_an_import_of_the_name_as_a_binding(tmp_path):
    _tree_with_references(tmp_path, "from .paths import PLAN_DIR\n")
    assert _measure(tmp_path) == {("pipeline/a.py", "PLAN_DIR"): 1}


def test_should_count_a_keyword_argument_named_plan_dir(tmp_path):
    _tree_with_references(tmp_path, "f(plan_dir=1)\n")
    assert _measure(tmp_path) == {("pipeline/a.py", "plan_dir"): 1}


def test_should_fail_loudly_on_a_syntax_error_naming_the_file(tmp_path):
    _tree_with_references(tmp_path, "def broken(:\n")
    with pytest.raises(ValueError, match="pipeline/a.py"):
        _measure(tmp_path)


# -- the committed set, on the real tree -------------------------------------


def test_should_have_no_use_outside_the_allowed_set_in_the_real_tree():
    repo_root = Path(__file__).resolve().parents[2]
    problems = _problems(_measure(repo_root), ALLOWED)
    assert problems == [], "\n".join(problems)
