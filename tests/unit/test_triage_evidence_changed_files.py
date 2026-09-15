"""Tests for the ``CHANGED FILES vs {base}:`` section of triage evidence.

TRIFID-3. ``pipeline.triage._current_git_state`` emits the ``GIT STATE:``
block of the triage evidence. On WAP-9 the overlord read that block and
concluded the branch had "edited a pre-existing test assertion" for a test
that does not exist on master at all -- it lived only in the branch's own NEW
file. A changed-file list carrying git's status letters (``A`` vs ``M``) makes
that misreading impossible, which is what this section adds.

These tests are self-contained: where a real repository is needed they build a
throwaway one under ``tmp_path`` with ``git init`` (a legitimate external
boundary fixture). No existing test file is touched.
"""
import re
import subprocess

import pytest

from pipeline import server as pipeline_server
from pipeline import triage

# A status line is git's --name-status form: a status letter (optionally with a
# similarity score, e.g. R100), a TAB, then the path.
_STATUS_RE = re.compile(r"^[A-Z]\d*\t")

_HEADER_PREFIX = "CHANGED FILES vs "


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _git(repo, *args):
    """Run git in ``repo``; raise on failure (fixture setup only)."""
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )


def _init_repo(tmp_path):
    """Create a throwaway repo with one base commit on its default branch, then
    check out the story's agent branch. Returns ``(repo, base_branch)``.

    The shape mirrors the real pipeline: the base ref is the repo's default
    branch (``master``/``main``) and the story's work happens on
    ``agent/<story_key>``, so ``git diff <base>...HEAD`` reports the branch's
    own changes.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "triage-test@example.test")
    _git(repo, "config", "user.name", "Triage Test")
    (repo / "base.txt").write_text("base\n")
    (repo / "mod.txt").write_text("mod\n")
    (repo / "del.txt").write_text("del\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(repo, "checkout", "-b", "agent/s1")
    return repo, base


def _patch_base(monkeypatch, base):
    """Point the base-ref resolver at the throwaway repo's base branch.

    The implementation resolves the base ref via ``_default_branch()`` (lazily
    imported from ``pipeline.server``), so patch both namespaces.
    """
    monkeypatch.setattr(triage, "_default_branch", lambda: base, raising=False)
    monkeypatch.setattr(pipeline_server, "_default_branch", lambda: base)


def _story():
    return {"key": "s1", "story_key": "s1"}


def _section_lines(result):
    """Return the lines of the CHANGED FILES section (header excluded)."""
    lines = result.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(_HEADER_PREFIX):
            return lines[i + 1:]
    pytest.fail(f"no CHANGED FILES header in evidence:\n{result}")


def _status_lines(result):
    return [ln for ln in _section_lines(result) if _STATUS_RE.match(ln)]


class _Completed:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _argv(args, kwargs):
    if args:
        return list(args[0])
    return list(kwargs.get("args") or [])


def _stub_diff(monkeypatch, on_diff):
    """Patch triage's subprocess seam: HEAD probe succeeds, the diff call is
    ``on_diff`` (which may raise)."""
    def _run(*args, **kwargs):
        if "diff" in _argv(args, kwargs):
            return on_diff()
        return _Completed(returncode=0, stdout="deadbeef\n")

    monkeypatch.setattr(triage, "subprocess.run", _run)


# ---------------------------------------------------------------------------
# Positive: git's status letters reach the evidence
# ---------------------------------------------------------------------------

def test_added_file_is_reported_with_A_status(tmp_path, monkeypatch):
    repo, base = _init_repo(tmp_path)
    _patch_base(monkeypatch, base)
    (repo / "new.txt").write_text("new\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add new.txt")

    result = triage._current_git_state(str(repo), _story())

    assert "A\tnew.txt" in result.splitlines()


def test_modified_file_is_reported_with_M_status(tmp_path, monkeypatch):
    repo, base = _init_repo(tmp_path)
    _patch_base(monkeypatch, base)
    (repo / "mod.txt").write_text("changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "modify mod.txt")

    result = triage._current_git_state(str(repo), _story())

    assert "M\tmod.txt" in result.splitlines()


def test_deleted_file_is_reported_with_D_status(tmp_path, monkeypatch):
    repo, base = _init_repo(tmp_path)
    _patch_base(monkeypatch, base)
    (repo / "del.txt").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "delete del.txt")

    result = triage._current_git_state(str(repo), _story())

    assert "D\tdel.txt" in result.splitlines()


def test_renamed_file_is_reported_verbatim(tmp_path, monkeypatch):
    repo, base = _init_repo(tmp_path)
    _patch_base(monkeypatch, base)
    _git(repo, "mv", "mod.txt", "renamed.txt")
    _git(repo, "commit", "-m", "rename mod.txt")

    result = triage._current_git_state(str(repo), _story())

    assert any(
        ln.startswith("R") and "\tmod.txt\trenamed.txt" in ln
        for ln in _status_lines(result)
    )


def test_section_header_names_the_base_ref(tmp_path, monkeypatch):
    repo, base = _init_repo(tmp_path)
    _patch_base(monkeypatch, base)

    result = triage._current_git_state(str(repo), _story())

    assert f"CHANGED FILES vs {base}:" in result


def test_existing_git_state_lines_are_unchanged(tmp_path, monkeypatch):
    """The change is purely additive: the GIT STATE lines must survive."""
    repo, base = _init_repo(tmp_path)
    _patch_base(monkeypatch, base)

    result = triage._current_git_state(str(repo), _story())

    assert "GIT STATE: HEAD" in result
    assert f"vs {base}" in result


# ---------------------------------------------------------------------------
# Negative: fail closed and loudly, never raise
# ---------------------------------------------------------------------------

def test_failing_diff_yields_unavailable_and_keeps_header(tmp_path, monkeypatch):
    repo, base = _init_repo(tmp_path)
    _patch_base(monkeypatch, base)
    _stub_diff(monkeypatch, lambda: _Completed(returncode=128, stderr="fatal"))

    result = triage._current_git_state(str(repo), _story())

    assert f"CHANGED FILES vs {base}:" in result
    assert "(unavailable)" in result


def test_raising_diff_yields_unavailable_and_keeps_header(tmp_path, monkeypatch):
    repo, base = _init_repo(tmp_path)
    _patch_base(monkeypatch, base)

    def _boom():
        raise OSError("no git binary")

    _stub_diff(monkeypatch, _boom)

    result = triage._current_git_state(str(repo), _story())

    assert f"CHANGED FILES vs {base}:" in result
    assert "(unavailable)" in result


def test_non_repo_directory_yields_unavailable_and_keeps_header(tmp_path, monkeypatch):
    """A directory that is not a usable git worktree must still name the
    section, so an absent section is never read as "no changes"."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    _patch_base(monkeypatch, "main")

    result = triage._current_git_state(str(not_a_repo), _story())

    assert "CHANGED FILES vs main:" in result
    assert "(unavailable)" in result


def test_missing_worktree_directory_does_not_raise(tmp_path, monkeypatch):
    """A missing worktree must never raise.

    The whole probe fails (there is no directory to run git in), so the
    function fails open to ``""`` -- the contract already pinned by
    ``tests/unit/test_triage_current_git_state.py::test_subprocess_raising_returns_empty``,
    which asserts ``""`` for exactly this input shape. The header +
    ``(unavailable)`` path is covered by the non-repo and failing-diff tests
    above, which are the reachable cases where an absent section could
    otherwise be misread as "no changes".
    """
    missing = tmp_path / "gone"
    _patch_base(monkeypatch, "main")

    result = triage._current_git_state(str(missing), _story())

    assert isinstance(result, str)
    assert result == ""


# ---------------------------------------------------------------------------
# Boundary: empty, exactly 50, and truncation
# ---------------------------------------------------------------------------

def test_no_changes_yields_none(tmp_path, monkeypatch):
    repo, base = _init_repo(tmp_path)
    _patch_base(monkeypatch, base)

    result = triage._current_git_state(str(repo), _story())

    assert f"CHANGED FILES vs {base}:" in result
    assert _section_lines(result) == ["(none)"]


def test_more_than_50_paths_are_truncated(tmp_path, monkeypatch):
    repo, base = _init_repo(tmp_path)
    _patch_base(monkeypatch, base)
    for i in range(53):
        (repo / f"f{i:03d}.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "53 files")

    result = triage._current_git_state(str(repo), _story())

    assert f"CHANGED FILES vs {base}:" in result
    assert len(_status_lines(result)) == 50
    assert "... and 3 more" in result.splitlines()
    # Bounded: collect_triage_evidence truncates the whole block at limit=8000.
    assert len(result) < 8000


def test_exactly_50_paths_are_not_truncated(tmp_path, monkeypatch):
    repo, base = _init_repo(tmp_path)
    _patch_base(monkeypatch, base)
    for i in range(50):
        (repo / f"g{i:03d}.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "50 files")

    result = triage._current_git_state(str(repo), _story())

    assert len(_status_lines(result)) == 50
    assert not any(ln.startswith("... and ") for ln in result.splitlines())


def test_51_paths_report_one_more(tmp_path, monkeypatch):
    repo, base = _init_repo(tmp_path)
    _patch_base(monkeypatch, base)
    for i in range(51):
        (repo / f"h{i:03d}.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "51 files")

    result = triage._current_git_state(str(repo), _story())

    assert len(_status_lines(result)) == 50
    assert "... and 1 more" in result.splitlines()
