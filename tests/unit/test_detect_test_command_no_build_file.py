"""Tests for detect_test_command's no-build-marker behaviour.

Root cause (reproduced live 2026-09-16): pipeline/build_detect.py's
detect_test_command ended with ``return cwd, ["npm", "test"]  # fallback``.
That fallback fires when NO recognised build marker exists in cwd or in any
immediate subdirectory. For a repo with no build system at all (e.g. the
scratch target repo scripts/smoke_getting_started.py creates, holding a single
README.md) the gate then ran npm against a directory with no package.json:

    npm error code ENOENT
    npm error path <worktree>/package.json    -> exit 254

so correct, committed work was marked FAILED.

The fix: the no-build-marker case must return a portable no-op command that
exits 0 (a Python-based no-op such as ``[sys.executable, "-c", "pass"]``),
with cwd exactly as before, and the 2-tuple contract must be preserved so the
six production unpack sites and the 25 monkeypatching test files keep working.

Every test below builds its own synthetic tree under tmp_path; none of them
asserts against this repository's own layout.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import build_detect as bd

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

#: Tokens that would mean the "no build system" case is still trying to run a
#: real ecosystem test suite (or a POSIX-only binary) instead of a no-op.
_REAL_SUITE_TOKENS = ("npm", "yarn", "pytest", "mvn", "gradlew", "cargo", "make")


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run cmd with cwd, no shell, and return the completed process."""
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=120, check=False
    )


def _module_source() -> str:
    return Path(bd.__file__).read_text()


def _function_source(name: str) -> str:
    src = _module_source()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(src, node)
            assert segment is not None
            return segment
    raise AssertionError(f"{name} not found in {bd.__file__}")


def _docstring(name: str) -> str:
    src = _module_source()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_docstring(node) or ""
    raise AssertionError(f"{name} not found in {bd.__file__}")


def _assert_noop(cmd: list[str], cwd: Path) -> None:
    """Assert cmd is a portable, visible no-op that exits 0 in cwd."""
    assert isinstance(cmd, list), f"command must be a list, got {type(cmd)!r}"
    assert cmd, "command must not be empty"
    assert all(isinstance(part, str) for part in cmd), f"non-str argv element in {cmd!r}"

    # Not the old broken fallback, and not a real ecosystem suite.
    assert cmd != ["npm", "test"], "no-build-marker case still returns the npm fallback"
    joined = " ".join(cmd)
    for token in _REAL_SUITE_TOKENS:
        assert token not in joined, (
            f"no-build-marker command {cmd!r} still invokes a real suite token {token!r}"
        )

    # Not a POSIX-only binary such as ['true'].
    assert cmd[0] != "true", "must not depend on the POSIX-only 'true' binary"

    # Python-based no-op: the current interpreter running an inline program.
    assert cmd[0] == sys.executable, (
        f"expected a Python-based no-op starting with sys.executable, got {cmd!r}"
    )
    assert "-c" in cmd, f"expected an inline '-c' program, got {cmd!r}"

    # It must actually run, with no shell, and exit 0.
    result = _run(cmd, cwd)
    assert result.returncode == 0, (
        f"no-op command {cmd!r} exited {result.returncode} in {cwd}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def _assert_two_tuple(result: object, expected_cwd: Path) -> list[str]:
    """Pin the (Path, list[str]) contract every caller unpacks."""
    assert isinstance(result, tuple), f"expected a tuple, got {type(result)!r}"
    assert len(result) == 2, f"expected a 2-tuple, got {result!r}"
    cwd_out, cmd = result
    assert isinstance(cwd_out, Path), f"first element must be a Path, got {type(cwd_out)!r}"
    assert isinstance(cmd, list), f"second element must be a list, got {type(cmd)!r}"
    assert cwd_out == expected_cwd, f"expected cwd {expected_cwd}, got {cwd_out}"
    return cmd


# ---------------------------------------------------------------------------
# 1. POSITIVE - the bug: a README-only directory
# ---------------------------------------------------------------------------


def test_readme_only_dir_does_not_return_npm_fallback(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x\n")

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cmd != ["npm", "test"], (
        "a repo with no build system must not be handed an npm command that "
        "cannot work (npm error code ENOENT -> exit 254)"
    )
    assert cwd_out == tmp_path


def test_readme_only_dir_command_exits_zero(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x\n")

    _cwd_out, cmd = bd.detect_test_command(tmp_path)

    _assert_noop(cmd, tmp_path)


def test_readme_only_dir_matches_smoke_getting_started_scenario(tmp_path: Path) -> None:
    """The exact scratch-repo shape scripts/smoke_getting_started.py creates."""
    (tmp_path / "README.md").write_text("# scratch target\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=False, capture_output=True)

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cmd != ["npm", "test"]
    assert _run(cmd, cwd_out).returncode == 0


# ---------------------------------------------------------------------------
# 2. A completely EMPTY directory behaves the same way
# ---------------------------------------------------------------------------


def test_empty_dir_does_not_return_npm_fallback(tmp_path: Path) -> None:
    assert list(tmp_path.iterdir()) == []

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cmd != ["npm", "test"]
    assert cwd_out == tmp_path


def test_empty_dir_command_exits_zero(tmp_path: Path) -> None:
    assert list(tmp_path.iterdir()) == []

    _cwd_out, cmd = bd.detect_test_command(tmp_path)

    _assert_noop(cmd, tmp_path)


# ---------------------------------------------------------------------------
# 3. NEGATIVE CONTROL - real detection must be untouched
# ---------------------------------------------------------------------------


def test_package_json_still_yields_npm_test(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "x", "version": "1.0.0"}\n')

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cmd == ["npm", "test"], f"genuine npm detection was disabled: {cmd!r}"
    assert cwd_out == tmp_path


def test_package_json_with_yarn_lock_still_yields_yarn_test(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "x", "version": "1.0.0"}\n')
    (tmp_path / "yarn.lock").write_text("")

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cmd == ["yarn", "test"], f"genuine yarn detection was disabled: {cmd!r}"
    assert cwd_out == tmp_path


def test_pyproject_toml_still_yields_pytest_command(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cmd != ["npm", "test"], f"genuine pytest detection was disabled: {cmd!r}"
    assert "pytest" in cmd, f"expected a pytest invocation, got {cmd!r}"
    assert cwd_out == tmp_path


def test_setup_py_still_yields_pytest_command(tmp_path: Path) -> None:
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert "pytest" in cmd, f"expected a pytest invocation, got {cmd!r}"
    assert cwd_out == tmp_path


def test_cargo_toml_still_yields_cargo_test(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n")

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cmd == ["cargo", "test"], f"genuine cargo detection was disabled: {cmd!r}"
    assert cwd_out == tmp_path


def test_makefile_with_test_target_still_yields_make_test(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cmd == ["make", "test"], f"genuine make detection was disabled: {cmd!r}"
    assert cwd_out == tmp_path


# ---------------------------------------------------------------------------
# 4. NEGATIVE CONTROL - the immediate-subdirectory search still works
# ---------------------------------------------------------------------------


def test_markerless_root_with_child_package_json_returns_child(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x\n")
    child = tmp_path / "engine"
    child.mkdir()
    (child / "package.json").write_text('{"name": "engine", "version": "1.0.0"}\n')

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cwd_out == child, f"subdirectory search broken: returned {cwd_out}"
    assert cmd == ["npm", "test"], f"subdirectory detection broken: {cmd!r}"


def test_markerless_root_with_child_pyproject_returns_child(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x\n")
    child = tmp_path / "backend"
    child.mkdir()
    (child / "pyproject.toml").write_text("[project]\nname = 'backend'\n")

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cwd_out == child, f"subdirectory search broken: returned {cwd_out}"
    assert "pytest" in cmd, f"subdirectory detection broken: {cmd!r}"


def test_markerless_root_with_markerless_children_hits_noop(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x\n")
    for name in ("docs", "scripts"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "notes.txt").write_text("x\n")

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cwd_out == tmp_path
    _assert_noop(cmd, tmp_path)


# ---------------------------------------------------------------------------
# 5. BOUNDARY - the (Path, list[str]) 2-tuple contract is pinned
# ---------------------------------------------------------------------------


def test_contract_is_two_tuple_for_noop_case(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x\n")

    cmd = _assert_two_tuple(bd.detect_test_command(tmp_path), tmp_path)
    assert cmd != ["npm", "test"]


def test_contract_is_two_tuple_for_empty_dir(tmp_path: Path) -> None:
    cmd = _assert_two_tuple(bd.detect_test_command(tmp_path), tmp_path)
    assert cmd != ["npm", "test"]


def test_contract_is_two_tuple_for_package_json(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "x"}\n')

    cmd = _assert_two_tuple(bd.detect_test_command(tmp_path), tmp_path)
    assert cmd == ["npm", "test"]


def test_contract_is_two_tuple_for_subdirectory_hit(tmp_path: Path) -> None:
    child = tmp_path / "engine"
    child.mkdir()
    (child / "package.json").write_text('{"name": "engine"}\n')

    cmd = _assert_two_tuple(bd.detect_test_command(tmp_path), child)
    assert cmd == ["npm", "test"]


def test_contract_is_two_tuple_for_dotfiles_only_dir(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("x\n")

    cmd = _assert_two_tuple(bd.detect_test_command(tmp_path), tmp_path)
    assert cmd != ["npm", "test"]


def test_callers_can_unpack_two_tuple_without_a_predicate(tmp_path: Path) -> None:
    """The six production call sites unpack the 2-tuple; no predicate needed."""
    (tmp_path / "README.md").write_text("x\n")

    cwd_out, cmd = bd.detect_test_command(tmp_path)  # must not raise

    assert isinstance(cwd_out, Path)
    assert isinstance(cmd, list)
    assert bd.detect_test_command(tmp_path) is not None


def test_signature_unchanged(tmp_path: Path) -> None:
    sig = inspect.signature(bd.detect_test_command)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["cwd"], f"signature changed: {sig}"
    assert params[0].kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    annotation = str(sig.return_annotation)
    assert "tuple" in annotation, f"return annotation must stay a tuple: {annotation}"
    assert "list" in annotation, f"return annotation must stay a list: {annotation}"


# ---------------------------------------------------------------------------
# 6. BOUNDARY - dotfiles / hidden directories only
# ---------------------------------------------------------------------------


def test_dotfiles_only_dir_hits_noop_not_npm(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    (tmp_path / ".env").write_text("X=1\n")

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cwd_out == tmp_path
    assert cmd != ["npm", "test"]
    _assert_noop(cmd, tmp_path)


def test_hidden_directory_marker_is_skipped_and_noop_returned(tmp_path: Path) -> None:
    """The subdirectory search skips dot-directories, so a package.json inside
    one must NOT be picked up - the no-op path is the honest result."""
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "package.json").write_text('{"name": "hidden"}\n')

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cwd_out == tmp_path, f"hidden dir must be skipped, got {cwd_out}"
    assert cmd != ["npm", "test"]
    _assert_noop(cmd, tmp_path)


def test_dotfile_only_dir_command_is_portable_and_shell_free(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("x\n")

    _cwd_out, cmd = bd.detect_test_command(tmp_path)

    # No shell metacharacters / no shell=True needed.
    assert not any(ch in " ".join(cmd) for ch in ("&&", "||", ";", "|", ">", "<")), cmd
    result = subprocess.run(
        cmd, cwd=tmp_path, shell=False, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# Source-level requirements: the old fallback is gone, the new one documented
# ---------------------------------------------------------------------------


def test_old_npm_fallback_line_is_gone() -> None:
    src = _module_source()
    assert '["npm", "test"]  # fallback' not in src, (
        "the npm fallback line must be removed from pipeline/build_detect.py"
    )
    assert '["npm", "test"] # fallback' not in src


def test_detect_test_command_body_no_longer_returns_npm() -> None:
    body = _function_source("detect_test_command")
    # Ignore the docstring: it may legitimately *mention* the old npm fallback
    # when explaining the change; only the executable body must be npm-free.
    doc = _docstring("detect_test_command")
    if doc:
        body = body.replace(doc, "")
    assert "npm" not in body, (
        "detect_test_command's body must not return an npm command:\n" + body
    )


def test_detect_test_command_docstring_documents_noop_and_why() -> None:
    doc = _docstring("detect_test_command").lower()
    assert doc, "detect_test_command must keep a docstring"
    assert "no-op" in doc or "noop" in doc, (
        "docstring must state that the no-build-marker case returns a no-op command"
    )
    assert "build" in doc, "docstring must explain the no-build-marker case"
    assert "detect_build_command" in doc, (
        "docstring must reference the detect_build_command precedent directly above it"
    )


def test_detect_test_command_docstring_explains_gate_must_not_block() -> None:
    doc = _docstring("detect_test_command").lower()
    assert any(
        token in doc
        for token in ("block", "nothing to test", "no test", "no tests", "nothing to run")
    ), (
        "docstring must explain WHY: a repo with no build system has no test "
        "suite, so the gate must not block it"
    )


def test_detect_test_command_docstring_notes_command_is_recorded() -> None:
    doc = _docstring("detect_test_command").lower()
    assert any(
        token in doc
        for token in ("last_test_check", "manifest", "record", "visible", "persist")
    ), (
        "docstring must note the no-op stays visible in the record (callers "
        "persist it into the manifest's last_test_check.cmd)"
    )


def test_detect_build_command_docstring_no_longer_claims_test_fallback() -> None:
    doc = _docstring("detect_build_command")
    assert "no reasonable universal fallback for" not in doc, (
        "the now-false parenthetical claiming npm test is a reasonable "
        "universal fallback for tests must be fixed"
    )
    assert "unlike detect_test_command" not in doc, (
        "the old contrast ('unlike detect_test_command, there is no reasonable "
        "universal fallback for build') is now false and must be rewritten"
    )


def test_detect_build_command_docstring_states_symmetry() -> None:
    doc = _docstring("detect_build_command").lower()
    assert "detect_test_command" in doc, (
        "docstring must state the two functions are now symmetric in philosophy"
    )
    assert "no-op" in doc or "noop" in doc or "nothing to test" in doc or "no test" in doc, (
        "docstring must say detect_test_command likewise returns a no-op when "
        "nothing is detected"
    )


def test_detect_build_command_still_returns_none_when_no_marker(tmp_path: Path) -> None:
    """The precedent itself must be untouched."""
    (tmp_path / "README.md").write_text("x\n")
    assert bd.detect_build_command(tmp_path) is None


def test_detect_build_command_still_detects_real_build(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"build": "tsc"}}\n')
    result = bd.detect_build_command(tmp_path)
    assert result is not None
    assert result[1] == ["npm", "run", "build"]


@pytest.mark.parametrize("marker", ["README.md", "notes.txt"])
def test_arbitrary_non_marker_file_hits_noop(tmp_path: Path, marker: str) -> None:
    (tmp_path / marker).write_text("x\n")

    cwd_out, cmd = bd.detect_test_command(tmp_path)

    assert cwd_out == tmp_path
    _assert_noop(cmd, tmp_path)
