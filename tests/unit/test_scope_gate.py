"""TDD tests for ``pipeline.scope_gate`` -- the story ``files`` scope gate.

The gate exists because two live incidents shipped production changes that the
story brief never declared:

* a stray edit to ``scripts/pipeline-env.sh`` while the story's ``files`` list
  named only ``pipeline/foo.py``;
* a whole new top-level ``httpx/`` package (``httpx/__init__.py`` +
  ``httpx/_client.py``) vendored into the repo root, invisible to a
  per-file ``files`` list because the *directory* was new.

``pipeline.scope_gate`` is the pure classifier plus a thin git adapter:

* ``is_test_path(path)`` -- test paths are always allowed to change.
* ``scope_violations(changed_paths, allowed, base_top_level)`` -- PURE; returns
  the sorted, de-duplicated violation lines (``[]`` when clean).
* ``check_branch_scope(worktree, base_ref, allowed)`` -- runs
  ``git diff --name-only --no-renames <base_ref>..HEAD`` and
  ``git ls-tree --name-only <base_ref>`` and delegates to
  ``scope_violations``. It FAILS OPEN: any git failure, ``OSError`` or timeout
  yields ``[]`` so the reviewer still runs.

The gate only applies when a story declares ``files``; a story with no declared
scope is never gated.

These tests are RED until the implementation lands: ``pipeline.scope_gate``
does not exist yet, so the module import below raises ``ModuleNotFoundError``.
"""
from __future__ import annotations

import inspect
import subprocess

import pytest

# Import pipeline.server FIRST: pipeline.scope_gate is imported through the
# same package whose submodules form a circular import (pipeline.advance on its
# own raises ImportError). Importing the server module first breaks the cycle.
import pipeline.server  # noqa: F401  (import-order side effect only)
from pipeline import scope_gate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_HEADER = (
    "SCOPE GATE: this branch changes production paths outside the story's "
    "declared `files` scope."
)


def _outside(path: str) -> str:
    return f"{path}: outside this story's `files` scope"


def _new_package(top: str) -> str:
    return f"{top}/: new top-level package not present at the branch base"


class _FakeProc:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _GitStub:
    """Records git invocations and returns canned stdout per subcommand."""

    def __init__(
        self,
        diff_stdout: str = "",
        tree_stdout: str = "",
        diff_rc: int = 0,
        tree_rc: int = 0,
        exc: BaseException | None = None,
    ):
        self.diff_stdout = diff_stdout
        self.tree_stdout = tree_stdout
        self.diff_rc = diff_rc
        self.tree_rc = tree_rc
        self.exc = exc
        self.calls: list[tuple[object, dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, dict(kwargs)))
        if self.exc is not None:
            raise self.exc
        if "diff" in args:
            return _FakeProc(self.diff_stdout, self.diff_rc)
        if "ls-tree" in args:
            return _FakeProc(self.tree_stdout, self.tree_rc)
        raise AssertionError(f"unexpected git command: {args!r}")


def _install_git(monkeypatch, **kwargs) -> _GitStub:
    """Patch ``subprocess.run`` as seen from ``pipeline.scope_gate``."""
    stub = _GitStub(**kwargs)
    monkeypatch.setattr(scope_gate.subprocess, "run", stub)
    return stub


# ---------------------------------------------------------------------------
# Module surface: constant, docstring, exactly three functions
# ---------------------------------------------------------------------------


def test_scope_gate_header_constant_is_exact():
    assert scope_gate.SCOPE_GATE_HEADER == EXPECTED_HEADER


def test_module_docstring_states_the_definition_and_files_scope():
    doc = scope_gate.__doc__
    assert doc, "pipeline.scope_gate must carry a module docstring"
    assert "files" in doc
    assert "scope" in doc.lower()
    # The gate only applies when a story declares `files`.
    assert "declar" in doc.lower()


def test_module_exposes_exactly_the_three_public_functions():
    for name in ("is_test_path", "scope_violations", "check_branch_scope"):
        assert callable(getattr(scope_gate, name)), f"{name} must be callable"

    public = {
        name
        for name, obj in inspect.getmembers(scope_gate, inspect.isfunction)
        if not name.startswith("_") and obj.__module__ == scope_gate.__name__
    }
    assert public == {"is_test_path", "scope_violations", "check_branch_scope"}


# ---------------------------------------------------------------------------
# is_test_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tests/x.py",
        "tests/unit/test_foo.py",
        "tests/conftest.py",
        "pkg/test_a.py",
        "pkg/a_test.py",
        "conftest.py",
    ],
)
def test_is_test_path_true(path):
    assert scope_gate.is_test_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "pipeline/testing.py",
        "latest_tests.py",
        "pipeline/foo.py",
        "scripts/pipeline-env.sh",
        "httpx/__init__.py",
        "docs/notes.md",
    ],
)
def test_is_test_path_false(path):
    assert scope_gate.is_test_path(path) is False


# ---------------------------------------------------------------------------
# scope_violations -- regression cases named after the incidents
# ---------------------------------------------------------------------------


def test_pipeline_env_stray_edit_is_a_violation():
    changed = ["pipeline/foo.py", "scripts/pipeline-env.sh", "tests/unit/test_foo.py"]
    result = scope_gate.scope_violations(changed, ["pipeline/foo.py"], {"pipeline", "tests"})
    assert result == [_outside("scripts/pipeline-env.sh")]


def test_httpx_stray_package_is_a_violation():
    changed = ["pipeline/foo.py", "httpx/__init__.py", "httpx/_client.py"]
    result = scope_gate.scope_violations(
        changed, ["pipeline/foo.py"], {"pipeline", "tests", "app"}
    )
    assert _new_package("httpx") in result
    assert _outside("httpx/__init__.py") in result
    assert _outside("httpx/_client.py") in result
    assert result == [
        _new_package("httpx"),
        _outside("httpx/__init__.py"),
        _outside("httpx/_client.py"),
    ]


# ---------------------------------------------------------------------------
# scope_violations -- positive / negative / boundary
# ---------------------------------------------------------------------------


def test_all_changes_in_scope_is_clean():
    changed = ["pipeline/foo.py", "pipeline/bar.py"]
    assert scope_gate.scope_violations(changed, changed, {"pipeline"}) == []


def test_only_test_files_changed_is_clean():
    changed = ["tests/unit/test_foo.py", "pkg/a_test.py", "conftest.py"]
    assert scope_gate.scope_violations(changed, ["pipeline/foo.py"], {"pipeline"}) == []


def test_unlisted_markdown_doc_is_a_violation():
    result = scope_gate.scope_violations(
        ["docs/notes.md"], ["pipeline/foo.py"], {"pipeline", "docs"}
    )
    assert result == [_outside("docs/notes.md")]


def test_empty_allowed_with_one_production_change_is_a_violation():
    result = scope_gate.scope_violations(["pipeline/foo.py"], [], {"pipeline"})
    assert result == [_outside("pipeline/foo.py")]


def test_deleted_file_outside_scope_is_a_violation():
    # A deletion still shows up in `git diff --name-only`, so it is graded.
    result = scope_gate.scope_violations(
        ["pipeline/foo.py", "pipeline/old.py"], ["pipeline/foo.py"], {"pipeline"}
    )
    assert result == [_outside("pipeline/old.py")]


def test_empty_changed_paths_is_clean():
    assert scope_gate.scope_violations([], ["pipeline/foo.py"], {"pipeline"}) == []


def test_new_top_level_dir_without_init_has_no_package_line():
    result = scope_gate.scope_violations(
        ["newdir/mod.py"], ["pipeline/foo.py"], {"pipeline"}
    )
    assert result == [_outside("newdir/mod.py")]
    assert not any("new top-level package" in line for line in result)


def test_new_package_whose_files_are_allowed_has_no_package_line():
    changed = ["newpkg/__init__.py", "newpkg/mod.py"]
    result = scope_gate.scope_violations(changed, changed, {"pipeline"})
    assert result == []


def test_top_level_dir_present_at_base_has_no_package_line():
    changed = ["pipeline/__init__.py", "pipeline/new.py"]
    result = scope_gate.scope_violations(changed, ["pipeline/new.py"], {"pipeline"})
    assert result == [_outside("pipeline/__init__.py")]


def test_new_package_line_requires_init_in_changed_paths():
    # `pkg/` is new and has changed files, but no `pkg/__init__.py` change.
    result = scope_gate.scope_violations(
        ["pkg/mod.py", "pkg/sub/deep.py"], [], {"pipeline"}
    )
    assert result == [_outside("pkg/mod.py"), _outside("pkg/sub/deep.py")]


def test_scope_violations_returns_sorted_deduplicated_lines():
    changed = ["b/x.py", "a/y.py", "b/x.py", "a/y.py"]
    result = scope_gate.scope_violations(changed, [], set())
    assert result == [_outside("a/y.py"), _outside("b/x.py")]
    assert result == sorted(set(result))


def test_scope_violations_is_pure_and_never_shells_out(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("scope_violations must not touch subprocess")

    monkeypatch.setattr(scope_gate.subprocess, "run", _boom)
    result = scope_gate.scope_violations(
        ["pipeline/foo.py", "scripts/pipeline-env.sh"], ["pipeline/foo.py"], {"pipeline"}
    )
    assert result == [_outside("scripts/pipeline-env.sh")]


# ---------------------------------------------------------------------------
# check_branch_scope -- adapter (git is the external boundary)
# ---------------------------------------------------------------------------


def test_check_branch_scope_none_base_ref_returns_empty_without_git(monkeypatch):
    stub = _install_git(monkeypatch, diff_stdout="scripts/pipeline-env.sh\n")
    assert scope_gate.check_branch_scope("/tmp/wt", None, ["pipeline/foo.py"]) == []
    assert stub.calls == []


def test_check_branch_scope_runs_expected_git_commands(monkeypatch):
    stub = _install_git(
        monkeypatch, diff_stdout="pipeline/foo.py\n", tree_stdout="pipeline\ntests\n"
    )
    assert scope_gate.check_branch_scope("/tmp/wt", "main", ["pipeline/foo.py"]) == []

    commands = [call[0] for call in stub.calls]
    assert ["git", "diff", "--name-only", "--no-renames", "main..HEAD"] in commands
    assert ["git", "ls-tree", "--name-only", "main"] in commands
    assert len(stub.calls) == 2

    for _args, kwargs in stub.calls:
        assert kwargs["cwd"] == "/tmp/wt"
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        assert kwargs["timeout"] is not None
        assert kwargs["timeout"] > 0


def test_check_branch_scope_success_parses_stdout_ignoring_blanks(monkeypatch):
    diff_stdout = (
        "pipeline/foo.py\n"
        "\n"
        "scripts/pipeline-env.sh\n"
        "   \n"
        "tests/unit/test_foo.py\n"
        "\n"
    )
    tree_stdout = "pipeline\n\ntests\napp\n"
    _install_git(monkeypatch, diff_stdout=diff_stdout, tree_stdout=tree_stdout)

    result = scope_gate.check_branch_scope("/tmp/wt", "main", ["pipeline/foo.py"])
    assert result == [_outside("scripts/pipeline-env.sh")]


def test_check_branch_scope_detects_new_top_level_package(monkeypatch):
    diff_stdout = "pipeline/foo.py\nhttpx/__init__.py\nhttpx/_client.py\n"
    tree_stdout = "pipeline\ntests\napp\n"
    _install_git(monkeypatch, diff_stdout=diff_stdout, tree_stdout=tree_stdout)

    result = scope_gate.check_branch_scope("/tmp/wt", "main", ["pipeline/foo.py"])
    assert result == [
        _new_package("httpx"),
        _outside("httpx/__init__.py"),
        _outside("httpx/_client.py"),
    ]


def test_check_branch_scope_clean_branch_returns_empty(monkeypatch):
    _install_git(
        monkeypatch,
        diff_stdout="pipeline/foo.py\ntests/unit/test_foo.py\n",
        tree_stdout="pipeline\ntests\n",
    )
    assert scope_gate.check_branch_scope("/tmp/wt", "main", ["pipeline/foo.py"]) == []


def test_check_branch_scope_diff_failure_fails_open(monkeypatch):
    _install_git(monkeypatch, diff_stdout="", diff_rc=128, tree_stdout="pipeline\n")
    assert scope_gate.check_branch_scope("/tmp/wt", "main", ["pipeline/foo.py"]) == []


def test_check_branch_scope_ls_tree_failure_fails_open(monkeypatch):
    _install_git(
        monkeypatch,
        diff_stdout="scripts/pipeline-env.sh\n",
        tree_stdout="",
        tree_rc=128,
    )
    assert scope_gate.check_branch_scope("/tmp/wt", "main", ["pipeline/foo.py"]) == []


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("git not found"),
        PermissionError("git not executable"),
        OSError("boom"),
    ],
)
def test_check_branch_scope_oserror_fails_open(monkeypatch, exc):
    _install_git(monkeypatch, exc=exc)
    assert scope_gate.check_branch_scope("/tmp/wt", "main", ["pipeline/foo.py"]) == []


def test_check_branch_scope_timeout_fails_open(monkeypatch):
    exc = subprocess.TimeoutExpired(cmd=["git", "diff"], timeout=1)
    _install_git(monkeypatch, exc=exc)
    assert scope_gate.check_branch_scope("/tmp/wt", "main", ["pipeline/foo.py"]) == []
