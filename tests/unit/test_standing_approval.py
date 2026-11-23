"""Tests for ``pipeline.standing_approval.approve_standing`` (sap-01).

The standing approval is an in-process call the scheduler makes on a named
person's behalf. It is off unless ``PIPELINE_STANDING_APPROVAL`` is exactly
``1``, and every refusal leaves the manifest and decisions log untouched and
writes exactly one ``refused`` audit record that carries a reason token and no
plan content.
"""

import contextlib
import json

import pytest

from app.identity import LOCAL_OPERATOR, Principal
from pipeline import actor, persistence, plan_approval, server
from pipeline.plan_approval import canonical_digest
from pipeline.standing_approval import STANDING_ENV, approve_standing

PLAN = "p"
APPROVER = "alice@example.test"
INGESTER = "ingester@example.test"
STANDING_ID = "standing-1"
HUMAN = Principal(subject="bob", roles=frozenset({"approver"}), auth_method="oidc")
NETWORK_CLIENT = Principal(
    subject="svc-client",
    auth_method="shared-secret",
    principal_type="agent",
    team_scope="org-1/team-1",
)


def _manifest():
    return {
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


@pytest.fixture
def plan(tmp_path, monkeypatch):
    """A pending plan whose digest is current, with PLAN_DIR redirected."""
    monkeypatch.setattr(server, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    manifest = _manifest()
    manifest["ingested_by"] = INGESTER
    manifest["approval"] = {
        "status": "pending_approval",
        "digest": canonical_digest(manifest),
        "digest_version": plan_approval.APPROVAL_DIGEST_VERSION,
        "warnings_seen": ["w1"],
    }
    (tmp_path / f"{PLAN}.manifest.json").write_text(json.dumps(manifest))
    return tmp_path


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(STANDING_ENV, "1")
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")


def _read_manifest(plan_dir):
    return json.loads((plan_dir / f"{PLAN}.manifest.json").read_text())


def _write_manifest(plan_dir, manifest):
    (plan_dir / f"{PLAN}.manifest.json").write_text(json.dumps(manifest))


def _set_approval(plan_dir, **fields):
    manifest = _read_manifest(plan_dir)
    manifest["approval"] = {**manifest["approval"], **fields}
    _write_manifest(plan_dir, manifest)


def _decisions(plan_dir):
    path = plan_dir / f"{PLAN}.decisions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _audit_records(plan_dir, plan_name=PLAN):
    path = plan_dir / f"{plan_name}.audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _digest(plan_dir):
    return canonical_digest(_read_manifest(plan_dir))


def _call(expected_digest, **overrides):
    kwargs = {
        "standing_id": STANDING_ID,
        "approver_subject": APPROVER,
        "expected_digest": expected_digest,
        "still_valid": lambda: True,
    }
    kwargs.update(overrides)
    return approve_standing(PLAN, **kwargs)


def _assert_refused(plan_dir, before_bytes, reason):
    """A refusal writes no manifest and no decision, and one refused audit record."""
    assert (plan_dir / f"{PLAN}.manifest.json").read_bytes() == before_bytes
    assert _decisions(plan_dir) == []
    records = _audit_records(plan_dir)
    assert len(records) == 1
    assert records[0]["action"] == "plan.approved"
    assert records[0]["outcome"] == "refused"
    assert records[0]["detail"] == {"reason": reason}


def _manifest_bytes(plan_dir):
    return (plan_dir / f"{PLAN}.manifest.json").read_bytes()


def _restamp(plan_dir, *, drop=(), **fields):
    """Change manifest fields and re-stamp the pending digest to match them."""
    manifest = _read_manifest(plan_dir)
    for key in drop:
        manifest.pop(key)
    manifest.update(fields)
    manifest["approval"]["digest"] = canonical_digest(manifest)
    _write_manifest(plan_dir, manifest)


# -- positive path ----------------------------------------------------------


def test_should_approve_pending_plan_as_standing_approval(plan, enabled):
    result = _call(_digest(plan))

    assert result["ok"] is True
    after = _read_manifest(plan)
    assert after["approval"]["status"] == "approved"
    assert after["approval"]["decided_by"] == APPROVER
    assert after["approval"]["role"] == "standing"
    assert after["approval"]["self_approved"] is False
    assert after["approval"]["standing_id"] == STANDING_ID
    assert _decisions(plan)[-1]["standing_id"] == STANDING_ID
    assert _audit_records(plan)[-1]["outcome"] == "allowed"


def test_should_make_standing_approval_activatable(plan, enabled):
    _call(_digest(plan))

    assert plan_approval.is_activatable(_read_manifest(plan)) is True


def test_should_record_allowed_audit_with_standing_identity(plan, enabled):
    _call(_digest(plan))

    [record] = _audit_records(plan)
    assert record["outcome"] == "allowed"
    assert record["detail"]["decided_by"] == APPROVER
    assert record["detail"]["role"] == "standing"
    assert record["detail"]["standing_id"] == STANDING_ID


def test_should_record_self_approval_when_approver_ingested_the_plan(plan, enabled):
    _restamp(plan, ingested_by=APPROVER)

    result = _call(_digest(plan))

    assert result["ok"] is True
    assert result["self_approved"] is True
    assert _read_manifest(plan)["approval"]["self_approved"] is True
    [record] = _audit_records(plan)
    assert record["detail"]["self_approved"] is True


def test_should_record_no_self_approval_when_another_person_ingested_the_plan(
    plan, enabled
):
    result = _call(_digest(plan))

    assert result["self_approved"] is False
    assert _read_manifest(plan)["approval"]["self_approved"] is False
    [record] = _audit_records(plan)
    assert record["detail"]["self_approved"] is False


def test_should_record_self_approval_when_the_plan_has_no_ingester(plan, enabled):
    _restamp(plan, drop=("ingested_by",))

    result = _call(_digest(plan))

    assert result["self_approved"] is True
    assert _read_manifest(plan)["approval"]["self_approved"] is True


def test_should_record_the_scheduler_as_the_real_actor(plan, enabled):
    _call(_digest(plan))

    approval = _read_manifest(plan)["approval"]
    assert approval["decided_by"] == APPROVER
    assert approval["recorded_by"] == LOCAL_OPERATOR.subject
    [record] = _audit_records(plan)
    assert record["detail"]["actor"] == LOCAL_OPERATOR.subject


# -- the flag ---------------------------------------------------------------


def test_should_refuse_when_flag_unset(plan, monkeypatch):
    monkeypatch.delenv(STANDING_ENV, raising=False)
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")
    before = _manifest_bytes(plan)

    result = _call(_digest(plan))

    assert result == {"ok": False, "error": "standing approval is disabled"}
    assert _manifest_bytes(plan) == before
    assert _decisions(plan) == []
    assert _audit_records(plan) == []


def test_should_refuse_when_flag_is_true_not_exactly_one(plan, monkeypatch):
    monkeypatch.setenv(STANDING_ENV, "true")
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")
    before = _manifest_bytes(plan)

    result = _call(_digest(plan))

    assert result == {"ok": False, "error": "standing approval is disabled"}
    assert _manifest_bytes(plan) == before
    assert _decisions(plan) == []
    assert _audit_records(plan) == []


# -- the plan-approval gate and the principal -------------------------------


def test_should_refuse_when_plan_approval_gate_is_off(plan, monkeypatch):
    monkeypatch.setenv(STANDING_ENV, "1")
    monkeypatch.delenv(plan_approval.APPROVAL_ENV, raising=False)
    before = _manifest_bytes(plan)

    result = _call(_digest(plan))

    assert result["ok"] is False
    assert result["reason"] == "gate_off"
    _assert_refused(plan, before, "gate_off")


def test_should_refuse_a_human_principal(plan, enabled):
    before = _manifest_bytes(plan)

    with actor.bind_principal(HUMAN):
        result = _call(_digest(plan))

    assert result["reason"] == "not_scheduler"
    _assert_refused(plan, before, "not_scheduler")


def test_should_refuse_a_network_client(plan, enabled):
    before = _manifest_bytes(plan)

    with actor.bind_principal(NETWORK_CLIENT):
        result = _call(_digest(plan))

    assert result["reason"] == "not_scheduler"
    _assert_refused(plan, before, "not_scheduler")


def test_should_refuse_the_local_operator_identity_when_a_transport_bound_it(
    plan, enabled
):
    # The dashboard's shared-secret login binds LOCAL_OPERATOR itself, so the
    # identity alone cannot tell a network caller from the scheduler.
    before = _manifest_bytes(plan)

    with actor.bind_principal(LOCAL_OPERATOR):
        result = _call(_digest(plan))

    assert result["reason"] == "not_scheduler"
    _assert_refused(plan, before, "not_scheduler")


# -- arguments --------------------------------------------------------------


@pytest.mark.parametrize("standing_id", ["", "bad id!", "x" * 81, None, 7])
def test_should_refuse_a_malformed_standing_id(plan, enabled, standing_id):
    before = _manifest_bytes(plan)

    result = _call(_digest(plan), standing_id=standing_id)

    assert result["reason"] == "bad_argument"
    _assert_refused(plan, before, "bad_argument")


@pytest.mark.parametrize("approver", ["", "   ", "a" * 513, None])
def test_should_refuse_a_malformed_approver_subject(plan, enabled, approver):
    before = _manifest_bytes(plan)

    result = _call(_digest(plan), approver_subject=approver)

    assert result["reason"] == "bad_argument"
    _assert_refused(plan, before, "bad_argument")


def test_should_refuse_when_expected_digest_is_missing(plan, enabled):
    before = _manifest_bytes(plan)

    result = _call(None)

    assert result["reason"] == "bad_argument"
    _assert_refused(plan, before, "bad_argument")


# -- plan state -------------------------------------------------------------


def test_should_refuse_a_plan_that_does_not_exist(plan, enabled):
    result = approve_standing(
        "missing",
        standing_id=STANDING_ID,
        approver_subject=APPROVER,
        expected_digest="0" * 64,
        still_valid=lambda: True,
    )

    assert result["ok"] is False
    assert result["reason"] == "no_such_plan"
    assert _decisions(plan) == []
    [record] = _audit_records(plan, "missing")
    assert record["outcome"] == "refused"
    assert record["detail"] == {"reason": "no_such_plan"}


def test_should_refuse_a_plan_already_approved(plan, enabled):
    _set_approval(plan, status="approved")
    before = _manifest_bytes(plan)

    result = _call(_digest(plan))

    assert result["reason"] == "not_pending"
    _assert_refused(plan, before, "not_pending")


def test_should_refuse_a_plan_already_rejected(plan, enabled):
    _set_approval(plan, status="changes_requested", reason="needs tests")
    before = _manifest_bytes(plan)

    result = _call(_digest(plan))

    assert result["reason"] == "not_pending"
    _assert_refused(plan, before, "not_pending")


def test_should_refuse_a_stale_expected_digest(plan, enabled):
    before = _manifest_bytes(plan)

    result = _call("0" * 64)

    assert result["reason"] == "digest_mismatch"
    _assert_refused(plan, before, "digest_mismatch")


def test_should_refuse_when_stored_digest_no_longer_matches_content(plan, enabled):
    manifest = _read_manifest(plan)
    manifest["context"] = "edited after ingest"
    _write_manifest(plan, manifest)
    before = _manifest_bytes(plan)

    result = _call(_digest(plan))

    assert result["reason"] == "digest_mismatch"
    _assert_refused(plan, before, "digest_mismatch")


def test_should_refuse_an_outdated_digest_version(plan, enabled):
    _set_approval(plan, digest_version=plan_approval.APPROVAL_DIGEST_VERSION - 1)
    before = _manifest_bytes(plan)

    result = _call(_digest(plan))

    assert result["reason"] == "outdated_digest_version"
    _assert_refused(plan, before, "outdated_digest_version")


# -- still_valid ------------------------------------------------------------


@pytest.mark.parametrize("answer", [False, 1, "yes", None])
def test_should_refuse_when_still_valid_is_not_exactly_true(plan, enabled, answer):
    before = _manifest_bytes(plan)

    result = _call(_digest(plan), still_valid=lambda: answer)

    assert result["reason"] == "standing_invalid"
    _assert_refused(plan, before, "standing_invalid")


def test_should_refuse_when_still_valid_raises(plan, enabled):
    before = _manifest_bytes(plan)

    def _boom():
        raise RuntimeError("directory unreachable")

    result = _call(_digest(plan), still_valid=_boom)

    assert result["reason"] == "standing_invalid"
    assert "directory unreachable" not in json.dumps(result)
    _assert_refused(plan, before, "standing_invalid")


# -- lock -------------------------------------------------------------------


class _LockedStore:
    """The real store, except that the plan lock is never acquired."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    @contextlib.contextmanager
    def transaction(self, plan_name, wait_seconds=0.0):
        yield False


def test_should_refuse_when_the_plan_lock_is_not_acquired(plan, enabled, monkeypatch):
    monkeypatch.setattr(server, "_store", _LockedStore(server._store))
    before = _manifest_bytes(plan)

    result = _call(_digest(plan))

    assert result["reason"] == "locked"
    assert (plan / f"{PLAN}.manifest.json").read_bytes() == before
    assert _decisions(plan) == []
    [record] = _audit_records(plan)
    assert record["outcome"] == "refused"
    assert record["detail"] == {"reason": "locked"}


def test_should_refuse_when_policy_denies_plan_approve_to_the_scheduler(
    plan, enabled, monkeypatch
):
    from pipeline import authorization

    def _deny_plan_approve(_authorizer, _principal, action, _resource):
        return str(getattr(action, "value", action)) != "plan:approve"

    monkeypatch.setattr(authorization, "is_allowed", _deny_plan_approve)
    before = _manifest_bytes(plan)

    result = _call(_digest(plan))

    assert result == {
        "ok": False,
        "error": "not authorized",
        "reason": "not_authorized",
    }
    assert _manifest_bytes(plan) == before
    assert _decisions(plan) == []
    records = _audit_records(plan)
    assert [r["action"] for r in records] == ["plan:approve"]
    assert records[0]["outcome"] == "denied"


# -- plan guidance ----------------------------------------------------------


def _guide(plan_dir, guidance):
    """Attach a guidance record; guidance is outside the approval digest."""
    manifest = _read_manifest(plan_dir)
    manifest["guidance"] = guidance
    _write_manifest(plan_dir, manifest)


_NEEDED = {
    "status": "needed",
    "questions": ["Which database?"],
    "source": {"kind": "jira", "ref": "PROJ-1"},
    "request_excerpt": "x",
    "raised_at": "2026-11-23T00:00:00+00:00",
}


def test_should_refuse_a_plan_that_needs_guidance(plan, enabled):
    _guide(plan, _NEEDED)
    before = _manifest_bytes(plan)

    result = _call(_digest(plan))

    assert result == {
        "ok": False,
        "error": "plan needs guidance before it can be approved",
        "reason": "guidance_needed",
    }
    _assert_refused(plan, before, "guidance_needed")


def test_should_refuse_a_malformed_guidance_record(plan, enabled):
    _guide(plan, "x")
    before = _manifest_bytes(plan)

    result = _call(_digest(plan))

    assert result["reason"] == "guidance_needed"
    _assert_refused(plan, before, "guidance_needed")


def test_should_approve_a_plan_whose_guidance_is_resolved(plan, enabled):
    _guide(plan, {**_NEEDED, "status": "resolved", "note": "use postgres"})

    result = _call(_digest(plan))

    assert result["ok"] is True
    assert _read_manifest(plan)["approval"]["status"] == "approved"


# -- exposure ---------------------------------------------------------------


def test_should_not_register_standing_approval_as_an_mcp_tool():
    tool_names = set(server.mcp._tool_manager._tools)

    assert not any("standing" in name for name in tool_names)


def test_should_not_expose_standing_approval_on_a_dashboard_route():
    from app import dashboard

    paths = [getattr(route, "path", "") for route in dashboard.app.routes]

    assert not any("standing" in path for path in paths)
