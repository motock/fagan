"""Every enforced authorization decision lands in the audit stream (AUD-02).

``pipeline.authorization`` is the single place the service asks the policy for a
decision, so it is also the single place that has to leave a tamper-evident
trail of it: an allowed decision and a denied one each append exactly one
record to the plan's hash-chained audit stream. Two properties matter beyond
the record's contents:

* a check that never happened records nothing -- enforcement disabled means no
  record at all, because an "allowed" line for a skipped check is a false
  statement in an audit trail;
* a failing audit write never changes the decision -- the allowed call still
  returns ``None`` and the denied call still returns its refusal dict.

pipeline.server must load before pipeline.ingest; see the note in
``tests/unit/test_authorization.py``.
"""

from __future__ import annotations

import json

import pytest

import pipeline.server  # noqa: F401 - see the module docstring's import note
from app.identity import LOCAL_OPERATOR
from pipeline import audit, concurrency, persistence, server
from pipeline.actor import bind_correlation_id, bind_principal
from pipeline.authorization import ENFORCE_ENV, authorize


@pytest.fixture(autouse=True)
def plan_dirs(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding at a throwaway directory.

    ``pipeline_persistence`` and ``pipeline_concurrency`` import ``PLAN_DIR``
    from ``pipeline_paths`` at module load and read it as a free variable, so
    the patch has to land on their own bindings too. ``pipeline.audit`` reads
    ``persistence.PLAN_DIR`` at call time, so it follows automatically.
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(server, "PLAN_DIR", d)
    monkeypatch.setattr(persistence, "PLAN_DIR", d)
    monkeypatch.setattr(concurrency, "PLAN_DIR", d)
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    return d


def _records(plan_name: str) -> list[dict]:
    """Read the plan's audit stream, one decoded dict per line."""
    path = audit.audit_path(plan_name)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _sidecar(plan_name: str) -> list[dict]:
    """Read the plan's notification sidecar, one decoded dict per line."""
    path = persistence._notifications_jsonl_path(plan_name)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _allow() -> None:
    """Ask the policy for an action the local operator is permitted."""
    with bind_principal(LOCAL_OPERATOR):
        authorize("plan:approve", plan_name="p1", story_key="S-1", manifest={})


def _deny() -> dict | None:
    """Ask the policy for an action a non-operator principal is denied."""
    with bind_principal(_alice()):
        return authorize("plan:approve", plan_name="p1", story_key="S-1", manifest={})


def _alice():
    from app.identity import Principal

    return Principal(subject="alice", roles={"approver"})


# ---------- an allowed decision ----------


def test_allowed_action_appends_exactly_one_record() -> None:
    _allow()
    assert len(_records("p1")) == 1


def test_allowed_record_names_the_action() -> None:
    _allow()
    assert _records("p1")[0]["action"] == "plan:approve"


def test_allowed_record_has_allowed_outcome() -> None:
    _allow()
    assert _records("p1")[0]["outcome"] == "allowed"


def test_allowed_record_names_the_principal() -> None:
    _allow()
    assert _records("p1")[0]["detail"]["principal"] == LOCAL_OPERATOR.subject


def test_allowed_record_names_the_plan() -> None:
    _allow()
    assert _records("p1")[0]["target"]["plan"] == "p1"


# ---------- a denied decision ----------


def test_denied_action_appends_exactly_one_record() -> None:
    _deny()
    assert len(_records("p1")) == 1


def test_denied_record_has_denied_outcome() -> None:
    _deny()
    assert _records("p1")[0]["outcome"] == "denied"


def test_denied_action_still_returns_the_refusal_dict() -> None:
    refusal = _deny()
    assert refusal["ok"] is False and refusal["error"]


def test_denied_action_still_writes_the_notification_sidecar() -> None:
    _deny()
    assert len(_sidecar("p1")) == 1


def test_denied_decision_is_named_in_both_files() -> None:
    _deny()
    sidecar_names_principal = "alice" in json.dumps(_sidecar("p1"))
    audit_names_principal = _records("p1")[0]["detail"]["principal"] == "alice"
    assert sidecar_names_principal and audit_names_principal


# ---------- enforcement disabled: nothing is recorded ----------


def test_disabled_enforcement_creates_no_audit_file(monkeypatch) -> None:
    monkeypatch.setenv(ENFORCE_ENV, "0")
    _deny()
    assert not audit.audit_path("p1").exists()


def test_disabled_enforcement_still_returns_none(monkeypatch) -> None:
    monkeypatch.setenv(ENFORCE_ENV, "0")
    assert _deny() is None


def test_recording_resumes_once_enforcement_is_re_enabled(monkeypatch) -> None:
    monkeypatch.setenv(ENFORCE_ENV, "0")
    _deny()
    monkeypatch.delenv(ENFORCE_ENV)
    _allow()
    assert len(_records("p1")) == 1


# ---------- the story scope ----------


def test_story_key_is_recorded_in_the_target() -> None:
    _allow()
    assert _records("p1")[0]["target"]["story"] == "S-1"


def test_omitted_story_key_is_recorded_as_empty_string() -> None:
    with bind_principal(LOCAL_OPERATOR):
        authorize("plan:approve", plan_name="p1", manifest={})
    assert _records("p1")[0]["target"]["story"] == ""


# ---------- the correlation id ----------


def test_correlation_id_is_the_bound_value() -> None:
    with bind_correlation_id("req-7"):
        _allow()
        assert _records("p1")[0]["correlation_id"] == "req-7"


def test_correlation_id_is_empty_outside_the_block() -> None:
    with bind_correlation_id("req-7"):
        _allow()
    _allow()
    assert _records("p1")[1]["correlation_id"] == ""


# ---------- a failing audit write never changes the decision ----------


def test_failing_audit_write_leaves_an_allowed_decision_unchanged() -> None:
    def raiser(*args, **kwargs):
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(audit, "append_audit", raiser)
        assert _allow() is None


def test_failing_audit_write_leaves_a_denied_decision_unchanged() -> None:
    def raiser(*args, **kwargs):
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(audit, "append_audit", raiser)
        refusal = _deny()
    assert refusal["ok"] is False and refusal["error"]


def test_failing_audit_write_leaves_no_partial_record() -> None:
    def raiser(*args, **kwargs):
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(audit, "append_audit", raiser)
        _allow()
        assert len(_records("p1")) == 0


def test_denial_is_still_notified_when_the_audit_write_fails() -> None:
    def raiser(*args, **kwargs):
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(audit, "append_audit", raiser)
        _deny()
        assert len(_sidecar("p1")) == 1


def test_audit_recording_resumes_after_a_failed_write() -> None:
    def raiser(*args, **kwargs):
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(audit, "append_audit", raiser)
        _allow()
    _allow()
    assert len(_records("p1")) == 1
