"""Tests for harness._tdd_diff: capture the agent's test/impl file changes
vs the seeded init commit so the scorecard can flag a T2 cell that fixes
the bug but skips writing the requested regression test
(project_t2_tdd_skip_finding.md).

Pure-function helper: takes paths and returns booleans. No dispatch, no
network. Uses real tmp git repos so the `git diff` path is exercised end-
to-end (a faked subprocess would let an off-by-one in argv construction
slip through).
"""
import subprocess

import harness as h


def _git(cwd, *argv, env=None):
    return subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True, env=env)


def _seed_repo(repo, impl_file: str, test_file: str) -> str:
    """Init a repo with the init commit, one impl file, one existing test
    file. Returns the init commit sha. Mirrors what setup_workspace would
    write for a T2 task with seed_files."""
    subprocess.run(["mkdir", "-p", str(repo)], check=True)
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "t")
    (repo / impl_file).parent.mkdir(parents=True, exist_ok=True)
    (repo / impl_file).write_text("# placeholder impl\n")
    (repo / test_file).parent.mkdir(parents=True, exist_ok=True)
    (repo / test_file).write_text("# existing tests\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_tdd_diff_impl_only_change_marks_impl_changed_test_unchanged(tmp_path):
    # The TDD-skip pattern: agent fixes ratelimiter.py and commits, but
    # does NOT add a regression test to test_ratelimiter.py. The diff
    # helper must report impl_changed=True, test_changed=False so the
    # scorecard can flag it as a TDD skip.
    impl = "ratelimiter.py"
    test = "test_ratelimiter.py"
    init_sha = _seed_repo(tmp_path, impl, test)
    (tmp_path / impl).write_text("# fixed impl\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "agent fix")

    impl_changed, test_changed = h._tdd_diff(
        tmp_path, init_sha, impl_file=impl, ecosystem="pytest",
    )

    assert impl_changed is True
    assert test_changed is False


def test_tdd_diff_test_only_change_marks_impl_unchanged_test_changed(tmp_path):
    # Symmetric case: agent adds a regression test without changing the
    # impl (e.g. test reveals a pre-existing bug already fixed by
    # adjacent code). impl_changed=False, test_changed=True.
    impl = "ratelimiter.py"
    test = "test_ratelimiter.py"
    init_sha = _seed_repo(tmp_path, impl, test)
    (tmp_path / test).write_text("# existing tests + regression\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "agent test")

    impl_changed, test_changed = h._tdd_diff(
        tmp_path, init_sha, impl_file=impl, ecosystem="pytest",
    )

    assert impl_changed is False
    assert test_changed is True


def test_tdd_diff_both_changed_marks_both_true(tmp_path):
    # The compliant T2 path: agent fixes the impl AND adds a regression
    # test. Both booleans True so the scorecard does NOT flag it.
    impl = "ratelimiter.py"
    test = "test_ratelimiter.py"
    init_sha = _seed_repo(tmp_path, impl, test)
    (tmp_path / impl).write_text("# fixed impl\n")
    (tmp_path / test).write_text("# existing tests + regression\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "agent fix + test")

    impl_changed, test_changed = h._tdd_diff(
        tmp_path, init_sha, impl_file=impl, ecosystem="pytest",
    )

    assert impl_changed is True
    assert test_changed is True


def test_tdd_diff_no_changes_marks_both_false(tmp_path):
    # An agent that parks without writing code must not be flagged as a
    # TDD-skip; the diff is empty.
    impl = "ratelimiter.py"
    test = "test_ratelimiter.py"
    init_sha = _seed_repo(tmp_path, impl, test)

    impl_changed, test_changed = h._tdd_diff(
        tmp_path, init_sha, impl_file=impl, ecosystem="pytest",
    )

    assert impl_changed is False
    assert test_changed is False


def test_tdd_diff_cargo_ecosystem_recognises_src_tests_test_rs(tmp_path):
    # cargo test convention: tests live under tests/ and Rust integration
    # tests use the suffix _test.rs (e.g. tests/test_acceptance.rs).
    # The helper must recognise this so a cargo T2 task is also gradable.
    impl = "src/lib.rs"
    test = "tests/acceptance_test.rs"
    init_sha = _seed_repo(tmp_path, impl, test)
    (tmp_path / impl).write_text("// fixed\n")
    (tmp_path / test).write_text("// + regression\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "agent fix + test")

    impl_changed, test_changed = h._tdd_diff(
        tmp_path, init_sha, impl_file=impl, ecosystem="cargo",
    )

    assert impl_changed is True
    assert test_changed is True


def test_tdd_diff_npm_ecosystem_recognises_test_dir(tmp_path):
    # npm `node --test` convention: tests under test/ ending in .test.js
    # (e.g. test/acceptance.test.js). The helper must recognise this for
    # npm T2 stories to be gradable.
    impl = "src/merge.js"
    test = "test/acceptance.test.js"
    init_sha = _seed_repo(tmp_path, impl, test)
    (tmp_path / impl).write_text("// fixed\n")
    (tmp_path / test).write_text("// + regression\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "agent fix + test")

    impl_changed, test_changed = h._tdd_diff(
        tmp_path, init_sha, impl_file=impl, ecosystem="npm",
    )

    assert impl_changed is True
    assert test_changed is True
