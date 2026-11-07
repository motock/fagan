"""Unit tests for :mod:`pipeline.evidence_checks`.

Every test builds a real, throwaway git repository under ``tmp_path``.  The
only stand-in is the ``git fetch`` remote: a second local bare repository, so
nothing here touches the network.

``check_merge_base``'s stale-ref regression test mutates and force-pushes the
"remote" so the worktree's local ``refs/remotes/origin/main`` goes stale; the
fetch inside the check must correct it, and the ref must stay corrected for a
follow-up ``fetch=False`` call.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from pipeline import evidence_checks as ec
from pipeline.forge import CISnapshot

GIT_IDENT = (
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@example.invalid",
    "-c",
    "commit.gpgsign=false",
)

EVIDENCE_KEYS = {"check", "passed", "ref", "detail", "sha256"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(cwd: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], check=False, cwd=str(cwd), capture_output=True, text=True
    )


def _git(cwd: Path | str, *args: str) -> str:
    proc = _run(cwd, *args)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    proc = _run(path, "init", "-b", "main")
    if proc.returncode != 0:  # older git without `init -b`
        proc = _run(path, "-c", "init.defaultBranch=main", "init")
    assert proc.returncode == 0, proc.stderr
    return path


def _init_bare(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    proc = _run(path, "init", "--bare", "-b", "main")
    if proc.returncode != 0:
        proc = _run(path, "-c", "init.defaultBranch=main", "init", "--bare")
    assert proc.returncode == 0, proc.stderr
    return path


def _write(repo: Path, rel: str, text: str) -> Path:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, *GIT_IDENT, "commit", "-m", message, "--allow-empty")
    return _git(repo, "rev-parse", "HEAD")


def _assert_evidence(result: dict, check: str) -> None:
    assert isinstance(result, dict)
    assert EVIDENCE_KEYS <= set(result)
    assert result["check"] == check
    assert isinstance(result["passed"], bool)
    assert isinstance(result["ref"], str)
    assert isinstance(result["detail"], str)
    assert len(result["detail"]) <= 200
    assert isinstance(result["sha256"], str)
    assert len(result["sha256"]) == 64


# ---------------------------------------------------------------------------
# check_merge_base
# ---------------------------------------------------------------------------


def _origin_seed_and_clone(tmp_path: Path):
    """Bare origin, seed clone with commits R and A pushed, worktree clone."""
    origin = _init_bare(tmp_path / "origin.git")
    seed = _init_repo(tmp_path / "seed")
    _write(seed, "app.txt", "R\n")
    root = _commit(seed, "R")
    _write(seed, "app.txt", "A\n")
    on_main = _commit(seed, "A")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")

    worktree = tmp_path / "worktree"
    cloned = subprocess.run(
        ["git", "clone", str(origin), str(worktree)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cloned.returncode == 0, cloned.stderr
    return origin, seed, worktree, root, on_main


def _feature_commit(worktree: Path) -> str:
    _git(worktree, "switch", "-c", "feature")
    _write(worktree, "feature.txt", "H\n")
    return _commit(worktree, "H")


def test_merge_base_with_fresh_remote(tmp_path: Path) -> None:
    _origin, _seed, worktree, _root, on_main = _origin_seed_and_clone(tmp_path)
    head = _feature_commit(worktree)

    result = ec.check_merge_base(
        str(worktree), head_sha=head, base_branch="main", fetch=True
    )

    _assert_evidence(result, "check_merge_base")
    assert result["passed"] is True
    assert result["base_sha"] == on_main
    assert result["ref"] == head
    assert f"base_sha={on_main}" in result["detail"]
    assert _git(worktree, "merge-base", head, "origin/main") == on_main


def test_merge_base_fetch_corrects_stale_origin_ref(tmp_path: Path) -> None:
    _origin, seed, worktree, root, _on_main = _origin_seed_and_clone(tmp_path)
    head = _feature_commit(worktree)

    # The remote main is rewritten to a commit that branches off R, so the
    # worktree's cached origin/main (still A) is stale.
    _git(seed, "reset", "--hard", root)
    _write(seed, "app.txt", "B\n")
    rewritten = _commit(seed, "B")
    _git(seed, "push", "--force", "origin", "main")

    stale = _git(worktree, "rev-parse", "origin/main")
    assert stale != rewritten
    # What the drifted, no-fetch path would wrongly report.
    assert _git(worktree, "merge-base", head, stale) == stale

    result = ec.check_merge_base(
        str(worktree), head_sha=head, base_branch="main", fetch=True
    )
    _assert_evidence(result, "check_merge_base")
    assert result["passed"] is True
    assert result["base_sha"] == root
    assert result["base_sha"] != stale
    assert f"base_sha={root}" in result["detail"]
    assert _git(worktree, "rev-parse", "origin/main") == rewritten

    # The successful fetch permanently advanced refs/remotes/origin/main.
    again = ec.check_merge_base(
        str(worktree), head_sha=head, base_branch="main", fetch=False
    )
    _assert_evidence(again, "check_merge_base")
    assert again["passed"] is True
    assert again["base_sha"] == root


def test_merge_base_fetch_failure_fails_closed(tmp_path: Path) -> None:
    _origin, _seed, worktree, _root, on_main = _origin_seed_and_clone(tmp_path)
    head = _feature_commit(worktree)
    # A usable stale local ref exists; it must NOT be used as a fallback.
    _git(worktree, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    assert _git(worktree, "rev-parse", "origin/main") == on_main

    result = ec.check_merge_base(
        str(worktree), head_sha=head, base_branch="main", fetch=True
    )

    _assert_evidence(result, "check_merge_base")
    assert result["passed"] is False
    assert result["base_sha"] == ""
    assert result["detail"]  # names the failure, never stderr content


# ---------------------------------------------------------------------------
# check_scope
# ---------------------------------------------------------------------------


def test_check_scope_passes_for_declared_and_test_paths() -> None:
    result = ec.check_scope(
        ["pipeline/a.py", "tests/unit/test_x.py"],
        ["pipeline/a.py"],
        {"pipeline", "tests"},
        test_globs=("tests/**",),
    )
    _assert_evidence(result, "check_scope")
    assert result["passed"] is True
    assert result["detail"] == "violations=0"


def test_check_scope_fails_for_undeclared_production_path() -> None:
    result = ec.check_scope(
        ["pipeline/a.py", "scripts/deploy.sh"],
        ["pipeline/a.py"],
        {"pipeline", "scripts"},
    )
    _assert_evidence(result, "check_scope")
    assert result["passed"] is False
    assert "violations=1" in result["detail"]
    assert "scripts/deploy.sh" in result["detail"]


def test_check_scope_truncates_many_violations() -> None:
    paths = [f"scripts/gen_{i}.sh" for i in range(12)]
    result = ec.check_scope(paths, [], {"scripts"})
    _assert_evidence(result, "check_scope")
    assert result["passed"] is False
    assert "violations=12" in result["detail"]
    assert len(result["detail"]) <= 200
    # at most the first five violating paths are named
    assert sum(1 for i in range(12) if f"gen_{i}.sh" in result["detail"]) <= 5


def test_check_scope_ref_names_the_base_top_level() -> None:
    result = ec.check_scope([], [], {"pipeline", "app"})
    _assert_evidence(result, "check_scope")
    assert result["passed"] is True
    assert "app" in result["ref"] and "pipeline" in result["ref"]


# ---------------------------------------------------------------------------
# check_ci_green
# ---------------------------------------------------------------------------


def test_ci_green_all_pass() -> None:
    result = ec.check_ci_green({"unit": "pass", "lint": "pass"}, "a" * 40)
    _assert_evidence(result, "check_ci_green")
    assert result["passed"] is True
    assert result["detail"] == "pass=2"


def test_ci_green_accepts_cisnapshot_and_checks_object() -> None:
    snapshot = CISnapshot({"unit": "pass"})
    assert ec.check_ci_green(snapshot, "a" * 40)["passed"] is True

    class _HasChecks:
        def __init__(self) -> None:
            self.checks = {"unit": "pass", "lint": "pass"}

    result = ec.check_ci_green(_HasChecks(), "a" * 40)
    assert result["passed"] is True
    assert result["detail"] == "pass=2"


def test_ci_green_one_failure() -> None:
    result = ec.check_ci_green({"unit": "pass", "lint": "fail"}, "a" * 40)
    _assert_evidence(result, "check_ci_green")
    assert result["passed"] is False
    assert result["detail"] == "fail=1,pass=1"


def test_ci_green_empty_snapshot_is_not_green() -> None:
    result = ec.check_ci_green({}, "a" * 40)
    _assert_evidence(result, "check_ci_green")
    assert result["passed"] is False
    assert result["detail"] == "total=0"


def test_ci_green_none_state_fails() -> None:
    result = ec.check_ci_green({"unit": "pass", "lint": "none"}, "a" * 40)
    _assert_evidence(result, "check_ci_green")
    assert result["passed"] is False
    assert result["detail"] == "none=1,pass=1"


def test_ci_green_unreadable_state_fails() -> None:
    result = ec.check_ci_green({"unit": "unreadable"}, "a" * 40)
    _assert_evidence(result, "check_ci_green")
    assert result["passed"] is False
    assert result["detail"] == "unreadable=1"


# ---------------------------------------------------------------------------
# check_reviewer_approve
# ---------------------------------------------------------------------------


def test_reviewer_approve_matching_sha() -> None:
    head = "a" * 40
    result = ec.check_reviewer_approve(
        {"review_verdict": "APPROVE", "last_reviewed_sha": head}, head
    )
    _assert_evidence(result, "check_reviewer_approve")
    assert result["passed"] is True
    assert result["verdict_trust"] == "unverified"


def test_reviewer_approve_different_sha_fails() -> None:
    result = ec.check_reviewer_approve(
        {"review_verdict": "APPROVE", "last_reviewed_sha": "b" * 40}, "a" * 40
    )
    _assert_evidence(result, "check_reviewer_approve")
    assert result["passed"] is False
    assert result["verdict_trust"] == "unverified"


def test_reviewer_request_changes_fails() -> None:
    head = "a" * 40
    result = ec.check_reviewer_approve(
        {"review_verdict": "REQUEST_CHANGES", "last_reviewed_sha": head}, head
    )
    _assert_evidence(result, "check_reviewer_approve")
    assert result["passed"] is False


def test_reviewer_missing_keys_fails_without_raising() -> None:
    result = ec.check_reviewer_approve({}, "a" * 40)
    _assert_evidence(result, "check_reviewer_approve")
    assert result["passed"] is False
    assert result["verdict_trust"] == "unverified"

    partial = ec.check_reviewer_approve({"review_verdict": "APPROVE"}, "a" * 40)
    assert partial["passed"] is False


# ---------------------------------------------------------------------------
# git-backed checks 5-7: repository fixtures
# ---------------------------------------------------------------------------

BASE_OK = """\
def test_one():
    assert 1 == 1
    assert 2 == 2


def test_two():
    assert 3 == 3
    assert 4 == 4
"""


def _base_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "tests/unit/test_a.py", BASE_OK)
    _write(
        repo,
        "tests/unit/test_b.py",
        "def test_b():\n    assert True\n",
    )
    return repo, _commit(repo, "base")


# ---------------------------------------------------------------------------
# check_no_test_deleted
# ---------------------------------------------------------------------------


def test_no_test_deleted_passes_when_tests_survive(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path)
    _write(repo, "tests/unit/test_a.py", BASE_OK + "\n\ndef test_three():\n    assert True\n")
    head = _commit(repo, "head")

    result = ec.check_no_test_deleted(str(repo), base, head)
    _assert_evidence(result, "check_no_test_deleted")
    assert result["passed"] is True
    assert result["detail"] == "deleted_tests=0"


def test_no_test_deleted_fails_on_deletion(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path)
    _git(repo, "rm", "-q", "tests/unit/test_b.py")
    head = _commit(repo, "delete a test")

    result = ec.check_no_test_deleted(str(repo), base, head)
    _assert_evidence(result, "check_no_test_deleted")
    assert result["passed"] is False
    assert "tests/unit/test_b.py" in result["detail"]
    assert "deleted_tests=1" in result["detail"]


def test_no_test_deleted_fails_on_rename(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path)
    _git(repo, "mv", "tests/unit/test_a.py", "tests/unit/test_moved.py")
    head = _commit(repo, "rename a test")

    result = ec.check_no_test_deleted(str(repo), base, head)
    _assert_evidence(result, "check_no_test_deleted")
    # --no-renames reports the move as D + A; conservatively a deletion.
    assert result["passed"] is False
    assert "tests/unit/test_a.py" in result["detail"]


# ---------------------------------------------------------------------------
# check_assertions_not_reduced
# ---------------------------------------------------------------------------


def test_assertions_removed_fails(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path)
    _write(
        repo,
        "tests/unit/test_a.py",
        "def test_one():\n    assert 1 == 1\n",
    )
    head = _commit(repo, "remove assertions")

    result = ec.check_assertions_not_reduced(str(repo), base, head)
    _assert_evidence(result, "check_assertions_not_reduced")
    assert result["passed"] is False


def test_assertions_added_passes(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path)
    _write(repo, "tests/unit/test_a.py", BASE_OK + "\n\ndef test_extra():\n    assert True\n")
    head = _commit(repo, "add assertions")

    result = ec.check_assertions_not_reduced(str(repo), base, head)
    _assert_evidence(result, "check_assertions_not_reduced")
    assert result["passed"] is True


def test_assertions_moved_between_files_fails_per_file(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path)
    # base: test_a.py=4, test_b.py=1.  head: test_a.py=2, test_b.py=3.
    _write(
        repo,
        "tests/unit/test_a.py",
        "def test_one():\n    assert 1 == 1\n\n\ndef test_two():\n    assert 2 == 2\n",
    )
    _write(
        repo,
        "tests/unit/test_b.py",
        "def test_b():\n    assert True\n    assert 1 == 1\n    assert 2 == 2\n",
    )
    head = _commit(repo, "move assertions")

    result = ec.check_assertions_not_reduced(str(repo), base, head)
    _assert_evidence(result, "check_assertions_not_reduced")
    # Totals are equal (5 == 5) but per-file test_a.py dropped 4 -> 2.
    assert result["passed"] is False
    assert "tests/unit/test_a.py" in result["detail"]


def test_assertions_counts_self_assert_methods(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(
        repo,
        "tests/unit/test_case.py",
        (
            "class TestCase:\n"
            "    def test_one(self):\n"
            "        self.assertEqual(1, 1)\n"
        ),
    )
    base = _commit(repo, "base")
    _write(repo, "tests/unit/test_case.py", "class TestCase:\n    def test_one(self):\n        pass\n")
    head = _commit(repo, "drop assertion")

    result = ec.check_assertions_not_reduced(str(repo), base, head)
    assert result["passed"] is False


def test_assertions_syntax_error_at_head_fails_closed(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path)
    _write(repo, "tests/unit/test_a.py", "def test_one(:\n    assert True\n")
    head = _commit(repo, "broken")

    result = ec.check_assertions_not_reduced(str(repo), base, head)
    _assert_evidence(result, "check_assertions_not_reduced")
    assert result["passed"] is False
    assert result["detail"].startswith("unsupported_or_unparseable:")


def test_assertions_non_python_test_fails_closed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "tests/unit/test_widget.js", "test('one', () => { expect(1).toBe(1); });\n")
    base = _commit(repo, "base")
    _write(repo, "tests/unit/test_widget.js", "test('one', () => {});\n")
    head = _commit(repo, "head")

    result = ec.check_assertions_not_reduced(str(repo), base, head)
    _assert_evidence(result, "check_assertions_not_reduced")
    assert result["passed"] is False
    assert result["detail"] == "unsupported_or_unparseable:1"


# ---------------------------------------------------------------------------
# check_nothing_skipped
# ---------------------------------------------------------------------------

BASE_SKIPS = """\
import pytest


@pytest.mark.skip(reason="later")
def test_one():
    assert True


def test_two():
    pytest.skip("later")
"""

HEAD_SKIPS_THREE = """\
import pytest


@pytest.mark.skip(reason="later")
def test_one():
    assert True


@pytest.mark.skipif(True, reason="x")
def test_two():
    pytest.skip("later")
"""

HEAD_NO_SKIPS = """\
def test_one():
    assert True


def test_two():
    assert True
"""


def _skip_repo(tmp_path: Path, head_text: str) -> tuple[Path, str, str]:
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "tests/unit/test_a.py", BASE_SKIPS)
    base = _commit(repo, "base")
    _write(repo, "tests/unit/test_a.py", head_text)
    head = _commit(repo, "head")
    return repo, base, head


def test_skips_added_fails(tmp_path: Path) -> None:
    repo, base, head = _skip_repo(tmp_path, HEAD_SKIPS_THREE)
    result = ec.check_nothing_skipped(str(repo), base, head)
    _assert_evidence(result, "check_nothing_skipped")
    assert result["passed"] is False
    assert "tests/unit/test_a.py" in result["detail"]


def test_skips_unchanged_passes(tmp_path: Path) -> None:
    repo, base, head = _skip_repo(tmp_path, BASE_SKIPS)
    result = ec.check_nothing_skipped(str(repo), base, head)
    _assert_evidence(result, "check_nothing_skipped")
    assert result["passed"] is True


def test_skips_removed_passes(tmp_path: Path) -> None:
    repo, base, head = _skip_repo(tmp_path, HEAD_NO_SKIPS)
    result = ec.check_nothing_skipped(str(repo), base, head)
    _assert_evidence(result, "check_nothing_skipped")
    assert result["passed"] is True


def test_skips_syntax_error_fails_closed(tmp_path: Path) -> None:
    repo, base, head = _skip_repo(tmp_path, "def test_one(:\n    pass\n")
    result = ec.check_nothing_skipped(str(repo), base, head)
    _assert_evidence(result, "check_nothing_skipped")
    assert result["passed"] is False
    assert result["detail"].startswith("unsupported_or_unparseable:")


# ---------------------------------------------------------------------------
# detail never leaks file contents
# ---------------------------------------------------------------------------

SENTINEL = "SENTINEL_DO_NOT_LEAK_4f2a"


def test_detail_never_contains_file_contents(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path)
    body = (
        "def test_sentinel():\n"
        f'    secret = "{SENTINEL}"\n'
        "    assert secret\n"
    )
    _write(repo, "tests/unit/test_sentinel.py", body)
    _git(repo, "add", "-A")
    _git(repo, *GIT_IDENT, "commit", "-m", "add sentinel")
    base = _git(repo, "rev-parse", "HEAD")
    _write(
        repo,
        "tests/unit/test_sentinel.py",
        body + "\n\ndef test_more():\n    assert True\n",
    )
    head = _commit(repo, "head")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)

    results = [
        ec.check_merge_base(str(repo), head, "main", fetch=False),
        ec.check_scope(["tests/unit/test_sentinel.py"], [], {"tests"}),
        ec.check_ci_green({"unit": "fail"}, head),
        ec.check_reviewer_approve(
            {"review_verdict": "REQUEST_CHANGES", "last_reviewed_sha": "b" * 40},
            head,
        ),
        ec.check_no_test_deleted(str(repo), base, head),
        ec.check_assertions_not_reduced(str(repo), base, head),
        ec.check_nothing_skipped(str(repo), base, head),
    ]
    for result in results:
        _assert_evidence(result, result["check"])
        assert SENTINEL not in json.dumps(result)


# ---------------------------------------------------------------------------
# sha256 recomputation
# ---------------------------------------------------------------------------


def test_sha256_matches_independent_recomputation() -> None:
    result = ec.check_scope(
        ["pipeline/a.py", "scripts/deploy.sh"], ["pipeline/a.py"], {"pipeline"}
    )
    payload = {
        "check": result["check"],
        "passed": result["passed"],
        "ref": result["ref"],
        "detail": result["detail"],
    }
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert result["sha256"] == expected


def test_sha256_covers_truncated_detail_only() -> None:
    long_paths = [f"scripts/very_long_generated_name_number_{i:03d}.sh" for i in range(20)]
    result = ec.check_scope(long_paths, [], {"scripts"})
    assert len(result["detail"]) == 200
    payload = {
        "check": result["check"],
        "passed": result["passed"],
        "ref": result["ref"],
        "detail": result["detail"],
    }
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert result["sha256"] == expected
