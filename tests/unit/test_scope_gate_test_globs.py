"""Story file-scope gate honours a repo's declared ``.fagan.json`` test_globs.

LAG-4: ``scope_violations`` takes an optional ``test_globs`` keyword and the
worktree gate (``check_branch_scope``) loads those globs from the repo's
``.fagan.json``, reporting an invalid config as a violation instead of
crashing or ignoring it.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from pipeline import scope_gate

_OUTSIDE = "outside this story's `files` scope"


def _completed(cmd, stdout):
    return subprocess.CompletedProcess(cmd, 0, stdout, "")


def _fake_git(diff_stdout, tree_stdout="pipeline\n"):
    """Stand-in for ``subprocess.run`` answering the gate's two git calls."""

    def run(cmd, **kwargs):
        if cmd[1] == "diff":
            return _completed(cmd, diff_stdout)
        return _completed(cmd, tree_stdout)

    return run


# --- scope_violations: test_globs -------------------------------------------


def test_should_allow_path_matching_declared_glob():
    assert (
        scope_gate.scope_violations(
            ["src/it/java/FooCheck.java"], [], set(), test_globs=("src/it/**",)
        )
        == []
    )


def test_glob_without_wildcard_matches_the_exact_path():
    assert (
        scope_gate.scope_violations(
            ["src/it/java/FooCheck.java"],
            [],
            set(),
            test_globs=("src/it/java/FooCheck.java",),
        )
        == []
    )


def test_glob_does_not_allow_a_path_outside_it():
    assert scope_gate.scope_violations(
        ["src/it/java/FooCheck.java"], [], set(), test_globs=("src/main/**",)
    ) == [f"src/it/java/FooCheck.java: {_OUTSIDE}"]


def test_should_still_flag_production_path_not_matching_any_glob():
    assert scope_gate.scope_violations(
        ["pipeline/foo.py"], [], set(), test_globs=("src/it/**", "tests/**")
    ) == [f"pipeline/foo.py: {_OUTSIDE}"]


def test_should_behave_as_before_with_empty_globs():
    changed = ["src/it/java/FooCheck.java", "pipeline/foo.py"]
    expected = [
        f"pipeline/foo.py: {_OUTSIDE}",
        f"src/it/java/FooCheck.java: {_OUTSIDE}",
    ]
    assert scope_gate.scope_violations(changed, [], set()) == expected
    assert scope_gate.scope_violations(changed, [], set(), test_globs=()) == expected


def test_is_test_path_still_allows_test_files_when_globs_are_declared():
    assert (
        scope_gate.scope_violations(
            ["tests/unit/test_x.py"], [], set(), test_globs=("src/it/**",)
        )
        == []
    )


def test_is_test_path_signature_is_unchanged():
    params = list(inspect.signature(scope_gate.is_test_path).parameters.values())
    assert len(params) == 1
    assert scope_gate.is_test_path("tests/unit/test_x.py") is True
    assert scope_gate.is_test_path("pipeline/foo.py") is False


# --- check_branch_scope: loads .fagan.json ----------------------------------


def test_gate_passes_declared_globs_to_scope_violations(tmp_path, monkeypatch):
    (tmp_path / ".fagan.json").write_text(
        '{"test_globs": ["src/it/**"]}', encoding="utf-8"
    )
    monkeypatch.setattr(
        scope_gate.subprocess, "run", _fake_git("src/it/java/FooCheck.java\n")
    )
    assert scope_gate.check_branch_scope(str(tmp_path), "main", []) == []


def test_gate_without_fagan_json_still_flags_the_same_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scope_gate.subprocess, "run", _fake_git("src/it/java/FooCheck.java\n")
    )
    assert scope_gate.check_branch_scope(str(tmp_path), "main", []) == [
        f"src/it/java/FooCheck.java: {_OUTSIDE}"
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        '{"nope": 1}',
        '{"test_globs": "src/it/**"}',
    ],
)
def test_should_report_invalid_fagan_json_as_violation(tmp_path, monkeypatch, payload):
    (tmp_path / ".fagan.json").write_text(payload, encoding="utf-8")
    monkeypatch.setattr(
        scope_gate.subprocess, "run", _fake_git("pipeline/foo.py\n")
    )
    lines = scope_gate.check_branch_scope(str(tmp_path), "main", ["pipeline/foo.py"])
    reported = [line for line in lines if line.startswith(".fagan.json: invalid - ")]
    assert len(reported) == 1
    assert reported[0].removeprefix(".fagan.json: invalid - ").strip()


# --- docs -------------------------------------------------------------------


def test_reference_documents_test_globs_as_scope_gate_test_paths():
    text = Path("REFERENCE.md").read_text(encoding="utf-8")
    start = text.index("## Test, lint and build commands")
    end = text.index("\n## ", start + 1)
    section = text[start:end]
    assert "test_globs" in section
    assert "fnmatch" in section
