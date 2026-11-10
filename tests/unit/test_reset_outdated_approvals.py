"""Tests for the one-time re-approval command (ADR 0007 / ENT-LC-01).

``python -m pipeline.tools.reset_outdated_approvals`` repairs the records a
digest-version bump invalidated. It **never approves anything**: an approved
plan recorded under an older version leaves the command as
``pending_approval`` and must be re-approved by a person who can now see the
descriptions and team the digest covers.

The tests stub ``APPROVAL_DIGEST_VERSION`` to 2, so they keep their meaning
when the real version is bumped: version 1 is the "older" version here.
"""
import fcntl
import json
import os
from pathlib import Path

import pytest

from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import plan_approval as pa
from pipeline import server as p
from pipeline.plan_approval import canonical_digest
from pipeline.tools import reset_outdated_approvals as reset

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "specs" / "PLAN_APPROVAL.md"
REFERENCE_PATH = Path(__file__).resolve().parents[2] / "REFERENCE.md"

PLAN = "ro1"
OTHER = "ro2"
COMMAND = "python -m pipeline.tools.reset_outdated_approvals"

#: The sentence the docs must carry verbatim: the whole point of the command.
NEVER_APPROVES = "It never approves a plan."

#: Decision metadata the reset must clear, exactly as ``reset_if_changed``
#: clears it. ``decided_at`` is the legacy alias of ``at``; ``reason`` is a
#: ``changes_requested`` decision and must not outlive it.
_CLEARED = ("decided_by", "at", "decided_at", "correlation_id", "role", "self_approved", "reason")


def _manifest(**overrides):
    manifest = {
        "repo_root": "/tmp/repo",
        "role_config": {},
        "context": "ctx",
        "stories": {
            "S1": {
                "summary": "do the thing",
                "agent_instructions": "instructions",
                "dependencies": [],
                "persona": "engineer",
                "model": None,
                "acceptance": ["it works"],
                "risk": "low",
                "backend": "claude",
                "tdd_split": True,
                "files": ["a.py"],
                "blocked_by": [],
                "status": "todo",
            }
        },
    }
    manifest.update(overrides)
    return manifest


def _decision(manifest, status, version=1, **extra):
    """An approval record exactly as ``_decide_plan`` writes it."""
    record = {
        "plan": PLAN,
        "status": status,
        "digest": canonical_digest(manifest),
        "digest_version": version,
        "decided_by": "local-operator",
        "at": "2026-01-01T00:00:00+00:00",
        "correlation_id": "corr-1",
        "role": "local-operator",
        "self_approved": True,
        "warnings_seen": ["w1"],
    }
    record.update(extra)
    return record


def _write(plan_dir, manifest, plan_name=PLAN):
    manifest = dict(manifest)
    path = plan_dir / f"{plan_name}.manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


def _stored(plan_dir, plan_name=PLAN):
    return json.loads((plan_dir / f"{plan_name}.manifest.json").read_text())


def _approval(plan_dir, plan_name=PLAN):
    return _stored(plan_dir, plan_name)["approval"]


def _bytes(plan_dir, plan_name=PLAN):
    return (plan_dir / f"{plan_name}.manifest.json").read_bytes()


def _audit(plan_dir, plan_name=PLAN):
    path = plan_dir / f"{plan_name}.audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _outcomes(result):
    return {entry["plan"]: entry["outcome"] for entry in result["plans"]}


@pytest.fixture(autouse=True)
def _gate_on_and_version_two(monkeypatch):
    """Gate on and a stubbed current version of 2: version 1 records are old."""
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    monkeypatch.setenv("PIPELINE_POLICY_ENFORCE", "0")
    monkeypatch.setattr(pa, "APPROVAL_DIGEST_VERSION", 2, raising=False)


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding the command touches at ``tmp_path``."""
    for module in (p, ppers, pcon):
        monkeypatch.setattr(module, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def notices(monkeypatch):
    """Record every notice ``plan_approval_notice`` delivers."""
    recorded = []

    def _stub(plan_name, message, **kwargs):
        recorded.append({"plan_name": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(p, "_notify_user", _stub)
    return recorded


@pytest.fixture
def approved_v1(plan_dir):
    manifest = _manifest()
    manifest["approval"] = _decision(manifest, "approved", version=1)
    _write(plan_dir, manifest)
    return plan_dir


# ---------------------------------------------------------------------------
# approved under an older version -> pending_approval


def test_should_reset_an_approved_record_to_pending_approval(approved_v1):
    result = reset.run()

    assert _outcomes(result) == {PLAN: "reset_to_pending"}
    approval = _approval(approved_v1)
    assert approval["status"] == "pending_approval"
    assert approval["digest"] == canonical_digest(_stored(approved_v1))
    assert approval["digest_version"] == 2


def test_should_clear_the_previous_decision_metadata(approved_v1):
    reset.run()

    approval = _approval(approved_v1)

    for field in _CLEARED:
        assert field not in approval, field
    assert approval["warnings_seen"] == ["w1"]
    assert approval["reset_reason"] == "digest_version_upgrade"


def test_should_append_exactly_one_audit_event(approved_v1):
    reset.run()

    records = _audit(approved_v1)

    assert len(records) == 1
    assert records[0]["action"] == "plan.approval_reset"
    assert records[0]["outcome"] == "allowed"
    assert records[0]["detail"] == {
        "from_version": 1,
        "to_version": 2,
        "previous_status": "approved",
        "outcome": "reset_to_pending",
    }


def test_should_send_exactly_one_pending_approval_notice(approved_v1, notices):
    reset.run()

    assert len(notices) == 1
    assert notices[0]["plan_name"] == PLAN
    assert notices[0]["event"] == "plan_pending_approval"


def test_should_leave_the_plan_activatable_only_after_a_new_approval(approved_v1):
    reset.run()

    assert pa.is_activatable(_stored(approved_v1)) is False


def test_should_leave_the_plan_reapprovable_by_a_person(approved_v1):
    """The command must not strand the plan: a fresh decision still works."""
    reset.run()
    digest = canonical_digest(_stored(approved_v1))

    from pipeline.service import PipelineService

    result = PipelineService().approve_plan(PLAN, expected_digest=digest)

    assert result["ok"] is True, result
    stored = _stored(approved_v1)
    assert stored["approval"]["status"] == "approved"
    assert stored["approval"]["digest_version"] == 2
    assert pa.is_activatable(stored) is True


# ---------------------------------------------------------------------------
# pending / changes_requested under an older version -> restamped


def test_should_restamp_a_pending_record_and_keep_it_pending(plan_dir):
    manifest = _manifest()
    manifest["approval"] = {
        "status": "pending_approval",
        "digest": "stale-digest",
        "digest_version": 1,
        "warnings_seen": ["w1"],
    }
    _write(plan_dir, manifest)

    result = reset.run()

    assert _outcomes(result) == {PLAN: "restamped"}
    approval = _approval(plan_dir)
    assert approval["status"] == "pending_approval"
    assert approval["digest"] == canonical_digest(_stored(plan_dir))
    assert approval["digest_version"] == 2


def test_should_restamp_a_changes_requested_record_and_keep_the_reason(plan_dir):
    manifest = _manifest()
    manifest["approval"] = _decision(
        manifest, "changes_requested", version=1, reason="split the story"
    )
    _write(plan_dir, manifest)

    result = reset.run()

    assert _outcomes(result) == {PLAN: "restamped"}
    approval = _approval(plan_dir)
    assert approval["status"] == "changes_requested"
    assert approval["reason"] == "split the story"
    assert approval["digest"] == canonical_digest(_stored(plan_dir))
    assert approval["digest_version"] == 2
    assert pa.is_activatable(_stored(plan_dir)) is False


def test_should_audit_a_restamp_without_notifying(plan_dir, notices):
    manifest = _manifest()
    manifest["approval"] = _decision(manifest, "changes_requested", version=1)
    _write(plan_dir, manifest)

    reset.run()

    records = _audit(plan_dir)
    assert len(records) == 1
    assert records[0]["detail"]["previous_status"] == "changes_requested"
    assert records[0]["detail"]["outcome"] == "restamped"
    assert notices == []


# ---------------------------------------------------------------------------
# nothing to do


def test_should_leave_a_current_version_record_untouched(plan_dir, notices):
    manifest = _manifest()
    manifest["approval"] = _decision(manifest, "approved", version=2)
    path = _write(plan_dir, manifest)
    before = path.read_bytes()

    result = reset.run()

    assert _outcomes(result) == {PLAN: "current"}
    assert path.read_bytes() == before
    assert _audit(plan_dir) == []
    assert notices == []


def test_should_leave_a_plan_without_an_approval_record_untouched(plan_dir):
    path = _write(plan_dir, _manifest())
    before = path.read_bytes()

    result = reset.run()

    assert _outcomes(result) == {PLAN: "no_record"}
    assert path.read_bytes() == before
    assert _audit(plan_dir) == []


def test_should_report_nothing_for_an_empty_plan_dir(plan_dir):
    result = reset.run()

    assert result["plans"] == []
    assert result["summary"] == {
        "reset_to_pending": 0,
        "restamped": 0,
        "current": 0,
        "no_record": 0,
        "skipped": 0,
    }
    assert reset.main([]) == 0


def test_should_be_idempotent(approved_v1):
    reset.run()
    after_first = _bytes(approved_v1)
    audit_after_first = _audit(approved_v1)

    result = reset.run()

    assert _outcomes(result) == {PLAN: "current"}
    assert _bytes(approved_v1) == after_first
    assert _audit(approved_v1) == audit_after_first


def test_should_exit_zero_when_nothing_was_skipped(approved_v1):
    assert reset.main([]) == 0


# ---------------------------------------------------------------------------
# --dry-run


def test_dry_run_should_report_the_same_outcomes_and_write_nothing(approved_v1, notices):
    before = _bytes(approved_v1)

    result = reset.run(dry_run=True)

    assert _outcomes(result) == {PLAN: "reset_to_pending"}
    assert _bytes(approved_v1) == before
    assert _audit(approved_v1) == []
    assert notices == []


def test_dry_run_should_report_every_outcome(plan_dir, notices):
    approved = _manifest()
    approved["approval"] = _decision(approved, "approved", version=1)
    _write(plan_dir, approved, PLAN)
    pending = _manifest()
    pending["approval"] = _decision(pending, "pending_approval", version=1)
    _write(plan_dir, pending, "ro-pending")
    current = _manifest()
    current["approval"] = _decision(current, "approved", version=2)
    _write(plan_dir, current, "ro-current")
    _write(plan_dir, _manifest(), "ro-none")

    result = reset.run(dry_run=True)

    assert _outcomes(result) == {
        PLAN: "reset_to_pending",
        "ro-pending": "restamped",
        "ro-current": "current",
        "ro-none": "no_record",
    }
    assert result["summary"] == {
        "reset_to_pending": 1,
        "restamped": 1,
        "current": 1,
        "no_record": 1,
        "skipped": 0,
    }
    assert notices == []


# ---------------------------------------------------------------------------
# skips


@pytest.fixture
def held_lock(plan_dir):
    """Hold the raw plan lock from this process without the reentrant bookkeeping.

    ``_plan_lock`` treats a *thread-local* hold as reentrant, so the test takes
    the flock directly: the command must then report ``skipped_locked``.
    """
    path = plan_dir / f"{PLAN}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    yield
    os.close(fd)


def test_should_skip_a_locked_plan_and_exit_one(approved_v1, held_lock):
    before = _bytes(approved_v1)

    result = reset.run()

    assert _outcomes(result) == {PLAN: "skipped_locked"}
    assert result["summary"]["skipped"] == 1
    assert _bytes(approved_v1) == before
    assert _audit(approved_v1) == []
    assert reset.main([]) == 1


def test_should_skip_an_unreadable_manifest_and_exit_one(plan_dir):
    path = plan_dir / f"{PLAN}.manifest.json"
    path.write_text("{not json")

    result = reset.run()

    assert _outcomes(result) == {PLAN: "skipped_unreadable"}
    assert result["summary"]["skipped"] == 1
    assert path.read_text() == "{not json"
    assert _audit(plan_dir) == []
    assert reset.main([]) == 1


# ---------------------------------------------------------------------------
# fail closed


def test_should_reset_a_malformed_record_as_if_it_were_approved(plan_dir):
    manifest = _manifest()
    manifest["approval"] = "x"
    _write(plan_dir, manifest)

    result = reset.run()

    assert _outcomes(result) == {PLAN: "reset_to_pending"}
    approval = _approval(plan_dir)
    assert approval["status"] == "pending_approval"
    assert approval["digest_version"] == 2
    assert _audit(plan_dir)[0]["detail"]["from_version"] is None


def test_should_reset_an_unknown_status_as_if_it_were_approved(plan_dir):
    manifest = _manifest()
    manifest["approval"] = _decision(manifest, "weird", version=1)
    _write(plan_dir, manifest)

    result = reset.run()

    assert _outcomes(result) == {PLAN: "reset_to_pending"}
    approval = _approval(plan_dir)
    assert approval["status"] == "pending_approval"
    for field in _CLEARED:
        assert field not in approval, field


_SCENARIOS = {
    "approved_v1": lambda manifest: _decision(manifest, "approved", version=1),
    "pending_v1": lambda manifest: _decision(manifest, "pending_approval", version=1),
    "changes_requested_v1": lambda manifest: _decision(
        manifest, "changes_requested", version=1, reason="no"
    ),
    "malformed_string": lambda manifest: "x",
    "malformed_status": lambda manifest: _decision(manifest, "weird", version=1),
    "malformed_version": lambda manifest: _decision(
        manifest, "approved", version="two"
    ),
}


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_should_never_write_the_approved_status(plan_dir, scenario):
    """Every record the command *writes* is decidable, never approved.

    A record already written under the current version is only *reported*
    (``current``, byte-identical), which is why ``approved_v2`` is not here:
    that plan keeps the approval a person made under this build's version.
    """
    manifest = _manifest()
    manifest["approval"] = _SCENARIOS[scenario](manifest)
    _write(plan_dir, manifest)

    result = reset.run()

    outcome = _outcomes(result)[PLAN]
    assert outcome != "current"
    assert outcome != "no_record"
    assert _approval(plan_dir)["status"] != "approved"


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_should_write_nothing_on_a_dry_run(plan_dir, scenario, notices):
    manifest = _manifest()
    manifest["approval"] = _SCENARIOS[scenario](manifest)
    path = _write(plan_dir, manifest)
    before = path.read_bytes()

    reset.run(dry_run=True)

    assert path.read_bytes() == before
    assert _audit(plan_dir) == []
    assert notices == []


# ---------------------------------------------------------------------------
# output and exit code


def test_should_print_one_json_line_per_plan_then_a_summary(plan_dir, capsys):
    approved = _manifest()
    approved["approval"] = _decision(approved, "approved", version=1)
    _write(plan_dir, approved, PLAN)
    current = _manifest()
    current["approval"] = _decision(current, "approved", version=2)
    _write(plan_dir, current, OTHER)

    assert reset.main([]) == 0

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert lines == [
        {"plan": PLAN, "outcome": "reset_to_pending"},
        {"plan": OTHER, "outcome": "current"},
        {
            "reset_to_pending": 1,
            "restamped": 0,
            "current": 1,
            "no_record": 0,
            "skipped": 0,
        },
    ]


# ---------------------------------------------------------------------------
# docs


def test_should_name_the_command_and_the_never_approves_sentence_in_the_spec():
    text = DOC_PATH.read_text()

    assert COMMAND in text
    assert NEVER_APPROVES in text
    assert "#### Re-approval after a digest version change" in text
    for outcome in (
        "reset_to_pending",
        "restamped",
        "current",
        "no_record",
        "skipped_locked",
        "skipped_unreadable",
    ):
        assert outcome in text, outcome


def test_should_name_the_command_in_the_operator_reference():
    text = REFERENCE_PATH.read_text()

    assert COMMAND in text
