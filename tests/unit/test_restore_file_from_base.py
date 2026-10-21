"""Mode 49b: restore_file's from_base mode restores the base branch's version.

``restore_file`` historically ran ``git checkout HEAD -- <path>``: it discards
uncommitted edits but keeps whatever the branch's own commits contain. The
destructive-git block refuses ``git checkout <base> -- <path>``, so an agent
whose own commit captured a bad change had no safe first-class way back to the
base branch's version. ``from_base=True`` restores via ``git show <base>:<path>``
instead.

Every case runs against both agent variants, because the two tools modules are
independent copies and a fix applied to only one of them is a real regression.
"""
from __future__ import annotations

import subprocess

import pytest

from scripts import local_agent_oracle_tools, local_agent_tools

MODULES = [local_agent_tools, local_agent_oracle_tools]
MODULE_IDS = ["local", "oracle"]


def _git(repo, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _make_repo(tmp_path, *, symbolic_head=True, origin_master=True):
    """A repo whose HEAD commit is the agent's own bad commit.

    Commit A holds "BASE VERSION\\n" and is what origin's default branch points
    at; commit B holds "BAD VERSION\\n" and is HEAD.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "agent@example.com")
    _git(repo, "config", "user.name", "Agent")
    (repo / "mod.txt").write_text("BASE VERSION\n")
    _git(repo, "add", "mod.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    if origin_master:
        _git(repo, "update-ref", "refs/remotes/origin/master", base_sha)
    if symbolic_head:
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/master")
    (repo / "mod.txt").write_text("BAD VERSION\n")
    _git(repo, "commit", "-q", "-am", "bad")
    return repo


def _count(origin) -> int:
    return origin.get("_RESTORES_THIS_RUN", {}).get("mod.txt", 0)


@pytest.mark.parametrize("mod", MODULES, ids=MODULE_IDS)
def test_from_base_restores_the_base_version_after_a_bad_commit(mod, tmp_path):
    repo = _make_repo(tmp_path)
    origin = {"CWD": repo}

    result = mod.run_tool_impl(origin, "restore_file",
                               {"path": "mod.txt", "from_base": True})

    assert result.startswith("restored mod.txt from origin/master"), result
    assert (repo / "mod.txt").read_text() == "BASE VERSION\n"
    assert _count(origin) == 1


@pytest.mark.parametrize("mod", MODULES, ids=MODULE_IDS)
def test_default_restore_still_uses_head(mod, tmp_path):
    repo = _make_repo(tmp_path)
    origin = {"CWD": repo}

    result = mod.run_tool_impl(origin, "restore_file", {"path": "mod.txt"})

    assert result.startswith("restored mod.txt to its last commit (HEAD)"), result
    assert (repo / "mod.txt").read_text() == "BAD VERSION\n"
    assert _count(origin) == 1


@pytest.mark.parametrize("mod", MODULES, ids=MODULE_IDS)
def test_from_base_restores_a_deleted_file(mod, tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "mod.txt").unlink()
    origin = {"CWD": repo}

    result = mod.run_tool_impl(origin, "restore_file",
                               {"path": "mod.txt", "from_base": True})

    assert result.startswith("restored mod.txt from origin/master"), result
    assert (repo / "mod.txt").read_text() == "BASE VERSION\n"
    assert _count(origin) == 1


@pytest.mark.parametrize("mod", MODULES, ids=MODULE_IDS)
def test_from_base_errors_when_path_is_absent_from_base(mod, tmp_path):
    repo = _make_repo(tmp_path)
    origin = {"CWD": repo}

    result = mod.run_tool_impl(origin, "restore_file",
                               {"path": "missing.txt", "from_base": True})

    assert result.startswith(
        "ERROR: could not restore missing.txt from origin/master"), result
    assert not (repo / "missing.txt").exists()
    assert origin.get("_RESTORES_THIS_RUN", {}).get("missing.txt", 0) == 0

    # A failed from_base restore must not consume the cap: a second failure is
    # still a `git show` error, never a cap refusal.
    again = mod.run_tool_impl(origin, "restore_file",
                              {"path": "missing.txt", "from_base": True})
    assert again.startswith(
        "ERROR: could not restore missing.txt from origin/master"), again
    assert origin.get("_RESTORES_THIS_RUN", {}).get("missing.txt", 0) == 0


@pytest.mark.parametrize("mod", MODULES, ids=MODULE_IDS)
def test_from_base_falls_back_to_origin_master_without_symbolic_head(mod, tmp_path):
    repo = _make_repo(tmp_path, symbolic_head=False)
    origin = {"CWD": repo}

    result = mod.run_tool_impl(origin, "restore_file",
                               {"path": "mod.txt", "from_base": True})

    assert result.startswith("restored mod.txt from origin/master"), result
    assert (repo / "mod.txt").read_text() == "BASE VERSION\n"


@pytest.mark.parametrize("mod", MODULES, ids=MODULE_IDS)
def test_from_base_errors_when_no_base_ref_resolves(mod, tmp_path):
    repo = _make_repo(tmp_path, symbolic_head=False, origin_master=False)
    origin = {"CWD": repo}

    result = mod.run_tool_impl(origin, "restore_file",
                               {"path": "mod.txt", "from_base": True})

    assert result.startswith("ERROR: from_base restore of mod.txt failed"), result
    assert "origin/master" in result
    assert "origin/main" in result
    assert (repo / "mod.txt").read_text() == "BAD VERSION\n"
    assert _count(origin) == 0

    # State regression: once a base ref exists the very next call succeeds and
    # starts the counter at 1 (the earlier failure left it at 0).
    base_sha = _git(repo, "rev-parse", "HEAD~1")
    _git(repo, "update-ref", "refs/remotes/origin/master", base_sha)
    ok = mod.run_tool_impl(origin, "restore_file",
                           {"path": "mod.txt", "from_base": True})
    assert ok.startswith("restored mod.txt from origin/master"), ok
    assert (repo / "mod.txt").read_text() == "BASE VERSION\n"
    assert _count(origin) == 1


@pytest.mark.parametrize("mod", MODULES, ids=MODULE_IDS)
def test_restore_cap_counts_both_modes(mod, tmp_path):
    repo = _make_repo(tmp_path)
    origin = {"CWD": repo}

    first = mod.run_tool_impl(origin, "restore_file", {"path": "mod.txt"})
    assert first.startswith("restored mod.txt to its last commit (HEAD)"), first
    assert _count(origin) == 1
    assert (repo / "mod.txt").read_text() == "BAD VERSION\n"

    second = mod.run_tool_impl(origin, "restore_file",
                               {"path": "mod.txt", "from_base": True})
    assert second.startswith("restored mod.txt from origin/master"), second
    assert _count(origin) == 2
    assert (repo / "mod.txt").read_text() == "BASE VERSION\n"

    third = mod.run_tool_impl(origin, "restore_file",
                              {"path": "mod.txt", "from_base": True})
    assert third.startswith("ERROR: restore_file refused"), third
    assert _count(origin) == 2
    assert (repo / "mod.txt").read_text() == "BASE VERSION\n"
