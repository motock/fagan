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
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import threading
import time

import pytest

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


# ---------------------------------------------------------------------------
# 4. The single write path for concurrently-writable story fields
#
# ``story["baseline_test_check"]`` was lost five times across three paths in
# the b3+b4 runs (2026-11-09/10) and every loss needed a manual
# pause/edit/resume to restore the cached baseline:
#
#   * Path A - a redispatch onto a worktree that already carries the
#     ``.dispatch_baseline_test_checked`` marker recorded NOTHING, because the
#     recording was guarded on ``not baseline_marker.exists()``.
#   * Path B - the dispatch runs with the plan lock released, so its manifest
#     write races a ``patch_story`` transaction read and the later
#     whole-manifest write persists a copy that PREDATES the baseline.
#   * Path C - a grade write does the same.
#
# The fix is a single write path, ``pipeline.story_fields.apply_story_fields``:
# take the plan lock, RE-READ the manifest inside it, merge only the given
# fields into the fresh story dict, save, and return the fresh story. These
# tests drive that public helper.
# ---------------------------------------------------------------------------
def _write_manifest(plan_dir, plan_name, stories):
    """Write a minimal manifest the way the pipeline lays one out on disk."""
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _manifest_bytes(plan_dir, plan_name):
    return (plan_dir / f"{plan_name}.manifest.json").read_bytes()


def _manifest(plan_dir, plan_name):
    return json.loads(_manifest_bytes(plan_dir, plan_name))


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point PLAN_DIR at a throwaway dir for every module that resolves it."""
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers
    from pipeline import server as p
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _store():
    """A real FileStore - the public Store API the helper is handed."""
    from pipeline.store import FileStore

    return FileStore()


def _apply_story_fields(*args, **kwargs):
    """Call the helper under test, imported lazily so collection still works
    before ``pipeline/story_fields.py`` exists."""
    from pipeline.story_fields import apply_story_fields

    return apply_story_fields(*args, **kwargs)


def _story(**overrides):
    story = {
        "summary": "Do thing",
        "agent_instructions": "Build it.",
        "status": "todo",
        "dependencies": [],
    }
    story.update(overrides)
    return story


BASELINE = {"returncode": 1, "failed_node_ids": ["tests/test_a.py::test_one"]}


def test_apply_story_fields_merges_only_the_given_fields(plan_dir):
    """The happy path: the given fields land on the story, every pre-existing
    field survives, and no other story in the manifest is touched."""
    _write_manifest(
        plan_dir,
        "sfmerge",
        {"S1": _story(), "S2": _story(summary="Other")},
    )
    store = _store()

    fresh = _apply_story_fields(store, "sfmerge", "S1", {"baseline_test_check": BASELINE})

    assert fresh is not None
    assert fresh["baseline_test_check"] == BASELINE
    # The pre-existing fields are still there: only ``fields`` was merged.
    assert fresh["summary"] == "Do thing"
    assert fresh["agent_instructions"] == "Build it."
    assert fresh["status"] == "todo"
    assert set(fresh) == set(_story()) | {"baseline_test_check"}

    on_disk = _manifest(plan_dir, "sfmerge")
    assert on_disk["stories"]["S1"]["baseline_test_check"] == BASELINE
    # The sibling story is untouched.
    assert on_disk["stories"]["S2"] == _story(summary="Other")


def test_apply_story_fields_re_reads_the_manifest_inside_the_lock(plan_dir):
    """The manifest is re-read INSIDE the lock, so a field another writer
    landed between the caller's read and the lock is not lost (Path B).

    A helper that merged into the caller's pre-lock copy would drop the
    concurrently written field; a helper that re-reads keeps both.
    """
    _write_manifest(plan_dir, "sfrace", {"S1": _story()})
    store = _store()

    # The caller's pre-lock copy, and a concurrent writer landing on top of
    # it while the caller was not yet holding the lock.
    stale = store.get_manifest("sfrace")
    assert "concurrent_field" not in stale["stories"]["S1"]
    concurrent = store.get_manifest("sfrace")
    concurrent["stories"]["S1"]["concurrent_field"] = "written-by-someone-else"
    store.save_manifest("sfrace", concurrent)

    fresh = _apply_story_fields(
        store, "sfrace", "S1", {"baseline_test_check": BASELINE}
    )

    assert fresh is not None
    assert fresh["concurrent_field"] == "written-by-someone-else"
    assert fresh["baseline_test_check"] == BASELINE
    on_disk = _manifest(plan_dir, "sfrace")
    assert on_disk["stories"]["S1"]["concurrent_field"] == "written-by-someone-else"
    assert on_disk["stories"]["S1"]["baseline_test_check"] == BASELINE


def test_apply_story_fields_returns_none_when_the_lock_is_unavailable(plan_dir):
    """When the plan lock cannot be acquired the helper writes NOTHING and
    returns None - the manifest on disk stays byte-identical."""
    _write_manifest(plan_dir, "sflocked", {"S1": _story()})
    store = _store()
    before = _manifest_bytes(plan_dir, "sflocked")

    # Take the plan's lock file with a raw flock. The pipeline's own lock
    # machinery only grants a same-process grace retry to locks IT took, so a
    # raw holder makes the transaction fail to acquire deterministically.
    lock_path = plan_dir / "sflocked.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _apply_story_fields(
            store, "sflocked", "S1", {"baseline_test_check": BASELINE}
        )
    finally:
        os.close(fd)

    assert result is None
    assert _manifest_bytes(plan_dir, "sflocked") == before
    assert "baseline_test_check" not in _manifest(plan_dir, "sflocked")["stories"]["S1"]


def test_apply_story_fields_keeps_both_an_interleaved_baseline_and_patch(plan_dir):
    """The b4 race: a baseline write and a patch_story-style write, interleaved
    on the same plan, must BOTH survive in the final manifest."""
    _write_manifest(plan_dir, "sfinterleaved", {"S1": _story()})
    store = _store()

    # A reader that took its copy before either write landed - the shape of
    # the dispatch's own unlocked manifest write.
    stale = store.get_manifest("sfinterleaved")

    _apply_story_fields(store, "sfinterleaved", "S1", {"baseline_test_check": BASELINE})
    _apply_story_fields(
        store, "sfinterleaved", "S1", {"agent_instructions": "Patched by the operator"}
    )

    on_disk = _manifest(plan_dir, "sfinterleaved")["stories"]["S1"]
    assert on_disk["baseline_test_check"] == BASELINE
    assert on_disk["agent_instructions"] == "Patched by the operator"
    # The stale pre-lock copy never carried either field.
    assert "baseline_test_check" not in stale["stories"]["S1"]
    assert stale["stories"]["S1"]["agent_instructions"] == "Build it."


def test_apply_story_fields_grade_result_write_preserves_the_baseline(plan_dir):
    """A grade-result write must not drop the recorded baseline (Path C): the
    helper merges into the fresh story, so the baseline survives."""
    _write_manifest(plan_dir, "sfgrade", {"S1": _story()})
    store = _store()

    _apply_story_fields(store, "sfgrade", "S1", {"baseline_test_check": BASELINE})
    _apply_story_fields(
        store,
        "sfgrade",
        "S1",
        {
            "status": "failed",
            "last_test_check": {"returncode": 1, "failed_node_ids": ["tests/test_a.py::test_one"]},
        },
    )

    on_disk = _manifest(plan_dir, "sfgrade")["stories"]["S1"]
    assert on_disk["baseline_test_check"] == BASELINE
    assert on_disk["status"] == "failed"
    assert on_disk["last_test_check"]["returncode"] == 1


def test_apply_story_fields_forwards_an_explicit_wait_seconds(plan_dir):
    """An explicit ``wait_seconds`` is forwarded to the store's transaction."""
    _write_manifest(plan_dir, "sfwait", {"S1": _story()})

    class _RecordingStore:
        """A FileStore that remembers the wait budget it was handed."""

        def __init__(self):
            from pipeline.store import FileStore

            self._inner = FileStore()
            self.seen = []

        def transaction(self, plan_name, wait_seconds=0.0):
            self.seen.append(wait_seconds)
            return self._inner.transaction(plan_name, wait_seconds)

        def get_manifest(self, plan_name):
            return self._inner.get_manifest(plan_name)

        def save_manifest(self, plan_name, manifest):
            return self._inner.save_manifest(plan_name, manifest)

    store = _RecordingStore()
    result = _apply_story_fields(
        store, "sfwait", "S1", {"baseline_test_check": BASELINE}, wait_seconds=3.5
    )

    assert result is not None
    assert store.seen == [3.5]
    assert _manifest(plan_dir, "sfwait")["stories"]["S1"]["baseline_test_check"] == BASELINE


def test_apply_story_fields_default_wait_seconds_still_writes(plan_dir):
    """The default ``wait_seconds=None`` must still take the lock and write -
    it is the call shape the dispatch uses."""
    _write_manifest(plan_dir, "sfdefault", {"S1": _story()})
    store = _store()

    result = _apply_story_fields(store, "sfdefault", "S1", {"baseline_test_check": BASELINE})

    assert result is not None
    assert result["baseline_test_check"] == BASELINE
    assert _manifest(plan_dir, "sfdefault")["stories"]["S1"]["baseline_test_check"] == BASELINE


def test_apply_story_fields_on_a_missing_story_leaves_the_manifest_untouched(plan_dir):
    """A story that does not exist must not be conjured, and must not leave a
    half-written manifest behind."""
    from pipeline.story_fields import apply_story_fields

    _write_manifest(plan_dir, "sfmissing", {"S1": _story()})
    store = _store()
    before = _manifest_bytes(plan_dir, "sfmissing")

    try:
        result = apply_story_fields(
            store, "sfmissing", "NOPE", {"baseline_test_check": BASELINE}
        )
    except (LookupError, ValueError):
        # The error shape for an unknown story is the caller's business; what
        # matters is that nothing was written.
        result = None

    assert result is None
    assert _manifest_bytes(plan_dir, "sfmissing") == before
