"""``review_story(force=True)``: the explicit, audited way to re-run a review.

An unchanged-SHA skip (and the ``not_reviewable_state`` skip) is correct, but an
operator who genuinely wants the reviewer to look again had no sanctioned route:
``patch_story`` refuses ``last_reviewed_sha``, and a hand edit of the manifest
bypasses the plan lock and leaves no trace. ``force=True`` gives that request a
path that records itself.

What ``force`` is allowed to do is narrow, and these tests pin every edge of it:

* it clears exactly three markers - ``last_reviewed_sha``,
  ``last_reviewed_logic_fingerprint`` and ``skip_notified_sha`` - so the next
  review is not skipped, while leaving ``review_verdict``, ``review_feedback``,
  ``rework_attempts`` and every budget counter alone, so a forced review that
  lands ``REQUEST_CHANGES`` counts against the rework budget exactly like any
  other and ``force`` cannot loop past the cap;
* it refuses, changing nothing, while a detached review job holds the story
  (``review_job_running``) - a second review would race the live one - and
  refuses any story that is not at ``tests_passed``
  (``not_reviewable_state``);
* it records exactly one ``story:force_review`` audit entry, and only on the
  success path.

The ordering of the refusal checks matters as much as the refusals themselves:
clearing the markers before checking, or auditing a refused call, leaves a
story with no ``last_reviewed_sha`` - so a later ordinary review silently
becomes a full-branch review - and a follow-up ``force`` would append a second
audit entry. ``test_refused_force_changes_nothing_and_a_follow_up_forces_once``
encodes that whole sequence, including the follow-up call.

The service-level tests drive the REAL ``pipeline.server.review_story`` against
a real git repo with only ``_run_reviewer`` stubbed (fixture shape cribbed from
tests/unit/test_review_scope_gate.py), so the clear-then-review ordering, the
rework-budget increment and the audit entry are all exercised end to end.
"""

import asyncio
import inspect
import json
import subprocess

import pytest

import pipeline.concurrency as pcon
import pipeline.persistence as ppers
import pipeline.server as p
import pipeline.store as pstore
from app.identity import Principal
from pipeline.actor import bind_principal
from pipeline.remote_tool_policy import REMOTE_ENFORCED, remote_tool_policy
from pipeline.review_logic import review_logic_fingerprint

#: A caller the bearer gate authenticated remotely: not the local operator.
ALICE = Principal(subject="https://idp#alice", auth_method="oidc")


class _Ctx:
    """The two attributes the remote policy middleware is allowed to read."""

    def __init__(self, params: object, method: str = "tools/call") -> None:
        self.method = method
        self.params = params


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding the code under test reads at a temp dir.

    ``FileStore`` reads ``pipeline.store.PLAN_DIR`` as a free variable and the
    audit stream reads ``pipeline.persistence.PLAN_DIR`` at call time, so both
    must be patched or the test writes into the operator's real plan dir.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(pstore, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    monkeypatch.delenv("PIPELINE_STORE", raising=False)
    return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_manifest(plan_dir, plan_name, stories):
    """Write a manifest whose ``stories`` mapping is exactly *stories*."""
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories})
    )


def _read_story(plan_dir, plan_name, story_key):
    manifest = json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())
    return manifest["stories"][story_key]


def _audit_records(plan_dir, plan_name, action=None):
    """Return the plan's audit records, oldest first (empty when none)."""
    path = plan_dir / f"{plan_name}.audit.jsonl"
    if not path.exists():
        return []
    records = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    if action is not None:
        records = [record for record in records if record.get("action") == action]
    return records


def _skipped_story(**overrides):
    """A story that an unforced review would skip as unchanged-since-review."""
    story = {
        "summary": "already reviewed",
        "status": "tests_passed",
        "worktree": "/tmp/wt",
        "last_reviewed_sha": "sha-A",
        "last_reviewed_logic_fingerprint": "fp-A",
        "skip_notified_sha": "sha-A",
        "review_verdict": "REQUEST_CHANGES",
        "review_feedback": "fix the thing",
        "rework_attempts": 2,
    }
    story.update(overrides)
    return story


def _git(repo, *args):
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _init_repo(tmp_path):
    """A repo on branch ``main`` with a base commit, then branch ``feature``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "pipeline").mkdir()
    (repo / "pipeline" / "foo.py").write_text("x = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_foo.py").write_text("def test_x():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    # Pin the default branch name: _first_review_base probes main/master.
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-b", "feature")
    (repo / "pipeline" / "foo.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    return repo


def _head_sha(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _reviewed_story(repo, **overrides):
    """A ``tests_passed`` story whose branch was already reviewed at HEAD."""
    head = _head_sha(repo)
    story = {
        "summary": "Add thing",
        "status": "tests_passed",
        "worktree": str(repo),
        "risk": "low",
        "last_reviewed_sha": head,
        "last_reviewed_logic_fingerprint": review_logic_fingerprint(),
        "skip_notified_sha": head,
        "review_verdict": "REQUEST_CHANGES",
        "review_feedback": "the error path is untested",
        "rework_attempts": 2,
    }
    story.update(overrides)
    return story


def _quiet_environment(monkeypatch):
    """No e-mail spooling and no auto-escalation while a review runs."""
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(p, "_auto_escalation_enabled", lambda: False)


def _record_reviewer(monkeypatch, output="Untested.\nVERDICT: REQUEST_CHANGES",
                     on_call=None):
    """Stub ``_run_reviewer``; record each call and optionally observe on entry."""
    calls = []

    def _fake_reviewer(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        if on_call is not None:
            on_call()
        return output

    monkeypatch.setattr(p, "_run_reviewer", _fake_reviewer)
    return calls


def _spy_prepare(monkeypatch):
    """Count ``prepare_forced_review`` calls while still running the real one."""
    from pipeline import review_force

    calls = []
    real = review_force.prepare_forced_review

    def _spy(plan_name, story_key):
        calls.append((plan_name, story_key))
        return real(plan_name, story_key)

    monkeypatch.setattr(review_force, "prepare_forced_review", _spy)
    return calls


# ---------------------------------------------------------------------------
# prepare_forced_review: refusals
# ---------------------------------------------------------------------------

def test_refuses_while_a_review_job_is_running(plan_dir):
    from pipeline.review_force import prepare_forced_review

    _write_manifest(plan_dir, "pl", {"S1": _skipped_story(review_job={"id": "job-7"})})

    result = prepare_forced_review("pl", "S1")

    assert result == {"ok": False, "error": "review_job_running"}
    story = _read_story(plan_dir, "pl", "S1")
    assert story["last_reviewed_sha"] == "sha-A"
    assert story["last_reviewed_logic_fingerprint"] == "fp-A"
    assert story["skip_notified_sha"] == "sha-A"
    assert story["review_job"] == {"id": "job-7"}
    assert story["rework_attempts"] == 2
    assert _audit_records(plan_dir, "pl") == []


def test_refuses_a_story_that_is_not_at_tests_passed(plan_dir):
    from pipeline.review_force import prepare_forced_review

    _write_manifest(plan_dir, "pl", {"S1": _skipped_story(status="in_progress")})

    result = prepare_forced_review("pl", "S1")

    assert result == {"ok": False, "error": "not_reviewable_state"}
    assert "last_reviewed_sha" in _read_story(plan_dir, "pl", "S1")
    assert _audit_records(plan_dir, "pl") == []


def test_live_review_job_is_reported_before_a_wrong_status(plan_dir):
    """A running job is the honest reason; the status check must not pre-empt it."""
    from pipeline.review_force import prepare_forced_review

    _write_manifest(
        plan_dir,
        "pl",
        {"S1": _skipped_story(status="in_progress", review_job={"id": "job-7"})},
    )

    assert prepare_forced_review("pl", "S1") == {
        "ok": False,
        "error": "review_job_running",
    }


def test_unknown_story_is_refused_like_the_unforced_path(plan_dir):
    """A missing story answers exactly as it does without force - no exception."""
    from pipeline.review_force import prepare_forced_review

    _write_manifest(plan_dir, "pl", {"S1": _skipped_story()})

    assert prepare_forced_review("pl", "NOPE") == {
        "ok": False,
        "error": "No such story NOPE",
    }


def test_refused_force_changes_nothing_and_a_follow_up_forces_once(plan_dir):
    """The worked example: refusal leaves no trace, then the retry succeeds once."""
    from pipeline.review_force import prepare_forced_review

    story = _skipped_story(review_job={"id": "job-7"})
    _write_manifest(plan_dir, "pl", {"S1": story})

    first = prepare_forced_review("pl", "S1")

    assert first == {"ok": False, "error": "review_job_running"}
    unchanged = _read_story(plan_dir, "pl", "S1")
    assert unchanged["last_reviewed_sha"] == "sha-A"
    assert unchanged["last_reviewed_logic_fingerprint"] == "fp-A"
    assert unchanged["skip_notified_sha"] == "sha-A"
    assert unchanged["review_job"] == {"id": "job-7"}
    assert unchanged["rework_attempts"] == 2
    assert len(_audit_records(plan_dir, "pl")) == 0

    # The job finishes and drops its handle; now the force is allowed.
    manifest = json.loads((plan_dir / "pl.manifest.json").read_text())
    del manifest["stories"]["S1"]["review_job"]
    (plan_dir / "pl.manifest.json").write_text(json.dumps(manifest))

    second = prepare_forced_review("pl", "S1")

    assert second == {"ok": True}
    forced = _read_story(plan_dir, "pl", "S1")
    assert "last_reviewed_sha" not in forced
    assert "last_reviewed_logic_fingerprint" not in forced
    assert "skip_notified_sha" not in forced
    assert forced["rework_attempts"] == 2
    assert len(_audit_records(plan_dir, "pl")) == 1


# ---------------------------------------------------------------------------
# prepare_forced_review: the success path
# ---------------------------------------------------------------------------

def test_success_clears_exactly_the_three_markers(plan_dir):
    from pipeline.review_force import prepare_forced_review

    _write_manifest(plan_dir, "pl", {"S1": _skipped_story()})

    assert prepare_forced_review("pl", "S1") == {"ok": True}

    story = _read_story(plan_dir, "pl", "S1")
    assert "last_reviewed_sha" not in story
    assert "last_reviewed_logic_fingerprint" not in story
    assert "skip_notified_sha" not in story
    # Everything else is untouched: force may not reset the rework budget.
    assert story["rework_attempts"] == 2
    assert story["review_verdict"] == "REQUEST_CHANGES"
    assert story["review_feedback"] == "fix the thing"
    assert story["status"] == "tests_passed"
    assert story["worktree"] == "/tmp/wt"


def test_success_records_exactly_one_audit_entry(plan_dir):
    from pipeline.review_force import prepare_forced_review

    _write_manifest(plan_dir, "pl", {"S1": _skipped_story()})

    prepare_forced_review("pl", "S1")

    records = _audit_records(plan_dir, "pl")
    assert len(records) == 1
    assert records[0]["action"] == "story:force_review"
    assert records[0]["outcome"] == "forced"
    assert records[0]["target"] == {"story": "S1"}


def test_force_keeps_budget_counters_so_request_changes_still_counts(plan_dir):
    """A forced review that lands REQUEST_CHANGES increments the same counter."""
    from pipeline.review_force import prepare_forced_review

    _write_manifest(
        plan_dir,
        "pl",
        {
            "S1": _skipped_story(
                rework_attempts=2,
                review_deferred_count=1,
                review_inconclusive_count=1,
            )
        },
    )

    prepare_forced_review("pl", "S1")

    story = _read_story(plan_dir, "pl", "S1")
    assert story["rework_attempts"] == 2
    assert story["review_deferred_count"] == 1
    assert story["review_inconclusive_count"] == 1


# ---------------------------------------------------------------------------
# PipelineService.review_story(force=...)
# ---------------------------------------------------------------------------

def test_force_runs_the_reviewer_once_as_a_full_branch_review(plan_dir, monkeypatch):
    """A forced review is not skipped, and it is a FIRST-style full-branch review."""
    _quiet_environment(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    story = _reviewed_story(repo)
    _write_manifest(plan_dir, "pe", {"S1": story})

    result = p.review_story("pe", "S1", force=True)

    assert len(reviewer_calls) == 1
    # No last_reviewed_sha survived into the reviewer call: a first-style
    # full-branch review, not a since_sha rework diff.
    assert reviewer_calls[0]["kwargs"]["since_sha"] is None
    assert not result.get("skipped")
    stored = _read_story(plan_dir, "pe", "S1")
    # skip_notified_sha is only ever written on a skip, so its absence proves
    # the marker was cleared rather than the story skipping again.
    assert "skip_notified_sha" not in stored


def test_without_force_the_unchanged_review_is_still_skipped(plan_dir, monkeypatch):
    """force=False is today's behaviour, byte for byte: skip, reviewer never runs."""
    _quiet_environment(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)
    prepare_calls = _spy_prepare(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    story = _reviewed_story(repo)
    _write_manifest(plan_dir, "pe", {"S1": story})
    head = _head_sha(repo)

    result = p.review_story("pe", "S1")

    assert result["ok"] is True
    assert result["skipped"] == "unchanged_since_last_review"
    assert result["status"] == "tests_passed"
    assert reviewer_calls == []
    assert prepare_calls == []
    assert _read_story(plan_dir, "pe", "S1")["last_reviewed_sha"] == head


def test_force_is_refused_while_a_review_job_is_running(plan_dir, monkeypatch):
    _quiet_environment(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    head = _head_sha(repo)
    story = _reviewed_story(repo, review_job={"id": "job-7"})
    _write_manifest(plan_dir, "pe", {"S1": story})

    result = p.review_story("pe", "S1", force=True)

    assert result == {"ok": False, "error": "review_job_running"}
    assert reviewer_calls == []
    stored = _read_story(plan_dir, "pe", "S1")
    assert stored["last_reviewed_sha"] == head
    assert stored["skip_notified_sha"] == head
    assert stored["review_job"] == {"id": "job-7"}
    assert _audit_records(plan_dir, "pe", action="story:force_review") == []

    # The refusal left no state behind: an ordinary call is still a plain skip.
    assert p.review_story("pe", "S1")["skipped"] == "unchanged_since_last_review"
    assert reviewer_calls == []


def test_force_is_refused_on_a_story_that_is_not_tests_passed(plan_dir, monkeypatch):
    _quiet_environment(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    story = _reviewed_story(repo, status="in_progress")
    _write_manifest(plan_dir, "pe", {"S1": story})

    result = p.review_story("pe", "S1", force=True)

    assert result == {"ok": False, "error": "not_reviewable_state"}
    assert reviewer_calls == []
    assert _read_story(plan_dir, "pe", "S1")["last_reviewed_sha"] == _head_sha(repo)
    assert _audit_records(plan_dir, "pe", action="story:force_review") == []


def test_a_forced_review_records_exactly_one_audit_entry(plan_dir, monkeypatch):
    _quiet_environment(monkeypatch)
    _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _reviewed_story(repo)})

    p.review_story("pe", "S1", force=True)

    records = _audit_records(plan_dir, "pe", action="story:force_review")
    assert len(records) == 1
    assert records[0]["target"] == {"story": "S1"}


def test_forced_request_changes_still_counts_against_the_rework_budget(
    plan_dir, monkeypatch
):
    """force must not reset the budget, so it cannot be used to loop past the cap."""
    _quiet_environment(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 5)
    _record_reviewer(monkeypatch, output="Untested.\nVERDICT: REQUEST_CHANGES")
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _reviewed_story(repo, rework_attempts=2)})

    result = p.review_story("pe", "S1", force=True)

    assert result["status"] == "changes_requested"
    stored = _read_story(plan_dir, "pe", "S1")
    assert stored["rework_attempts"] == 3
    assert stored["review_feedback"]


def test_a_forced_review_cannot_loop_past_the_rework_cap(plan_dir, monkeypatch):
    """At the cap, the forced review parks the story: force resets no counter."""
    _quiet_environment(monkeypatch)
    monkeypatch.setattr(p, "REWORK_MAX_ATTEMPTS", 3)
    _record_reviewer(monkeypatch, output="Untested.\nVERDICT: REQUEST_CHANGES")
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _reviewed_story(repo, rework_attempts=2)})

    p.review_story("pe", "S1", force=True)

    stored = _read_story(plan_dir, "pe", "S1")
    assert stored["rework_attempts"] == 3
    assert stored["status"] == "parked"


def test_the_reviewer_still_sees_the_previous_verdict(plan_dir, monkeypatch):
    """force clears the skip markers only - never the verdict it is re-checking."""
    _quiet_environment(monkeypatch)
    seen = []
    _record_reviewer(
        monkeypatch,
        on_call=lambda: seen.append(_read_story(plan_dir, "pe", "S1")),
    )
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _reviewed_story(repo)})

    p.review_story("pe", "S1", force=True)

    assert len(seen) == 1
    assert seen[0]["review_verdict"] == "REQUEST_CHANGES"
    assert seen[0]["review_feedback"] == "the error path is untested"
    assert seen[0]["rework_attempts"] == 2
    assert "last_reviewed_sha" not in seen[0]


# ---------------------------------------------------------------------------
# The MCP tool wrapper and its callers
# ---------------------------------------------------------------------------

def test_force_defaults_to_false_on_both_entry_points():
    """Every existing caller passes two arguments, so force must stay optional."""
    from pipeline.server_tools_lifecycle import review_story as tool_review_story
    from pipeline.service import PipelineService

    for func in (tool_review_story, PipelineService.review_story):
        parameter = inspect.signature(func).parameters["force"]
        assert parameter.default is False


def test_the_tool_passes_force_through_to_the_service(monkeypatch):
    import pipeline.server_tools_lifecycle as lifecycle

    calls = []

    class _FakeService:
        def review_story(self, plan_name, story_key, force=False):
            calls.append((plan_name, story_key, force))
            return {"ok": True}

    monkeypatch.setattr(p, "_service", _FakeService())

    assert lifecycle.review_story("pe", "S1", force=True) == {"ok": True}
    assert calls == [("pe", "S1", True)]


def test_the_two_argument_call_used_by_review_job_still_works(plan_dir, monkeypatch):
    """pipeline/review_job.py:357 calls review_story(plan_name, story_key)."""
    _quiet_environment(monkeypatch)
    reviewer_calls = _record_reviewer(monkeypatch)
    repo = _init_repo(plan_dir.parent)
    _write_manifest(plan_dir, "pe", {"S1": _reviewed_story(repo)})

    result = p.review_story("pe", "S1")

    assert result["skipped"] == "unchanged_since_last_review"
    assert reviewer_calls == []


def test_force_does_not_add_a_new_action_or_bypass_authorization(plan_dir, monkeypatch):
    """A forced review is still decided as plan:dispatch, and a denial stands."""
    from pipeline import authorization

    reviewed = []
    monkeypatch.setattr(
        authorization,
        "authorize",
        lambda action, **kwargs: reviewed.append(action) or {"ok": False,
                                                             "error": "not authorized"},
    )
    reviewer_calls = _record_reviewer(monkeypatch)
    _write_manifest(plan_dir, "pe", {"S1": _skipped_story()})

    result = p.review_story("pe", "S1", force=True)

    assert result == {"ok": False, "error": "not authorized"}
    assert reviewed == ["plan:dispatch"]
    assert reviewer_calls == []


def test_review_story_is_still_reachable_through_the_remote_policy():
    """force adds no new permission: the remote path is authorized as before.

    Drives the real middleware the way tests/unit/test_remote_tool_policy.py
    does, with an OIDC caller, and asserts ``review_story`` is still classified
    as an enforced tool and still reaches its handler.
    """
    assert "review_story" in REMOTE_ENFORCED
    reached = []

    async def _call_next(ctx):
        reached.append(ctx)
        return {"ok": True}

    async def _run():
        with bind_principal(ALICE):
            return await remote_tool_policy(
                _Ctx({"name": "review_story"}), _call_next
            )

    assert asyncio.run(_run()) == {"ok": True}
    assert len(reached) == 1
