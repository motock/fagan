"""P0-MOOT-01: a prior review finding whose file no longer differs from the
base branch is moot, not unaddressed.

The APPROVE guard in ``pipeline/review_orchestrator.py`` downgrades an APPROVE
to REQUEST_CHANGES when a previously flagged non-test file is absent from
``git diff --name-only <last_reviewed_sha> HEAD``. A file that is byte-identical
to the base branch is objective proof the finding is resolved (there is nothing
left to change in it), exactly like the existing green-suite exemption for test
files. These tests drive ``_unaddressed_findings`` against a real temporary git
repo, following the pattern in ``tests/unit/test_branch_namespace.py``.
"""
import subprocess

from pipeline.review_orchestrator import _unaddressed_findings


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _commit(repo, message):
    _run(["git", "add", "-A"], repo)
    _run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", message],
        repo,
    )


def _repo(tmp_path, with_origin=True):
    """A worktree whose base branch resolves to ``main`` (or not, if no origin)."""
    repo = tmp_path / "repo"
    _run(["git", "init", "-q", "-b", "main", str(repo)], tmp_path)
    (repo / "README.md").write_text("seed\n")
    _commit(repo, "init")
    if with_origin:
        origin = tmp_path / "origin.git"
        _run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], tmp_path)
        _run(["git", "remote", "add", "origin", str(origin)], repo)
        _run(["git", "push", "-q", "-u", "origin", "main"], repo)
        # ``git remote add`` + push does not create refs/remotes/origin/HEAD;
        # _resolve_base_branch(worktree, "") reads exactly that ref.
        _run(["git", "remote", "set-head", "origin", "main"], repo)
    return repo


def test_touched_file_is_not_unaddressed(tmp_path):
    repo = _repo(tmp_path)
    assert _unaddressed_findings(["src/foo.py"], {"src/foo.py"}, str(repo)) == []


def test_untouched_file_differing_from_base_is_unaddressed(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "foo.py").write_text("base\n")
    _commit(repo, "add foo")
    _run(["git", "push", "-q", "origin", "main"], repo)
    (repo / "src" / "foo.py").write_text("changed\n")
    _commit(repo, "change foo")

    assert _unaddressed_findings(["src/foo.py"], set(), str(repo)) == ["src/foo.py"]


def test_untouched_file_identical_to_base_is_not_unaddressed(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "foo.py").write_text("same\n")
    _commit(repo, "add foo")
    _run(["git", "push", "-q", "origin", "main"], repo)

    assert _unaddressed_findings(["src/foo.py"], set(), str(repo)) == []


def test_untouched_test_file_is_not_unaddressed(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "test_foo.py").write_text("base\n")
    _commit(repo, "add test")
    _run(["git", "push", "-q", "origin", "main"], repo)
    (repo / "tests" / "unit" / "test_foo.py").write_text("changed\n")
    _commit(repo, "change test")

    assert _unaddressed_findings(["tests/unit/test_foo.py"], set(), str(repo)) == []


def test_unresolvable_base_leaves_finding_unaddressed(tmp_path):
    repo = _repo(tmp_path, with_origin=False)
    (repo / "src").mkdir()
    (repo / "src" / "foo.py").write_text("same\n")
    _commit(repo, "add foo")

    assert _unaddressed_findings(["src/foo.py"], set(), str(repo)) == ["src/foo.py"]


def test_nonexistent_path_is_unaddressed_not_exempt(tmp_path):
    repo = _repo(tmp_path)

    assert _unaddressed_findings(["src/missing.py"], set(), str(repo)) == [
        "src/missing.py"
    ]
