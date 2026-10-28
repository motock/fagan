"""Tests for the per-commit cache in front of the dispatch baseline snapshot.

``pipeline/dispatch.py`` calls ``_run_baseline_test_snapshot(worktree_path)``
for every fresh local-family dispatch, and that helper runs the repository's
full detected suite (bounded at 900 s) inside the scheduler pass. Fresh
worktrees start at the default branch's head, so concurrent and consecutive
dispatches across plans on one repository mostly snapshot the same commit: the
cache makes the suite run once per commit instead of once per dispatch.

The cache is a rename-and-delegate wrapper around
``_run_baseline_test_snapshot_uncached``: same return value, one INFO log line
on a hit, entries stored under ``<PLAN_DIR>/baseline_cache``.
"""
import hashlib
import json
import os
import stat
import subprocess
import threading
import time

# pipeline.server is imported explicitly first (pre-existing import cycle; see
# tests/unit/test_triage_cap_silent_skip.py for the same idiom).
import pipeline.server  # noqa: F401
from pipeline import dispatch_baseline as db

SENTINEL = {"exit_code": 0, "passed": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_repo(path):
    """Create a git repository with one committed file and return its path."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "tests").mkdir()
    (path / "tests" / "test_x.py").write_text("def test_ok():\n    assert True\n")
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    git = ["git", "-C", str(path), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    return path


def commit_new_file(repo, name="more.txt"):
    """Commit one more file so HEAD moves to a new sha."""
    (repo / name).write_text("more\n")
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", name], check=True)


def head_sha(repo):
    """The commit sha the wrapper keys its cache entry on."""
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def cache_file(plan_dir, repo):
    """The cache file the wrapper derives for ``repo`` at its current HEAD.

    Mirrors the documented key: the detected command, the worktree-relative
    test dir and the commit sha, hashed down to 32 hex characters.
    """
    payload = {"command": ["pytest", "-q"], "sha": head_sha(repo), "test_dir": "tests"}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]
    return plan_dir / "baseline_cache" / f"{digest}.json"


def patch_detect(monkeypatch):
    """Pin the detected test command so the cache key is deterministic."""
    monkeypatch.setattr(
        "pipeline.dispatch_baseline.detect_test_command",
        lambda wt: {"command": ["pytest", "-q"], "test_dir": str(wt / "tests")},
    )


def patch_uncached(monkeypatch, result, calls, delay=0.0):
    """Patch the uncached runner with a counting stub returning ``result``."""

    def stub(worktree_path):
        calls.append(worktree_path)
        if delay:
            time.sleep(delay)
        return None if result is None else dict(result)

    monkeypatch.setattr(
        "pipeline.dispatch_baseline._run_baseline_test_snapshot_uncached", stub
    )


# ---------------------------------------------------------------------------
# 1. Hit / miss behaviour
# ---------------------------------------------------------------------------
def test_same_head_runs_uncached_once(tmp_path, monkeypatch):
    """Two snapshots of the same commit run the suite once, both equal."""
    plan_dir = tmp_path / "plan"
    monkeypatch.setattr("pipeline.server.PLAN_DIR", plan_dir)
    patch_detect(monkeypatch)
    repo = make_repo(tmp_path / "wt")
    calls = []
    patch_uncached(monkeypatch, SENTINEL, calls)

    first = db._run_baseline_test_snapshot(repo)
    second = db._run_baseline_test_snapshot(repo)

    assert first == SENTINEL
    assert second == SENTINEL
    assert second == first
    assert len(calls) == 1
    assert cache_file(plan_dir, repo).exists()


def test_new_commit_misses(tmp_path, monkeypatch):
    """A different HEAD is a different key, so the suite runs again."""
    plan_dir = tmp_path / "plan"
    monkeypatch.setattr("pipeline.server.PLAN_DIR", plan_dir)
    patch_detect(monkeypatch)
    repo = make_repo(tmp_path / "wt")
    calls = []
    patch_uncached(monkeypatch, SENTINEL, calls)

    db._run_baseline_test_snapshot(repo)
    commit_new_file(repo)
    second = db._run_baseline_test_snapshot(repo)

    assert second == SENTINEL
    assert len(calls) == 2


def test_none_result_is_not_cached(tmp_path, monkeypatch):
    """A repository with no runnable suite must not poison the cache."""
    plan_dir = tmp_path / "plan"
    monkeypatch.setattr("pipeline.server.PLAN_DIR", plan_dir)
    patch_detect(monkeypatch)
    repo = make_repo(tmp_path / "wt")
    calls = []
    patch_uncached(monkeypatch, None, calls)

    assert db._run_baseline_test_snapshot(repo) is None
    assert db._run_baseline_test_snapshot(repo) is None

    assert len(calls) == 2
    assert not cache_file(plan_dir, repo).exists()


def test_missing_worktree_returns_none_without_running(tmp_path, monkeypatch):
    """A path that is not a directory is refused before any suite runs."""
    monkeypatch.setattr("pipeline.server.PLAN_DIR", tmp_path / "plan")
    patch_detect(monkeypatch)
    calls = []
    patch_uncached(monkeypatch, SENTINEL, calls)

    assert db._run_baseline_test_snapshot(tmp_path / "absent") is None
    assert calls == []
    assert not (tmp_path / "plan" / "baseline_cache").exists()


def test_corrupt_cache_file_is_overwritten(tmp_path, monkeypatch):
    """An unparseable entry is ignored, then replaced by a real one."""
    plan_dir = tmp_path / "plan"
    monkeypatch.setattr("pipeline.server.PLAN_DIR", plan_dir)
    patch_detect(monkeypatch)
    repo = make_repo(tmp_path / "wt")
    calls = []
    patch_uncached(monkeypatch, SENTINEL, calls)
    path = cache_file(plan_dir, repo)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")

    first = db._run_baseline_test_snapshot(repo)

    assert first == SENTINEL
    assert len(calls) == 1
    assert json.loads(path.read_text()) == SENTINEL

    assert db._run_baseline_test_snapshot(repo) == SENTINEL
    assert len(calls) == 1


def test_non_git_worktree_falls_through(tmp_path, monkeypatch):
    """Without a readable HEAD there is no key, so nothing is cached."""
    plan_dir = tmp_path / "plan"
    monkeypatch.setattr("pipeline.server.PLAN_DIR", plan_dir)
    patch_detect(monkeypatch)
    plain = tmp_path / "plain"
    (plain / "tests").mkdir(parents=True)
    calls = []
    patch_uncached(monkeypatch, SENTINEL, calls)

    assert db._run_baseline_test_snapshot(plain) == SENTINEL
    assert db._run_baseline_test_snapshot(plain) == SENTINEL

    assert len(calls) == 2
    assert (plan_dir / "baseline_cache").exists() is False


def test_hit_logs_short_sha_once(tmp_path, monkeypatch, caplog):
    """A hit logs exactly one INFO line naming the short sha, nothing else."""
    plan_dir = tmp_path / "plan"
    monkeypatch.setattr("pipeline.server.PLAN_DIR", plan_dir)
    patch_detect(monkeypatch)
    repo = make_repo(tmp_path / "wt")
    calls = []
    patch_uncached(monkeypatch, SENTINEL, calls)
    db._run_baseline_test_snapshot(repo)
    short = head_sha(repo)[:12]

    with caplog.at_level("INFO", logger="pipeline.dispatch_baseline"):
        caplog.clear()
        db._run_baseline_test_snapshot(repo)

    hits = [r for r in caplog.records if r.levelname == "INFO"]
    assert len(hits) == 1
    assert short in hits[0].getMessage()
    assert "hit=true" in hits[0].getMessage()
    assert str(plan_dir) not in hits[0].getMessage()


# ---------------------------------------------------------------------------
# 2. Permissions and pruning
# ---------------------------------------------------------------------------
def test_cache_dir_and_file_modes(tmp_path, monkeypatch):
    """The cache directory is private (0700) and the entry owner-only (0600)."""
    plan_dir = tmp_path / "plan"
    monkeypatch.setattr("pipeline.server.PLAN_DIR", plan_dir)
    patch_detect(monkeypatch)
    repo = make_repo(tmp_path / "wt")
    patch_uncached(monkeypatch, SENTINEL, [])

    db._run_baseline_test_snapshot(repo)

    path = cache_file(plan_dir, repo)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_stale_entry_pruned_on_miss_only(tmp_path, monkeypatch):
    """Misses drop entries older than 14 days; fresh ones survive."""
    plan_dir = tmp_path / "plan"
    monkeypatch.setattr("pipeline.server.PLAN_DIR", plan_dir)
    patch_detect(monkeypatch)
    repo = make_repo(tmp_path / "wt")
    calls = []
    patch_uncached(monkeypatch, SENTINEL, calls)
    cache_dir = plan_dir / "baseline_cache"
    cache_dir.mkdir(parents=True)
    old = cache_dir / "old.json"
    fresh = cache_dir / "fresh.json"
    old.write_text("{}")
    fresh.write_text("{}")
    now = time.time()
    os.utime(old, (now - 1_296_000, now - 1_296_000))  # 15 days
    os.utime(fresh, (now - 1_123_200, now - 1_123_200))  # 13 days

    db._run_baseline_test_snapshot(repo)

    assert len(calls) == 1
    assert old.exists() is False
    assert fresh.exists() is True
    assert cache_file(plan_dir, repo).exists()

    # A hit neither runs the suite nor prunes anything.
    db._run_baseline_test_snapshot(repo)
    assert len(calls) == 1
    assert old.exists() is False
    assert fresh.exists() is True


# ---------------------------------------------------------------------------
# 3. Concurrency
# ---------------------------------------------------------------------------
def test_concurrent_calls_run_suite_once(tmp_path, monkeypatch):
    """Concurrent dispatches of one commit serialise on the lock: one run."""
    plan_dir = tmp_path / "plan"
    monkeypatch.setattr("pipeline.server.PLAN_DIR", plan_dir)
    patch_detect(monkeypatch)
    repo = make_repo(tmp_path / "wt")
    calls = []
    patch_uncached(monkeypatch, SENTINEL, calls, delay=0.3)
    barrier = threading.Barrier(2)
    results = []
    elapsed = []

    def worker():
        barrier.wait()
        start = time.perf_counter()
        results.append(db._run_baseline_test_snapshot(repo))
        elapsed.append(time.perf_counter() - start)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert results == [SENTINEL, SENTINEL]
    assert min(elapsed) >= 0.25
