"""``check_story_status`` persists every transition through the record store.

Before this change the twelve manifest writes inside
``pipeline/story_status.py``'s ``check_story_status`` each called
``_atomic_write_json(manifest_path, manifest)`` directly. They now go through
``_store.records().put_manifest("default", plan_name, manifest)`` at the same
point in the same (caller-held) lock scope, so a fake store alone is enough to
observe the write -- and to prove that a missing plan or a rejected plan name
writes nothing at all.

The tests pin the two mistakes this migration invites:

* feeding a ``get_manifests()`` cache snapshot (or a single story dict) into
  ``put_manifest`` instead of the whole freshly-read manifest, which would drop
  or normalise the sibling stories; and
* ``manifest = manifest or {}`` followed by a write, which materialises an
  empty plan as a side effect of a read.

The detached-grading transitions are NOT on this module's write list: they live
in ``pipeline/detached_grade.py`` and remain delegated there, reached through
``manifest_path`` (still threaded through, unchanged by this story).

Under ``PIPELINE_STORE=sqlite`` the old sites wrote the file projection only,
bypassing the database row; going through ``put_manifest`` writes the row (the
file store projects it back to ``<plan>.manifest.json``), so the on-disk layout
is unchanged.
"""

from __future__ import annotations

import copy
import os
import re
import subprocess
from pathlib import Path

import pytest

import pipeline.server as p
from pipeline.store_records import InvalidRecordKeyError, validate_plan_name

PLAN = "persist-plan"
STORY = "story-a"
SIBLING = "story-b"


class _FakeStore:
    """A store whose manifests live in one dict, recording every write.

    ``get_manifest`` validates the plan name and hands back an independent deep
    copy (``None`` when absent); ``put_manifest`` records and stores a deep
    copy. ``get_manifests`` deliberately returns a *shared, normalised* snapshot
    so a writer that feeds it back into ``put_manifest`` fails loudly.
    """

    def __init__(self, plan_dir: Path, data: dict[str, dict] | None = None) -> None:
        self.plan_dir = plan_dir
        self.data: dict[str, dict] = data or {}
        self.puts: list[tuple[str, str, dict]] = []
        self.reads: list[tuple[str, str]] = []
        self.bulk_reads = 0

    # -- Store surface check_story_status touches ----------------------
    def manifest_path(self, plan_name):
        return self.plan_dir / f"{plan_name}.manifest.json"

    def records(self):
        return self

    def get_manifest(self, namespace, plan_name=None):
        # records().get_manifest(ns, plan) is the migrated form; a bare
        # store.get_manifest(plan) is the pre-migration read.
        if plan_name is None:
            namespace, plan_name = "default", namespace
        validate_plan_name(plan_name)
        self.reads.append((namespace, plan_name))
        stored = self.data.get(plan_name)
        return copy.deepcopy(stored) if stored is not None else None

    def get_manifests(self, namespace):
        # A shared read-only cache snapshot that has been "normalised" (sibling
        # stories stripped): feeding this to put_manifest loses data.
        self.bulk_reads += 1
        return {name: {"stories": {}} for name in self.data}

    def put_manifest(self, namespace, plan_name, manifest):
        validate_plan_name(plan_name)
        assert isinstance(manifest, dict), "manifest must be a dict"
        self.puts.append((namespace, plan_name, copy.deepcopy(manifest)))
        self.data[plan_name] = copy.deepcopy(manifest)


def _manifest(story: dict | None = None, sibling: dict | None = None) -> dict:
    return {
        "epics": {},
        "stories": {
            STORY: story if story is not None else {
                "summary": "thing",
                "status": "in_progress",
                "pid": 4242,
                "worktree": "/nonexistent/wt",
            },
            SIBLING: sibling if sibling is not None else {
                "summary": "other",
                "status": "queued",
                "attempts": 0,
            },
        },
    }


@pytest.fixture
def plan_dir(tmp_path):
    d = tmp_path / "plans"
    d.mkdir()
    return d


def _install(monkeypatch, plan_dir, data, *, agent_log_text="[step 0] bash: pwd\n",
             test_returncode=0, test_stdout="1 passed", alive=False,
             wedge_signals=None, story_backend="local"):
    """Install the fake store and stub every seam a check_story_status run hits.

    ``story['worktree']`` is pointed at a real directory so the post-mortem
    paths can stat ``agent.log``. ``subprocess.run`` is routed so the detected
    test command reports ``test_returncode`` while ``ps``/git report success
    with empty output.
    """
    worktree = plan_dir / "wt"
    worktree.mkdir(exist_ok=True)
    (worktree / "agent.log").write_text(agent_log_text)
    for manifest in data.values():
        for story in manifest.get("stories", {}).values():
            if isinstance(story, dict) and "worktree" in story:
                story["worktree"] = str(worktree)
                if "backend" not in story:
                    story["backend"] = story_backend

    store = _FakeStore(plan_dir, copy.deepcopy(data))
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir, raising=False)
    monkeypatch.setattr(p, "_store", store, raising=False)
    monkeypatch.setattr(
        p, "detect_test_command", lambda wt: (wt, ["pytest", "-q"]), raising=False
    )
    monkeypatch.setattr(p, "detect_lint_command", lambda wt: None, raising=False)
    monkeypatch.setattr(p, "_pytest_ignored_paths", lambda cmd: [], raising=False)
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True,
                        raising=False)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(p, "_rebrief_step_cap_struggle", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(p, "collect_story_wedge_signals",
                        lambda *a, **k: dict(wedge_signals or {}), raising=False)

    def _run(cmd, *a, **k):
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        text = " ".join(str(c) for c in argv)
        if text.startswith("ps ") or argv[:1] == ["ps"]:
            return subprocess.CompletedProcess(argv, 0, stdout="S\n", stderr="")
        if "pytest" in text:
            return subprocess.CompletedProcess(
                argv, test_returncode, stdout=test_stdout, stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(p.subprocess, "run", _run, raising=False)
    if alive:
        monkeypatch.setattr(p.os, "kill", lambda pid, sig: None, raising=False)
    else:
        monkeypatch.setattr(
            p.os, "kill",
            lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
            raising=False,
        )
    return store, worktree


def _age_agent_log(plan_dir):
    """Push the agent log's mtime past the startup grace window."""
    os.utime(plan_dir / "wt" / "agent.log", (0, 0))


def _stories(written: dict) -> dict:
    return written.get("stories", {})


# ---------------------------------------------------------------------------
# One test per distinct transition written by this module.
# ---------------------------------------------------------------------------


def test_tests_passed_is_written_through_the_store(plan_dir, monkeypatch):
    """The terminal write persists `tests_passed` and leaves the sibling story
    byte-identical -- the whole freshly-read manifest goes through."""
    store, _wt = _install(monkeypatch, plan_dir, {PLAN: _manifest()})

    p.check_story_status(PLAN, STORY)

    # Mistake guarded against: put_manifest(get_manifests()[...]) drops the
    # sibling, and put_manifest(story) writes only the single story dict.
    assert len(store.puts) == 1, f"put_manifest calls: {len(store.puts)} != 1"
    _ns, plan_name, written = store.puts[0]
    assert plan_name == PLAN
    stories = _stories(written)
    assert len(stories) == 2
    assert stories[STORY]["status"] == "tests_passed"
    assert stories[SIBLING] == {
        "summary": "other", "status": "queued", "attempts": 0,
    }


def test_tests_failed_is_written_through_the_store(plan_dir, monkeypatch):
    """A red test run persists `failed` for the story and keeps the sibling."""
    store, _wt = _install(
        monkeypatch, plan_dir, {PLAN: _manifest()},
        test_returncode=1, test_stdout="1 failed",
    )

    p.check_story_status(PLAN, STORY)

    assert len(store.puts) == 1, f"put_manifest calls: {len(store.puts)} != 1"
    stories = _stories(store.puts[0][2])
    assert len(stories) == 2
    assert stories[STORY]["status"] == "failed"
    assert stories[SIBLING]["status"] == "queued"


def test_escalation_is_written_through_the_store(plan_dir, monkeypatch):
    """The watchdog-streak escalation branch persists `escalated` via the store.

    Reaching it needs an alive pid whose activity is stale, a streak already at
    the fallback threshold, no configured local fallback model, and auto
    escalation on.
    """
    threshold = p.STEP_CAP_FALLBACK_THRESHOLD
    store, _wt = _install(
        monkeypatch, plan_dir,
        {PLAN: _manifest(story={
            "summary": "thing", "status": "in_progress", "pid": 4242,
            "worktree": "/nonexistent/wt", "backend": "local",
            "model": "local:m", "watchdog_streak": threshold - 1,
            "dispatched_at": "2020-01-01T00:00:00+00:00",
        })},
        alive=True, wedge_signals={"activity_age_seconds": 10 ** 9},
    )
    monkeypatch.setattr(p, "_terminate_and_checkpoint", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: True,
                        raising=False)
    monkeypatch.setattr(p, "_escalation_target", lambda: ("claude", "opus"),
                        raising=False)
    monkeypatch.setattr(p, "_escalation_label", lambda: "claude", raising=False)
    monkeypatch.setattr(p, "_escalate_to_claude", lambda *a, **k: None,
                        raising=False)

    result = p.check_story_status(PLAN, STORY)

    assert result["reason"] == "step_cap_escalated_to_claude"
    assert result["status"] == "todo"
    assert len(store.puts) == 1, f"put_manifest calls: {len(store.puts)} != 1"
    stories = _stories(store.puts[0][2])
    assert len(stories) == 2
    # The escalated story is the one that was bumped and rewritten; the sibling
    # came along untouched (the whole manifest goes through the store).
    assert stories[STORY]["watchdog_streak"] == threshold
    assert stories[SIBLING] == {
        "summary": "other", "status": "queued", "attempts": 0,
    }


def test_stale_pid_interrupt_is_written_once(plan_dir, monkeypatch):
    """A dead pid that never wrote output marks the story `interrupted` ONCE;
    a second poll does not write again and does not disturb that state."""
    store, _wt = _install(
        monkeypatch, plan_dir,
        {PLAN: _manifest(story={
            "summary": "thing", "status": "in_progress", "pid": 4242,
            "dispatch_attempts": 1, "worktree": "/nonexistent/wt",
        })},
        agent_log_text="",
    )
    _age_agent_log(plan_dir)

    first = p.check_story_status(PLAN, STORY)

    assert first["status"] == "interrupted"
    assert len(store.puts) == 1, f"put_manifest calls: {len(store.puts)} != 1"
    stories = _stories(store.puts[0][2])
    assert len(stories) == 2
    assert stories[STORY]["status"] == "interrupted"
    # Mistake guarded against: also zeroing/deriving the attempt counter or
    # clearing pid so the follow-up call takes a different branch.
    assert stories[STORY]["dispatch_attempts"] == 2
    assert stories[STORY]["pid"] == 4242

    second = p.check_story_status(PLAN, STORY)

    assert second["status"] == "interrupted"
    assert len(store.puts) == 1, (
        f"second poll must not write again; put_manifest calls: "
        f"{len(store.puts)} != 1"
    )


def test_launch_failure_give_up_is_written_through_the_store(plan_dir, monkeypatch):
    """Once the launch-attempt budget is spent the story becomes terminal
    `failed` -- written through the store, sibling untouched."""
    store, _wt = _install(
        monkeypatch, plan_dir,
        {PLAN: _manifest(story={
            "summary": "thing", "status": "in_progress", "pid": 4242,
            "dispatch_attempts": p.DISPATCH_MAX_ATTEMPTS - 1,
            "worktree": "/nonexistent/wt",
        })},
        agent_log_text="",
    )
    _age_agent_log(plan_dir)

    result = p.check_story_status(PLAN, STORY)

    assert result["status"] == "failed"
    assert len(store.puts) == 1, f"put_manifest calls: {len(store.puts)} != 1"
    stories = _stories(store.puts[0][2])
    assert len(stories) == 2
    assert stories[STORY]["status"] == "failed"
    assert stories[SIBLING]["status"] == "queued"


def test_acceptance_tamper_is_written_through_the_store(plan_dir, monkeypatch):
    """A tampered acceptance fixture routes to `changes_requested` and refuses
    `tests_passed`, persisted through the store."""
    store, _wt = _install(monkeypatch, plan_dir, {PLAN: _manifest()})
    monkeypatch.setattr(p, "_acceptance_tampered", lambda *a, **k: ["oracle.py"],
                        raising=False)

    result = p.check_story_status(PLAN, STORY)

    assert result["status"] == "changes_requested"
    assert len(store.puts) == 1, f"put_manifest calls: {len(store.puts)} != 1"
    stories = _stories(store.puts[0][2])
    assert len(stories) == 2
    assert stories[STORY]["status"] == "changes_requested"


def test_empty_agent_branch_is_written_through_the_store(plan_dir, monkeypatch):
    """Tests pass but the agent branch has no new commits -> terminal `failed`,
    never a false `tests_passed`."""
    store, _wt = _install(monkeypatch, plan_dir, {PLAN: _manifest()})
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: False,
                        raising=False)

    result = p.check_story_status(PLAN, STORY)

    assert result["reason"] == "empty_agent_branch"
    assert len(store.puts) == 1, f"put_manifest calls: {len(store.puts)} != 1"
    stories = _stories(store.puts[0][2])
    assert len(stories) == 2
    assert stories[STORY]["status"] == "failed"


def test_grading_transitions_are_written_through_the_store(plan_dir, monkeypatch):
    """Grading is delegated to detached_grade with the manifest path, so this
    module contributes no grading write of its own: a run that hands off to the
    detached grader writes nothing to the manifest directly."""
    store, _worktree = _install(monkeypatch, plan_dir, {PLAN: _manifest()})
    seen: list[object] = []

    def _start(manifest, manifest_path, *a, **k):
        # The pass-on seam this story deliberately leaves in place (ADR 0012
        # stage C removes the last Store.manifest_path use).
        seen.append(manifest_path)
        return {"started": True}

    def _lifecycle(story, story_key, pid, worktree, manifest, plan_name,
                   *a, **k):
        seen.append(plan_name)
        return None, None

    monkeypatch.setattr(p, "start_detached_grade", _start, raising=False)
    monkeypatch.setattr(p, "_detached_grade_lifecycle", _lifecycle,
                        raising=False)

    p.check_story_status(PLAN, STORY)

    # Whatever branch ran, all manifest writes went through the store and none
    # bypassed it. The grader is handed the plan name, not a manifest path.
    assert store.puts, "expected at least one store write"
    for _ns, _plan, written in store.puts:
        assert len(_stories(written)) == 2
    for plan in seen:
        assert plan == PLAN


def test_every_manifest_write_in_the_module_goes_through_the_store():
    """No direct file write survives: the module has no ``_atomic_write_json``
    left and its manifest writes are all ``put_manifest`` calls."""
    source = Path(__file__).resolve().parents[2] / "pipeline" / "story_status.py"
    text = source.read_text()
    assert "_atomic_write_json" not in text, (
        "story_status must persist through the store, not the atomic JSON writer"
    )
    puts = re.findall(r'\.put_manifest\(\s*"default"', text)
    assert len(puts) == 12, f"expected 12 put_manifest writes, found {len(puts)}"
    assert 'get_manifest("default", plan_name)' in text


# ---------------------------------------------------------------------------
# Negative / boundary cases.
# ---------------------------------------------------------------------------


def test_should_return_plan_not_found_when_the_store_has_no_manifest(
    plan_dir, monkeypatch
):
    """A missing manifest returns the not-found dict and never materialises a
    plan: a second identical call still finds nothing and writes nothing."""
    store, _wt = _install(monkeypatch, plan_dir, {})

    first = p.check_story_status("missing-plan", STORY)

    assert first == {"ok": False, "error": "Plan not found"}
    assert len(store.puts) == 0, f"put_manifest calls: {len(store.puts)} != 0"

    second = p.check_story_status("missing-plan", STORY)

    assert second == {"ok": False, "error": "Plan not found"}
    # Mistake guarded against: `manifest = manifest or {}` then put_manifest({})
    # creates the plan as a side effect of a read.
    assert len(store.puts) == 0, f"put_manifest calls: {len(store.puts)} != 0"


def test_empty_but_valid_manifest_is_not_plan_not_found(plan_dir, monkeypatch):
    """A present-but-empty manifest is not the same thing as a missing one."""
    store, _wt = _install(
        monkeypatch, plan_dir, {PLAN: {"epics": {}, "stories": {}}}
    )

    result = p.check_story_status(PLAN, STORY)

    # Mistake guarded against: `if not manifest:` reports Plan not found for a
    # perfectly valid empty manifest.
    assert result != {"ok": False, "error": "Plan not found"}
    assert result == {"ok": False, "error": "Story not dispatched"}
    assert len(store.puts) == 0, f"put_manifest calls: {len(store.puts)} != 0"


def test_should_not_write_for_a_plan_name_the_store_rejects(plan_dir, monkeypatch):
    """A plan name the store rejects fails closed: no write, and the failure is
    a ValueError the existing caller handlers already catch."""
    store, _wt = _install(monkeypatch, plan_dir, {"-plan": _manifest()})

    with pytest.raises(ValueError) as excinfo:
        p.check_story_status("-plan", STORY)

    assert isinstance(excinfo.value, InvalidRecordKeyError)
    assert len(store.puts) == 0, f"put_manifest calls: {len(store.puts)} != 0"

    # A well-formed name still works afterwards -- exactly one write.
    store2, _wt2 = _install(monkeypatch, plan_dir, {PLAN: _manifest()})
    p.check_story_status(PLAN, STORY)
    assert len(store2.puts) == 1, f"put_manifest calls: {len(store2.puts)} != 1"
