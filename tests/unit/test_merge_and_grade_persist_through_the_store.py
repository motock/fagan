"""Merge outcomes and detached-grade transitions are persisted through the store.

``pipeline/merge.py``'s two manifest writes inside ``_approve_merge_impl``'s
``_store.transaction`` (the CI-gate result and the final "done" write) and
``pipeline/detached_grade.py``'s two state transitions (grade started, grade
result collected) used to call ``_atomic_write_json(manifest_path, manifest)``
directly. They now go through
``records().put_manifest("default", plan_name, manifest)`` at the same point in
the same lock scope, so a fake store alone is enough to observe the write - and
to prove that a busy lock or a rejected plan name writes nothing at all.

The grade tests pin the state machine the record store now owns:
``queued/0 -> grading/1 -> graded/1``, with the second write reading the state
the first one landed rather than a copy captured before it.

Under ``PIPELINE_STORE=sqlite`` the old sites wrote the file projection only,
bypassing the database row; going through ``put_manifest`` writes the row (the
file store projects it back to ``<plan>.manifest.json``), so the on-disk layout
is unchanged.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import pipeline.server as p
from pipeline import detached_grade, merge
from pipeline.store_records import InvalidRecordKeyError, validate_plan_name


class _FakeStore:
    """A store whose manifests live in one dict, recording every write.

    ``get_manifest`` validates the plan name and hands back an independent
    deep copy (``None`` when absent), ``put_manifest`` records and stores a deep
    copy, and ``get_manifests`` returns a shared read-only snapshot that a
    writer must never feed back into ``put_manifest``.
    """

    def __init__(self, plan_dir: Path) -> None:
        self.plan_dir = plan_dir
        self.data: dict[str, dict] = {}
        self.writes: list[tuple[str, str, dict]] = []
        self.reads: list[tuple[str, str]] = []
        self.bulk_reads = 0
        self.acquired = True
        self._snapshot_cache: dict[str, dict] = {}

    # -- Store surface the migrated sites touch ------------------------
    def manifest_path(self, plan_name):
        return self.plan_dir / f"{plan_name}.manifest.json"

    def records(self):
        return self

    def get_manifest(self, namespace, plan_name=None):
        # ``records().get_manifest(ns, plan)`` is the migrated form; a bare
        # ``store.get_manifest(plan)`` is the pre-migration path read that a
        # site this story does not touch still makes.
        if plan_name is None:
            namespace, plan_name = "default", namespace
        validate_plan_name(plan_name)
        self.reads.append((namespace, plan_name))
        stored = self.data.get(plan_name)
        return copy.deepcopy(stored) if stored is not None else None

    def get_manifests(self, namespace):
        self.bulk_reads += 1
        return copy.deepcopy(self._snapshot_cache)

    def put_manifest(self, namespace, plan_name, manifest):
        validate_plan_name(plan_name)
        if not isinstance(manifest, dict):
            raise TypeError("manifest must be a dict")
        self.writes.append((namespace, plan_name, copy.deepcopy(manifest)))
        self.data[plan_name] = copy.deepcopy(manifest)

    def save_manifest(self, plan_name, manifest):
        self.data[plan_name] = copy.deepcopy(manifest)

    def get_manifest_or_none(self, plan_name):
        stored = self.data.get(plan_name)
        return copy.deepcopy(stored) if stored is not None else None

    # -- plan lock -----------------------------------------------------
    def transaction(self, plan_name, wait_seconds=0.0):
        store = self

        class _Tx:
            def __enter__(self):
                return store.acquired

            def __exit__(self, *exc):
                return False

        return _Tx()

    # -- convenience for assertions ------------------------------------
    def seed(self, plan_name, manifest):
        self.data[plan_name] = copy.deepcopy(manifest)
        self._snapshot_cache = copy.deepcopy(self.data)

    def stored(self, plan_name):
        return copy.deepcopy(self.data.get(plan_name))


@pytest.fixture
def fake_store(tmp_path, monkeypatch):
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    store = _FakeStore(plan_dir)
    monkeypatch.setattr(p, "_store", store)
    return store


# ---------------------------------------------------------------------------
# pipeline/merge.py
# ---------------------------------------------------------------------------


class _Completed:
    """Stand-in for ``subprocess.run``'s result."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _merge_seams(monkeypatch, ci_state="pass"):
    """Patch every outside-the-lock seam ``_approve_merge_impl`` reaches."""
    import pipeline.authorization as auth
    import pipeline.review_merge_binding as binding

    monkeypatch.setattr(auth, "authorize", lambda *a, **k: None)
    monkeypatch.setattr(binding, "branch_head_sha", lambda branch: "head-sha")
    monkeypatch.setattr(binding, "check", lambda story, head: {"ok": True})
    monkeypatch.setattr(
        binding, "rebind_after_pipeline_push", lambda *a, **k: None
    )
    monkeypatch.setattr(
        p,
        "_ci_status",
        lambda *a, **k: {"state": ci_state, "error": ""},
    )
    monkeypatch.setattr(p, "_ci_rerun", lambda sha: None)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_merge_pr", lambda wt, key: "merged")
    monkeypatch.setattr(
        p, "_rebase_onto_master", lambda wt, br: {"ok": True, "error": ""}
    )
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_scoped_repo_root", _null_scoped_root)
    monkeypatch.setattr(p, "_reverify_acceptance", lambda *a, **k: {"state": "pass"})
    monkeypatch.setattr(p, "_reverify_build", lambda *a, **k: {"state": "pass"})
    monkeypatch.setattr(p, "_mcp_self_source_touched", lambda *a, **k: "")
    monkeypatch.setattr(merge, "record_audit", lambda *a, **k: None)
    # The gate force-pushes the story branch; ``Path("").is_dir()`` is the cwd,
    # so even the fixture's empty worktree reaches this call.
    monkeypatch.setattr(
        merge.subprocess,
        "run",
        lambda *a, **k: _Completed(returncode=0),
    )
    monkeypatch.setattr(p, "_mark_plane_done", lambda *a, **k: None)
    if hasattr(p, "_maybe_record_retro"):
        monkeypatch.setattr(p, "_maybe_record_retro", lambda *a, **k: None)


class _null_scoped_root:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _parked_manifest(plan_name: str, story_key: str = "S1") -> dict:
    return {
        "plan": plan_name,
        "revision": 7,
        "stories": {
            story_key: {
                "summary": "ready pr",
                "status": "parked",
                "review_verdict": "APPROVE",
                "review_verdict_sha": "head-sha",
                "risk": "low",
                "worktree": "",
                "parked_reason": "awaiting human merge",
            }
        },
    }


def test_should_persist_the_merge_result_through_the_record_store(
    fake_store, monkeypatch
):
    """The merge's manifest writes land through ``put_manifest``."""
    plan_name, story_key = "p1", "S1"
    fake_store.seed(plan_name, _parked_manifest(plan_name, story_key))
    _merge_seams(monkeypatch)

    result = merge._approve_merge_impl(plan_name, story_key)

    assert result["ok"] is True, result
    assert fake_store.writes, "the merge must persist through the record store"
    namespaces = {ns for ns, _plan, _m in fake_store.writes}
    plans = {plan for _ns, plan, _m in fake_store.writes}
    assert namespaces == {"default"}
    assert plans == {plan_name}
    # The final write is the merged outcome, taken from the manifest the store
    # holds - not a pre-lock copy.
    final = fake_store.stored(plan_name)
    assert final["stories"][story_key]["status"] == "done"
    assert "parked_reason" not in final["stories"][story_key]
    assert final["revision"] == 7  # merge.py never rewrites the revision counter
    assert fake_store.bulk_reads == 0, "a shared snapshot must never be written"


def test_should_not_write_when_the_merge_transaction_is_busy(
    fake_store, monkeypatch
):
    """A busy plan lock returns retriable and leaves the stored state alone."""
    plan_name, story_key = "p2", "S1"
    fake_store.seed(plan_name, _parked_manifest(plan_name, story_key))
    _merge_seams(monkeypatch)
    fake_store.acquired = False

    busy = merge._approve_merge_impl(plan_name, story_key)

    assert busy["ok"] is False
    assert busy["retriable"] is True
    assert fake_store.writes == []
    assert fake_store.stored(plan_name)["revision"] == 7
    assert (
        fake_store.stored(plan_name)["stories"][story_key]["status"] == "parked"
    )

    # Once the lock frees up the very same call merges the still-untouched copy.
    fake_store.acquired = True
    done = merge._approve_merge_impl(plan_name, story_key)
    assert done["ok"] is True
    assert fake_store.stored(plan_name)["stories"][story_key]["status"] == "done"


def test_should_report_unknown_plan_when_the_store_has_no_manifest(
    fake_store, monkeypatch
):
    """No manifest in the store -> "unknown plan", and nothing is created."""
    plan_name = "ghost"
    _merge_seams(monkeypatch)

    result = merge._set_plan_paused(plan_name, True)

    assert result["ok"] is False, result
    assert "manifest" in result["error"].lower(), result
    assert fake_store.writes == []
    assert fake_store.stored(plan_name) is None
    assert not fake_store.manifest_path(plan_name).exists()
    assert fake_store.bulk_reads == 0

    # The failed call left nothing behind: a manifest created afterwards is
    # paused from its own state, not from a phantom record.
    fake_store.seed(plan_name, {"plan": plan_name, "stories": {}})
    assert merge._set_plan_paused(plan_name, True)["ok"] is True
    assert fake_store.stored(plan_name)["paused"] is True


def test_should_fail_closed_when_the_merge_plan_name_is_not_a_record_key(
    fake_store, monkeypatch
):
    """``a..b`` passes key validation but the store rejects it: no write."""
    good = "p4"
    fake_store.seed(good, _parked_manifest(good))
    _merge_seams(monkeypatch)
    bad = "a..b"
    with pytest.raises(InvalidRecordKeyError) as excinfo:
        validate_plan_name(bad)

    # The same rejection reaches a caller of the merge site, as a ValueError
    # (InvalidRecordKeyError subclasses it), so an MCP tool wrapper's
    # ``except ValueError`` still turns it into an error result - and the
    # rejected name never reaches a write.
    with pytest.raises(ValueError):
        merge._approve_merge_impl(bad, "S1")

    assert isinstance(excinfo.value, InvalidRecordKeyError)
    assert fake_store.writes == []
    assert fake_store.stored(good)["revision"] == 7
    assert not (fake_store.plan_dir / f"{bad}.manifest.json").exists()


# ---------------------------------------------------------------------------
# pipeline/detached_grade.py
# ---------------------------------------------------------------------------


def _grade_tick(monkeypatch, *, story_key, plan_name, manifest_path, manifest,
                alive, collected=None, pid=9001):
    """One ``_detached_grade_lifecycle`` tick with the process boundary stubbed."""
    story = manifest["stories"][story_key]

    if collected is not None:
        monkeypatch.setattr(
            detached_grade, "collect_detached_grade", lambda *a, **k: collected,
            raising=False,
        )
    monkeypatch.setattr(
        detached_grade, "start_detached_grade",
        lambda *a, **k: pid, raising=False,
    )
    monkeypatch.setattr(
        detached_grade, "_is_pid_alive", lambda _pid: alive, raising=False
    )
    monkeypatch.setattr(
        detached_grade, "_untrack_scratchpad", None, raising=False
    )
    for var in (
        "PIPELINE_MAX_CONCURRENT_GRADES",
        "PIPELINE_GRADE_XDIST_WORKERS",
        "PIPELINE_STATE_DIR",
        "PIPELINE_GRADE_DETACHED",
    ):
        monkeypatch.delenv(var, raising=False)
    worktree = manifest_path.parent / "worktree"
    worktree.mkdir(exist_ok=True)
    return detached_grade._detached_grade_lifecycle(
        story,
        story_key,
        story.get("pid", 1234),
        worktree,
        manifest,
        plan_name,
        [".venv/bin/python", "-m", "pytest", "-q"],
        manifest_path.parent,
        {"PATH": "/usr/bin"},
    )


def test_should_persist_grade_start_and_grade_result_through_the_record_store(
    fake_store, monkeypatch
):
    """Both grade state transitions land through ``put_manifest``.

    The lifecycle's persisted transition is the grading bookkeeping it owns:
    tick 1 records ``grading_pid`` / ``grading_started_at`` /
    ``grading_result_path`` and frees the scheduler tick; tick 2 reads THAT
    back, consumes the bookkeeping and stamps ``collected_at``. Seeding
    ``attempt`` (a field the lifecycle never touches) pins that each write
    carries the whole manifest from a fresh read rather than a fragment.
    The two halves are also covered one at a time below; this test pins the
    hand-off between them.
    """
    plan_name, story_key = "g1", "s1"
    fake_store.seed(
        plan_name,
        {
            "plan": plan_name,
            "stories": {story_key: {"status": "in_progress", "attempt": 1}},
        },
    )
    manifest_path = fake_store.manifest_path(plan_name)

    # Tick 1: fresh read, grade starts.
    manifest = fake_store.records().get_manifest("default", plan_name)
    early, _ = _grade_tick(
        monkeypatch,
        story_key=story_key,
        plan_name=plan_name,
        manifest_path=manifest_path,
        manifest=manifest,
        alive=False,
    )
    assert early == {"status": "grading", "pid": 9001}
    assert len(fake_store.writes) == 1
    start = fake_store.writes[0]
    assert start[0] == "default" and start[1] == plan_name
    written = fake_store.stored(plan_name)["stories"][story_key]
    assert written["grading_pid"] == 9001
    assert written["grading_started_at"]
    assert written["grading_result_path"]
    assert written["attempt"] == 1

    # Tick 2: fresh read of WHAT TICK 1 WROTE, grade result collected.
    manifest = fake_store.records().get_manifest("default", plan_name)
    assert manifest["stories"][story_key]["grading_pid"] == 9001
    early, test_result = _grade_tick(
        monkeypatch,
        story_key=story_key,
        plan_name=plan_name,
        manifest_path=manifest_path,
        manifest=manifest,
        alive=False,
        collected={"stdout": "1 passed", "returncode": 0},
    )
    assert early is None
    assert test_result.returncode == 0
    assert len(fake_store.writes) == 2
    assert fake_store.writes[1][1] == plan_name
    result = fake_store.stored(plan_name)["stories"][story_key]
    assert "grading_pid" not in result
    assert "grading_started_at" not in result
    assert "grading_result_path" not in result
    assert result["collected_at"]
    assert result["attempt"] == 1, "the untouched field written by tick 1 survives"
    assert fake_store.bulk_reads == 1, "only the start tick's cap check reads the snapshot"


def test_should_persist_the_grade_start_through_the_record_store(
    fake_store, monkeypatch
):
    """The grade-start transition is one ``put_manifest``, whole manifest.

    One write, at the record store, carrying the manifest read for this tick -
    including the ``attempt`` field the lifecycle never touches, which the
    pre-migration path write also preserved.
    """
    plan_name, story_key = "g2", "s1"
    fake_store.seed(
        plan_name,
        {
            "plan": plan_name,
            "stories": {
                story_key: {"status": "in_progress", "attempt": 3, "pid": 4321}
            },
        },
    )
    manifest = fake_store.records().get_manifest("default", plan_name)

    early, test_result = _grade_tick(
        monkeypatch,
        story_key=story_key,
        plan_name=plan_name,
        manifest_path=fake_store.manifest_path(plan_name),
        manifest=manifest,
        alive=False,
    )

    assert early == {"status": "grading", "pid": 9001}
    assert test_result is None, "the tick is freed; a later tick grades"
    assert len(fake_store.writes) == 1
    namespace, written_plan, written = fake_store.writes[0]
    assert (namespace, written_plan) == ("default", plan_name)
    assert written["stories"][story_key]["grading_pid"] == 9001
    assert written["stories"][story_key]["attempt"] == 3
    assert fake_store.bulk_reads == 1, "only the start tick's cap check reads the snapshot"


def test_should_persist_the_grade_result_through_the_record_store(
    fake_store, monkeypatch
):
    """The grade-result transition is one ``put_manifest`` that consumes the
    bookkeeping the grade-start write landed, leaving the result behind."""
    plan_name, story_key = "g3", "s1"
    started_at = "2026-01-01T00:00:00+00:00"
    fake_store.seed(
        plan_name,
        {
            "plan": plan_name,
            "stories": {
                story_key: {
                    "status": "in_progress",
                    "attempt": 2,
                    "grading_pid": 9001,
                    "grading_started_at": started_at,
                    "grading_result_path": str(
                        fake_store.plan_dir / "grading" / story_key / "result.json"
                    ),
                }
            },
        },
    )
    manifest = fake_store.records().get_manifest("default", plan_name)

    early, test_result = _grade_tick(
        monkeypatch,
        story_key=story_key,
        plan_name=plan_name,
        manifest_path=fake_store.manifest_path(plan_name),
        manifest=manifest,
        alive=False,
        collected={"stdout": "1 passed", "returncode": 0},
    )

    assert early is None
    assert test_result.returncode == 0
    assert len(fake_store.writes) == 1
    namespace, written_plan, written = fake_store.writes[0]
    assert (namespace, written_plan) == ("default", plan_name)
    story_written = written["stories"][story_key]
    assert "grading_pid" not in story_written
    assert "grading_started_at" not in story_written
    assert "grading_result_path" not in story_written
    assert story_written["collected_at"]
    assert story_written["attempt"] == 2
    assert fake_store.bulk_reads == 0


def test_should_fail_closed_when_the_grade_plan_name_is_not_a_record_key(
    fake_store, monkeypatch
):
    """A store-rejected plan name raises before any write; nothing is corrupted.

    ``a..b`` passes ``_validate_key`` but not ``validate_plan_name``, so
    ``put_manifest`` raises ``InvalidRecordKeyError`` (a ``ValueError``) - the
    lifecycle never falls back to writing the file projection, and the seeded
    state stays ``queued/0``.
    """
    valid, story_key = "g-ok", "s1"
    fake_store.seed(
        valid,
        {
            "plan": valid,
            "stories": {story_key: {"status": "in_progress", "state": "queued",
                                    "attempt": 0}},
        },
    )
    bad = "a..b"
    manifest_path = fake_store.plan_dir / f"{bad}.manifest.json"
    manifest = {
        "plan": bad,
        "stories": {story_key: {"status": "in_progress", "state": "queued",
                                "attempt": 0}},
    }

    with pytest.raises(InvalidRecordKeyError):
        _grade_tick(
            monkeypatch,
            story_key=story_key,
            plan_name=bad,
            manifest_path=manifest_path,
            manifest=manifest,
            alive=False,
        )

    assert isinstance(InvalidRecordKeyError("x"), ValueError)
    assert fake_store.writes == []
    assert not manifest_path.exists()
    assert json.loads(json.dumps(manifest))["stories"][story_key]["state"] == (
        "queued"
    )
    # The rejected name left no record behind: a retry under a valid name
    # starts from queued/0 and performs the transition.
    retry = fake_store.records().get_manifest("default", valid)
    assert retry["stories"][story_key]["state"] == "queued"
    assert retry["stories"][story_key]["attempt"] == 0
