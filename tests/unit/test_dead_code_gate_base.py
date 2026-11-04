"""Dead-code gate: the diff base must be the fetched origin base.

The gate used to diff against the *local* `base_branch`. In a worktree whose
local `master` was created before other stories merged upstream, everything
those stories merged since then looks like it was added by the current
story - and their helpers get reported as this story's dead code (NTF-03).

So: if `origin/<base_branch>` resolves in the worktree, diff against that
instead. No fetch is performed here (the dispatch has already fetched), and
a missing origin remote keeps today's local-base behaviour.
"""

import subprocess

from pipeline import server as p

# ---------------------------------------------------------------------------
# Helpers (copied from tests/unit/test_dead_code_gate.py; `_git` returns its
# CompletedProcess here so tests can read rev-parse output)
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _init_repo_with_feature_branch(tmp_path, base_content, feature_content,
                                    base_filename="mod.py",
                                    extra_feature_files=None):
    """A repo with `base_content` committed on master, then a `feature`
    branch (checked out from master) adding `feature_content` as a new
    commit - the shape check_story_status's real callers operate on: a
    story branch that diverged from the default branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "master", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / base_filename).write_text(base_content)
    _git("add", base_filename, cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)

    _git("checkout", "-q", "-b", "feature", cwd=repo)
    (repo / base_filename).write_text(feature_content)
    files_to_add = [base_filename]
    for name, content in (extra_feature_files or {}).items():
        (repo / name).write_text(content)
        files_to_add.append(name)
    _git("add", *files_to_add, cwd=repo)
    _git("commit", "-q", "-m", "feature work", cwd=repo)
    return repo


# ---------------------------------------------------------------------------
# The stale-local-master shape
# ---------------------------------------------------------------------------

_APP_PY = "def used_entry():\n    return 1\n"
_UPSTREAM_PY = "def merged_upstream_helper():\n    return 2\n"
_MOD_PY = (
    "def story_entry():\n"
    "    return used_by_mod()\n\n\n"
    "def used_by_mod():\n"
    "    return 1\n\n\n"
    "def story_dead_helper():\n"
    "    return 0\n"
)


def _build_clone_with_stale_master(tmp_path):
    """A clone whose local `master` is behind `origin/master`.

    `origin/master` gains `upstream.py` from another story *after* the clone
    was made; the clone fetches but never advances its local `master`. The
    story branch is cut from `origin/master` and adds `mod.py`, which holds
    one used and one genuinely dead helper.
    """
    bare = tmp_path / "origin.git"
    _git("init", "--bare", "-q", "-b", "master", bare.as_posix(), cwd=tmp_path)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "master", cwd=seed)
    _git("config", "user.email", "t@t", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "app.py").write_text(_APP_PY)
    _git("add", "app.py", cwd=seed)
    _git("commit", "-q", "-m", "c0", cwd=seed)
    _git("remote", "add", "origin", bare.as_posix(), cwd=seed)
    _git("push", "-q", "origin", "master", cwd=seed)

    clone = tmp_path / "clone"
    _git("clone", "-q", bare.as_posix(), clone.as_posix(), cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)

    # Another story merges upstream: origin/master moves, local master does not.
    _git("checkout", "-q", "-b", "tmp-upstream", "master", cwd=clone)
    (clone / "upstream.py").write_text(_UPSTREAM_PY)
    _git("add", "upstream.py", cwd=clone)
    _git("commit", "-q", "-m", "c1", cwd=clone)
    _git("push", "-q", "origin", "tmp-upstream:master", cwd=clone)
    _git("checkout", "-q", "master", cwd=clone)

    _git("fetch", "-q", "origin", cwd=clone)
    _git("checkout", "-q", "-b", "feature/story", "origin/master", cwd=clone)
    (clone / "mod.py").write_text(_MOD_PY)
    _git("add", "mod.py", cwd=clone)
    _git("commit", "-q", "-m", "c2", cwd=clone)
    return clone


def test_upstream_merged_function_not_reported(tmp_path):
    """A helper that another story already merged upstream must not be
    attributed to this story just because local `master` is stale."""
    clone = _build_clone_with_stale_master(tmp_path)
    stale = _git("rev-parse", "master", cwd=clone).stdout.strip()

    got1 = p._find_dead_new_functions(clone, "master")
    got2 = p._find_dead_new_functions(clone, "master")

    assert not any("upstream.py:merged_upstream_helper" in item for item in got1)
    assert got1 == got2
    assert _git("rev-parse", "master", cwd=clone).stdout.strip() == stale


def test_genuinely_dead_story_function_still_reported(tmp_path):
    """Negative case: resolving the base to origin/master must not blunt the
    gate - a real dead new function from the story is still reported."""
    clone = _build_clone_with_stale_master(tmp_path)

    got = p._find_dead_new_functions(clone, "master")

    assert any("mod.py:story_dead_helper" in item for item in got)
    assert not any("mod.py:used_by_mod" in item for item in got)


def test_no_origin_remote_keeps_local_base_behaviour(tmp_path):
    """No origin remote: `origin/master` cannot resolve, so the local base
    branch is used exactly as before (and the gate must not crash)."""
    repo = _init_repo_with_feature_branch(
        tmp_path,
        base_content=_APP_PY,
        feature_content=_MOD_PY,
    )

    got = p._find_dead_new_functions(repo, "master")

    assert any("mod.py:story_dead_helper" in item for item in got)
    assert not any("app.py:used_entry" in item for item in got)


def test_base_branch_already_origin_prefixed_is_used_as_is(tmp_path):
    """A caller that already passes `origin/master` must not be prefixed
    again into `origin/origin/master`."""
    clone = _build_clone_with_stale_master(tmp_path)

    got = p._find_dead_new_functions(clone, "origin/master")

    assert not any("upstream.py:merged_upstream_helper" in item for item in got)
    assert any("mod.py:story_dead_helper" in item for item in got)
