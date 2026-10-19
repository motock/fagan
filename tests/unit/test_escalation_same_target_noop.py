"""ESC-NOOP-1: an escalation whose resolved target equals the story's current
``(backend, model)`` must be a no-op.

Diagnosed live on story M56-1: the story sat on ``ollama/glm-5.3:cloud`` with
``PIPELINE_ESCALATION_BACKEND=ollama`` and ``PIPELINE_ESCALATION_MODEL=glm-5.3:cloud``
set. At the review rework cap the scheduler ran ``_escalate_review_to_claude``,
which rewrote the identical pair, popped ``rework_attempts``, set
``escalated=True``, and handed the same model three more cycles. The story never
changed tier, yet it burned 5 rework cycles and carried an ``escalated`` flag
that pollutes the first-pass-clean metric.

The dispatch-failure escalation sites already carry a same-target guard
(``_escalation_target() != (story.get("backend"), story.get("model"))``). These
tests pin the same guard at the four review-loop sites and the triage
``escalate_model`` site, and - just as importantly - pin that a *real*
escalation (different target) still fires.

Every test asserts the full state, not just ``status == "parked"``: the specific
failure mode is a guard that parks but still resets ``rework_attempts`` and sets
``escalated=True``. That passes a status-only assertion while corrupting the
next pass (``escalated=True`` makes the next pass skip the guard entirely, and a
reset budget grants three more cycles).

Harness shape mirrors ``tests/unit/test_final_rework_escalation.py`` (plan_dir /
agents_dir fixtures, ``_run_reviewer`` mocked to force a verdict) and
``tests/unit/test_no_commit_rework_fresh_context.py`` (dead pid, passing test
command, HEAD == ``last_reviewed_sha``). Fully standalone.
"""

import json

import pytest

import pipeline.server as p
import pipeline.triage as ptriage
from pipeline import concurrency as pcon
from pipeline import persistence as ppers

SAME_BACKEND = "ollama"
SAME_MODEL = "glm-5.3:cloud"
OTHER_BACKEND = "claude"
OTHER_MODEL = "opus"
HEAD_SHA = "abc123"

# A REQUEST_CHANGES verdict carrying genuine findings text, so it is NOT routed
# through the empty-findings inconclusive path (see _has_review_findings).
_REQUEST_CHANGES_WITH_FINDINGS = (
    "The error path is untested and the SQL is injectable; add coverage and "
    "parameterize the query.\nVERDICT: REQUEST_CHANGES"
)

# No VERDICT line at all -> _parse_verdict returns UNKNOWN (inconclusive), and
# the text is not a transient transport error.
_INCONCLUSIVE_OUTPUT = "I read the diff but could not settle on a verdict."


# ---------------------------------------------------------------------------
# Fixtures / helpers (standalone, mirroring the reference test files).
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    # pipeline_persistence and pipeline_concurrency import PLAN_DIR from
    # pipeline_paths at module load and read it as a free var, so patches must
    # land on their own bindings too.
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(p, "AGENTS_DIR", d)
    return d


def _write_manifest_with_story(plan_dir, plan_name, story_key, story, *,
                               top_level=None):
    manifest = {"epics": {}, "stories": {story_key: story}}
    if top_level:
        manifest.update(top_level)
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(manifest))


def _read_story(plan_dir, plan_name, story_key):
    return json.loads(
        (plan_dir / f"{plan_name}.manifest.json").read_text()
    )["stories"][story_key]


def _make_story(plan_dir, *, status, **extra):
    story = {
        "summary": "Add thing",
        "status": status,
        "worktree": str(plan_dir / "wt"),
        "risk": "low",
    }
    story.update(extra)
    return story


def _capture_notifications(monkeypatch):
    """Record every user notification emitted by the server, the escalation
    module and the triage module (each binds ``_notify_user`` separately)."""
    calls = []

    def _record(plan_name, msg, **kwargs):
        calls.append((plan_name, msg))

    monkeypatch.setattr(p, "_notify_user", _record)
    monkeypatch.setattr("pipeline.escalation._notify_user", _record)
    monkeypatch.setattr(ptriage, "_notify_user", _record)
    return calls


def _enable_auto_escalation(monkeypatch):
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: True)


def _same_target_env(monkeypatch):
    """Point escalation at exactly the pair the story is already running on."""
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", SAME_BACKEND)
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", SAME_MODEL)


def _different_target_env(monkeypatch):
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", OTHER_BACKEND)
    monkeypatch.setenv("PIPELINE_ESCALATION_MODEL", OTHER_MODEL)


def _force_request_changes(monkeypatch, output=None):
    out = output if output is not None else _REQUEST_CHANGES_WITH_FINDINGS
    monkeypatch.setattr(p, "_run_reviewer", lambda *a, **k: out)

    def _boom_pr(*a, **k):
        raise AssertionError("PR must not be opened on REQUEST_CHANGES")

    monkeypatch.setattr(p, "_open_pr", _boom_pr)


def _assert_no_escalation_notification(calls):
    assert not any("escalating to" in msg for _pn, msg in calls), (
        f"a same-target escalation must not announce an escalation; got {calls!r}"
    )


# ---------------------------------------------------------------------------
# (a) Review rework cap, SAME target -> parks, budget preserved, not escalated.
# ---------------------------------------------------------------------------

def test_review_rework_cap_same_target_parks_without_resetting_budget(
    plan_dir, agents_dir, monkeypatch,
):
    """The M56-1 defect. Story on ollama/glm-5.3:cloud, escalation target the
    same pair, rework_attempts at 2 so this cycle's attempts becomes 3 ==
    REWORK_MAX_ATTEMPTS. The guard is false, so the existing else branch parks
    and nothing about the budget or the escalated flag changes."""
    _enable_auto_escalation(monkeypatch)
    _same_target_env(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)

    story = _make_story(
        plan_dir, status="tests_passed",
        backend=SAME_BACKEND, model=SAME_MODEL,
        rework_attempts=2,  # this cycle's attempts -> 3 == rework_cap
        escalated=False,
    )
    _write_manifest_with_story(plan_dir, "esc", "S1", story)
    calls = _capture_notifications(monkeypatch)
    _force_request_changes(monkeypatch)

    result = p.review_story("esc", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "REQUEST_CHANGES"
    on_disk = _read_story(plan_dir, "esc", "S1")
    assert on_disk["status"] == "parked", (
        f"same-target escalation must park; got {on_disk['status']!r}")
    assert on_disk["parked_reason"] == (
        "rework budget exhausted after 3 review cycles"
    ), f"unexpected parked_reason {on_disk.get('parked_reason')!r}"
    assert on_disk["rework_attempts"] == 3, (
        "the rework budget must be preserved, not reset; got "
        f"{on_disk.get('rework_attempts')!r}")
    assert not on_disk.get("escalated"), (
        "a same-target escalation must not mark the story escalated")
    assert on_disk["backend"] == SAME_BACKEND
    assert on_disk["model"] == SAME_MODEL
    _assert_no_escalation_notification(calls)
    assert any("parked" in msg for _pn, msg in calls), (
        f"expected a story_parked notification, got {calls!r}")

    # Follow-up pass on the same story: the buggy version left escalated=True
    # (which makes the next pass skip the guard entirely) and a reset budget
    # (which grants three more cycles). Re-dispatch it and assert it parks
    # again with the budget still intact and the flag still clear.
    manifest = json.loads((plan_dir / "esc.manifest.json").read_text())
    manifest["stories"]["S1"]["status"] = "tests_passed"
    (plan_dir / "esc.manifest.json").write_text(json.dumps(manifest))

    second = p.review_story("esc", "S1")

    assert second["ok"] is True
    again = _read_story(plan_dir, "esc", "S1")
    assert again["status"] == "parked", (
        f"the follow-up pass must park again; got {again['status']!r}")
    assert not again.get("escalated"), (
        "the follow-up pass must still see escalated unset")
    assert again.get("rework_attempts") == 4, (
        "the follow-up pass must increment the preserved budget (3 -> 4), not "
        f"restart it; got {again.get('rework_attempts')!r}")
    assert again["parked_reason"] == (
        "rework budget exhausted after 4 review cycles"
    ), f"unexpected follow-up parked_reason {again.get('parked_reason')!r}"


# ---------------------------------------------------------------------------
# (b) Review rework cap, DIFFERENT target -> escalation still fires.
# ---------------------------------------------------------------------------

def test_review_rework_cap_different_target_still_escalates(
    plan_dir, agents_dir, monkeypatch,
):
    """The regression bar against over-blocking: when the escalation target
    differs from the story's current pair, the escalation must still rewrite
    backend/model, set escalated, reset the budget and notify."""
    _enable_auto_escalation(monkeypatch)
    _different_target_env(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)

    story = _make_story(
        plan_dir, status="tests_passed",
        backend=SAME_BACKEND, model=SAME_MODEL,
        rework_attempts=2,  # this cycle's attempts -> 3 == rework_cap
        escalated=False,
    )
    _write_manifest_with_story(plan_dir, "esc", "S1", story)
    calls = _capture_notifications(monkeypatch)
    _force_request_changes(monkeypatch)

    result = p.review_story("esc", "S1")

    assert result["ok"] is True
    on_disk = _read_story(plan_dir, "esc", "S1")
    assert on_disk["escalated"] is True, (
        "a different-target escalation must still mark the story escalated")
    assert on_disk["backend"] == OTHER_BACKEND, (
        f"backend must be rewritten to the target; got {on_disk.get('backend')!r}")
    assert on_disk["model"] == OTHER_MODEL, (
        f"model must be rewritten to the target; got {on_disk.get('model')!r}")
    assert "rework_attempts" not in on_disk, (
        "escalation resets the rework budget; got "
        f"{on_disk.get('rework_attempts')!r}")
    assert on_disk["status"] == "changes_requested"
    assert any("escalating to" in msg for _pn, msg in calls), (
        f"expected an escalation notification, got {calls!r}")


# ---------------------------------------------------------------------------
# (c) Inconclusive-review exhaustion, SAME target -> parks.
# ---------------------------------------------------------------------------

def test_inconclusive_exhaustion_same_target_parks(
    plan_dir, agents_dir, monkeypatch,
):
    """The two inconclusive sites must carry the guard too - guarding only the
    rework-cap site leaves them rewriting the identical pair."""
    _enable_auto_escalation(monkeypatch)
    _same_target_env(monkeypatch)
    monkeypatch.setattr(p, "REVIEW_INCONCLUSIVE_MAX", 3)

    story = _make_story(
        plan_dir, status="tests_passed",
        backend=SAME_BACKEND, model=SAME_MODEL,
        review_inconclusive_count=2,  # this cycle -> 3 >= max
        escalated=False,
    )
    _write_manifest_with_story(plan_dir, "esc", "S1", story)
    calls = _capture_notifications(monkeypatch)
    _force_request_changes(monkeypatch, output=_INCONCLUSIVE_OUTPUT)

    result = p.review_story("esc", "S1")

    assert result["ok"] is True
    assert result["verdict"] == "UNKNOWN"
    on_disk = _read_story(plan_dir, "esc", "S1")
    assert on_disk["status"] == "parked", (
        f"same-target inconclusive exhaustion must park; got {on_disk['status']!r}")
    assert "review inconclusive after 3 attempts" in on_disk["parked_reason"], (
        f"unexpected parked_reason {on_disk.get('parked_reason')!r}")
    assert on_disk["review_inconclusive_count"] == 3, (
        "the inconclusive counter must be preserved, not reset; got "
        f"{on_disk.get('review_inconclusive_count')!r}")
    assert not on_disk.get("escalated"), (
        "a same-target escalation must not mark the story escalated")
    assert on_disk["backend"] == SAME_BACKEND
    assert on_disk["model"] == SAME_MODEL
    _assert_no_escalation_notification(calls)


# ---------------------------------------------------------------------------
# (d) No-new-commit exhaustion, SAME target -> parks with the existing reason.
# ---------------------------------------------------------------------------

def _no_new_commit_setup(plan_dir, monkeypatch, *, rework_attempts=1):
    """Scaffolding for the Mode 27 no-new-commit path: dead pid, passing test
    command, HEAD == last_reviewed_sha."""
    worktree = plan_dir / "wt"
    worktree.mkdir()
    (worktree / "agent.log").write_text(
        "[step 1] bash: pytest -q\n"
        "[step 2] bash: pytest -q\n"
        "stuck re-running the same failing test\n"
    )
    story = {
        "summary": "thing",
        "status": "in_progress",
        "pid": 4242,
        "worktree": str(worktree),
        "backend": SAME_BACKEND,
        "model": SAME_MODEL,
        "rework_attempts": rework_attempts,
        "escalated": False,
        "last_reviewed_sha": HEAD_SHA,
        "agent_instructions": "GOAL: build the thing.",
    }
    _write_manifest_with_story(plan_dir, "plan", "S1", story)

    monkeypatch.setattr(
        p.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(p, "detect_test_command", lambda wt: (wt, ["true"]))
    monkeypatch.setattr(p, "_default_branch", lambda: "main")
    monkeypatch.setattr(p, "_worktree_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: "diagnosis")
    _enable_auto_escalation(monkeypatch)

    class Result:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def run_mock(*args, **kwargs):
        if "rev-parse" in args[0]:
            return Result(stdout=HEAD_SHA + "\n")
        return Result()

    monkeypatch.setattr(p.subprocess, "run", run_mock)
    return worktree


def test_no_new_commit_exhaustion_same_target_parks(
    plan_dir, agents_dir, monkeypatch,
):
    """REWORK_MAX_ATTEMPTS_NO_COMMIT is 2; rework_attempts at 1 makes this
    cycle's attempts 2. Same target -> the code after the ``if`` parks with the
    pre-existing reason and leaves the budget and flag alone."""
    _same_target_env(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS_NO_COMMIT", 2)
    _no_new_commit_setup(plan_dir, monkeypatch, rework_attempts=1)
    calls = _capture_notifications(monkeypatch)

    result = p.check_story_status("plan", "S1")

    assert result["status"] == "parked"
    assert result["reason"] == "no_new_commit_rework_budget_exhausted"
    on_disk = _read_story(plan_dir, "plan", "S1")
    assert on_disk["status"] == "parked"
    assert "no new commit after 2 rework redispatches" in on_disk["parked_reason"], (
        f"unexpected parked_reason {on_disk.get('parked_reason')!r}")
    assert on_disk["rework_attempts"] == 2, (
        "the rework budget must be preserved, not reset; got "
        f"{on_disk.get('rework_attempts')!r}")
    assert not on_disk.get("escalated"), (
        "a same-target escalation must not mark the story escalated")
    assert on_disk["backend"] == SAME_BACKEND
    assert on_disk["model"] == SAME_MODEL
    _assert_no_escalation_notification(calls)


# ---------------------------------------------------------------------------
# (e) Triage escalate_model, SAME target -> falls through to the ladder-
#     exhausted park, backend/model untouched.
# ---------------------------------------------------------------------------

def test_triage_escalate_model_same_target_parks_ladder_exhausted(
    plan_dir, monkeypatch,
):
    """The triage site must fall through to the existing ``_park`` rather than
    adding its own else branch with new message text."""
    _same_target_env(monkeypatch)
    monkeypatch.setattr(ptriage, "_auto_escalation_enabled", lambda: True)
    escalate_calls = []
    monkeypatch.setattr(
        ptriage, "_escalate_to_claude",
        lambda *a, **k: escalate_calls.append((a, k)))
    calls = _capture_notifications(monkeypatch)

    story = {
        "summary": "thing",
        "status": "in_progress",
        "backend": SAME_BACKEND,
        "model": SAME_MODEL,
        "escalated": False,
    }
    manifest = {"epics": {}, "stories": {"S1": story}}
    manifest_path = plan_dir / "plan.manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    result = ptriage.execute_ruling(
        "plan", "S1", story,
        {"action": "escalate_model", "rationale": "local tier cannot finish"},
        manifest, manifest_path,
    )

    assert result == "park_for_human"
    assert escalate_calls == [], (
        "a same-target escalation must not call _escalate_to_claude")
    assert story["status"] == "parked"
    assert "escalate_model ruled but ladder exhausted" in story["parked_reason"], (
        f"unexpected parked_reason {story.get('parked_reason')!r}")
    assert story["backend"] == SAME_BACKEND, (
        "the same-target guard must not rewrite the backend")
    assert story["model"] == SAME_MODEL, (
        "the same-target guard must not rewrite the model")
    assert not story.get("escalated")
    _assert_no_escalation_notification(calls)
