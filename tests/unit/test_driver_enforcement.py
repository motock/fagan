"""ENF-03: the pipeline drivers and the security-audit record must ask the policy.

``review_story`` and ``advance_pipeline`` drive a plan forward (reviews,
dispatches, merges) and ``record_security_audit`` writes the host record the
periodic security-audit reminder trusts -- a forged record silences that
reminder for a repository. None of them asked the policy.

The contract pinned here:

* the local operator -- the identity the unattended scheduler and daemon run as
  -- keeps working exactly as before: the review and advance wrappers still
  delegate, and ``advance_all_plans`` still advances every plan;
* a principal the policy denies gets the refusal dict back and the delegated
  implementation is never reached, so no review, dispatch or merge happens;
* ``advance_pipeline`` runs on every scheduler tick (roughly every 60s per
  plan), so its *allowed* decisions are deliberately not recorded while its
  denials are -- the chain must not grow on every tick;
* ``advance_all_plans`` is not gated itself: it calls ``advance_pipeline`` per
  plan, so a denied caller gets one refusal entry per plan in its ``plans``
  map rather than a bare refusal;
* ``record_security_audit`` asks for host-scoped ``config:write`` *after* its
  ``repo_root``/``sha`` argument validation (a malformed request must not
  append a policy record, and an attacker-controlled path must not reach
  ``git rev-parse``) and *before* it writes any audit state (a refused caller
  must not be able to plant the record the reminder trusts);
* ``PIPELINE_POLICY_ENFORCE=0`` is the operator's kill switch and lets a
  non-operator through every one of them;
* the dashboard review and advance routes answer ``403`` for a refusal instead
  of reporting it as a generic failure.

``PIPELINE_POLICY_ENFORCE`` is read on every call, so ``monkeypatch.setenv`` is
observed at call time. The audit readers below are the same ones the existing
enforcement suites use: plan chains live at ``<PLAN_DIR>/<plan>.audit.jsonl``
and the host chain at ``<PLAN_DIR>/.audit.host.jsonl``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

# pipeline.server is imported explicitly; import order is not required for
# cold-importability (every pipeline module imports cold, see
# tests/unit/test_hub_satellite_cold_imports.py).
import pipeline.server  # noqa: F401
from app import auth
from app import dashboard as dashboard_mod
from app.identity import Principal
from pipeline import paths, security_audit
from pipeline import server as psrv
from pipeline.actor import bind_principal
from pipeline.audit import append_audit

PLAN = "p1"
PLAN_B = "p2"
STORY = "S-1"
ENFORCE_ENV = "PIPELINE_POLICY_ENFORCE"
_HEADER = "X-Pipeline-Api-Key"

#: An authenticated non-operator: what an OIDC bearer token resolves to. The
#: policy allows it nothing on a plan resource, so every driver must refuse.
ALICE = Principal(subject="alice", auth_method="oidc")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _svc() -> psrv.PipelineService:
    return psrv.PipelineService()


def _manifest(plan_name: str) -> dict:
    """A minimal manifest the store can read for *plan_name*."""
    return {
        "repo_root": "/tmp/repo",
        "version": 3,
        "stories": {
            STORY: {
                "summary": "do the thing",
                "status": "running",
                "dispatched_at": "2026-01-01T00:00:00+00:00",
            }
        },
    }


def _write_plan(plan_dir, plan_name: str) -> None:
    (plan_dir / f"{plan_name}.manifest.json").write_text(json.dumps(_manifest(plan_name)))


def _plan_records(plan_dir, plan_name: str) -> list[dict]:
    """Every record in *plan_name*'s audit chain, oldest first."""
    path = plan_dir / f"{plan_name}.audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _host_decisions(plan_dir) -> list[tuple]:
    """``(action, outcome)`` for every record in the host-scoped chain."""
    path = plan_dir / ".audit.host.jsonl"
    if not path.exists():
        return []
    return [
        (record.get("action"), record.get("outcome"))
        for record in (json.loads(line) for line in path.read_text().splitlines() if line)
    ]


def _append_plan_record(plan_name: str) -> None:
    """Append one pre-existing record to *plan_name*'s chain."""
    append_audit(plan_name, action="story:patch", outcome="allowed")


class _Spy:
    """A patched implementation that records its calls and returns a sentinel."""

    SENTINEL: ClassVar[dict] = {"ok": True, "spy": "called"}

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return dict(self.SENTINEL)

    @property
    def count(self) -> int:
        return len(self.calls)


# --------------------------------------------------------------------------- #
# Positive: the local operator (scheduler/daemon identity) is unaffected
# --------------------------------------------------------------------------- #
def test_local_operator_review_story_still_reviews(plan_dir, monkeypatch):
    """No-regression case: nothing bound resolves to the local operator."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_original_review_story", spy)

    result = _svc().review_story(PLAN, STORY)

    assert (result, spy.calls) == (spy.SENTINEL, [(PLAN, STORY)])


def test_local_operator_advance_pipeline_still_advances(plan_dir, monkeypatch):
    """No-regression case: the every-60s advance tick still runs."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_advance_pipeline_locked", spy)

    result = _svc().advance_pipeline(PLAN)

    assert (result, spy.calls) == (spy.SENTINEL, [(PLAN,)])


def test_local_operator_advance_all_plans_still_advances_every_plan(plan_dir, monkeypatch):
    """No-regression case: one tick advances every plan the store lists."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    _write_plan(plan_dir, PLAN_B)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_advance_pipeline_locked", spy)

    result = _svc().advance_all_plans()

    assert (result, spy.count) == (
        {"ok": True, "plans": {PLAN: spy.SENTINEL, PLAN_B: spy.SENTINEL}},
        2,
    )


# --------------------------------------------------------------------------- #
# Negative: a denied principal cannot drive a plan and never reaches the work
# --------------------------------------------------------------------------- #
def test_denied_principal_gets_refusal_from_review_story(plan_dir, monkeypatch):
    """No review runs for a denied caller: the delegated review is never called."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_original_review_story", spy)

    with bind_principal(ALICE):
        result = _svc().review_story(PLAN, STORY)

    assert (result.get("ok"), result.get("forbidden"), result.get("action")) == (
        False,
        True,
        "plan:dispatch",
    )
    assert spy.count == 0


def test_denied_principal_gets_refusal_from_advance_pipeline(plan_dir, monkeypatch):
    """No tick runs for a denied caller: the delegated advance is never called."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_advance_pipeline_locked", spy)

    with bind_principal(ALICE):
        result = _svc().advance_pipeline(PLAN)

    assert (result.get("ok"), result.get("forbidden"), result.get("action")) == (
        False,
        True,
        "plan:dispatch",
    )
    assert spy.count == 0


def test_invalid_plan_key_is_rejected_before_the_policy_is_asked(plan_dir, monkeypatch):
    """Validation stays first: a malformed request appends no policy record."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_advance_pipeline_locked", spy)
    before = len(_plan_records(plan_dir, PLAN))

    with bind_principal(ALICE), pytest.raises(ValueError):
        _svc().advance_pipeline("../etc/passwd")

    assert (spy.count, len(_plan_records(plan_dir, PLAN))) == (0, before)


def test_denied_principal_advance_all_plans_refuses_each_plan(plan_dir, monkeypatch):
    """advance_all_plans is not gated itself: every plan carries its own refusal."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    _write_plan(plan_dir, PLAN_B)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_advance_pipeline_locked", spy)
    chains_before = {p: len(_plan_records(plan_dir, p)) for p in (PLAN, PLAN_B)}

    with bind_principal(ALICE):
        result = _svc().advance_all_plans()

    assert result.get("ok") is True
    plans = result.get("plans") or {}
    assert sorted(plans) == [PLAN, PLAN_B]
    for plan_name in (PLAN, PLAN_B):
        assert (plans[plan_name].get("forbidden"), plans[plan_name].get("action")) == (
            True,
            "plan:dispatch",
        )
        records = _plan_records(plan_dir, plan_name)
        assert [
            (record.get("action"), record.get("outcome"))
            for record in records[chains_before[plan_name] :]
        ] == [("plan:dispatch", "denied")]
    assert spy.count == 0


def test_operator_advance_all_plans_after_a_denial_still_advances(plan_dir, monkeypatch):
    """A denial is not sticky: the same process advances once the operator asks."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    _write_plan(plan_dir, PLAN_B)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_advance_pipeline_locked", spy)

    with bind_principal(ALICE):
        refused = _svc().advance_all_plans()
    after_denials = {p: len(_plan_records(plan_dir, p)) for p in (PLAN, PLAN_B)}
    allowed = _svc().advance_all_plans()

    assert sorted(refused.get("plans") or {}) == [PLAN, PLAN_B]
    assert (allowed.get("ok"), spy.count) == (True, 2)
    assert {p: len(_plan_records(plan_dir, p)) for p in (PLAN, PLAN_B)} == after_denials


def test_advance_tick_records_denials_only(plan_dir, monkeypatch):
    """2 -> 2 -> 3 -> 3 -> 4: every tick would otherwise bury the chain in noise."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_advance_pipeline_locked", spy)
    for _ in range(2):
        _append_plan_record(PLAN)

    assert len(_plan_records(plan_dir, PLAN)) == 2
    _svc().advance_pipeline(PLAN)  # A: allowed, not recorded
    assert (spy.count, len(_plan_records(plan_dir, PLAN))) == (1, 2)

    with bind_principal(ALICE):
        _svc().advance_pipeline(PLAN)  # B: denied, recorded
    assert (spy.count, len(_plan_records(plan_dir, PLAN))) == (1, 3)

    _svc().advance_pipeline(PLAN)  # C: allowed again, still not recorded
    assert (spy.count, len(_plan_records(plan_dir, PLAN))) == (2, 3)

    with bind_principal(ALICE):
        _svc().advance_pipeline(PLAN)  # D: denied, recorded
    records = _plan_records(plan_dir, PLAN)
    assert (spy.count, len(records)) == (2, 4)
    assert (records[-1].get("action"), records[-1].get("outcome")) == (
        "plan:dispatch",
        "denied",
    )


def test_kill_switch_lets_a_denied_principal_drive_the_pipeline(plan_dir, monkeypatch):
    """PIPELINE_POLICY_ENFORCE=0 is the operator's escape hatch for every driver."""
    monkeypatch.setenv(ENFORCE_ENV, "0")
    _write_plan(plan_dir, PLAN)
    review = _Spy()
    advance = _Spy()
    monkeypatch.setattr(psrv, "_original_review_story", review)
    monkeypatch.setattr(psrv, "_advance_pipeline_locked", advance)

    with bind_principal(ALICE):
        reviewed = _svc().review_story(PLAN, STORY)
        advanced = _svc().advance_pipeline(PLAN)

    assert (reviewed, advanced, review.count, advance.count) == (
        review.SENTINEL,
        advance.SENTINEL,
        1,
        1,
    )


# --------------------------------------------------------------------------- #
# record_security_audit: the record the audit reminder trusts is host-scoped
# --------------------------------------------------------------------------- #
def _git(repo, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def audit_state_dir(plan_dir, monkeypatch):
    """Point every PLAN_DIR binding at a tmp dir (the git repo stays real).

    ``plan_dir`` already redirects the server/persistence/concurrency
    bindings, which is where the host-scoped audit chain is written;
    ``pipeline.paths`` is the binding ``record_security_audit`` reads its state
    directory from.
    """
    monkeypatch.setattr(paths, "PLAN_DIR", plan_dir)
    return plan_dir


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "f.txt").write_text("0")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "c0")
    return root


def _state_path(audit_state_dir, repo) -> Path:
    return security_audit.state_path(audit_state_dir, str(repo))


def test_local_operator_records_the_audit_and_one_allowed_host_record(
    audit_state_dir, repo, monkeypatch
):
    """No-regression case: the audit still lands, and the decision is recorded."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    result = psrv.record_security_audit(str(repo))

    assert result["ok"] is True
    assert _host_decisions(audit_state_dir) == [("config:write", "allowed")]
    state = security_audit.read_audit_state(audit_state_dir, str(repo))
    assert state is not None
    assert state["last_audited_sha"] == result["last_audited_sha"]


def test_denied_principal_cannot_record_an_audit(audit_state_dir, repo, monkeypatch):
    """A forged record silences the reminder, so a refusal must plant nothing."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with bind_principal(ALICE):
        result = psrv.record_security_audit(str(repo))

    assert (result.get("ok"), result.get("forbidden"), result.get("action")) == (
        False,
        True,
        "config:write",
    )
    assert _host_decisions(audit_state_dir) == [("config:write", "denied")]
    assert not _state_path(audit_state_dir, repo).exists()
    assert security_audit.read_audit_state(audit_state_dir, str(repo)) is None


def test_malformed_arguments_are_rejected_before_the_policy_is_asked(
    audit_state_dir, repo, monkeypatch
):
    """Validation stays first: a malformed request appends nothing at all."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with bind_principal(ALICE):
        bad_path = psrv.record_security_audit("relative/path")
        bad_sha = psrv.record_security_audit(str(repo), "not-a-hex-sha")

    assert (bad_path.get("ok"), bad_sha.get("ok")) == (False, False)
    assert _host_decisions(audit_state_dir) == []
    assert not _state_path(audit_state_dir, repo).exists()


def test_refusal_after_a_successful_audit_leaves_the_record_intact(
    audit_state_dir, repo, monkeypatch
):
    """0 -> 1 -> 2 -> 3: a later refusal neither writes nor rewrites the state."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    assert _host_decisions(audit_state_dir) == []

    with bind_principal(ALICE):
        refused_before = psrv.record_security_audit(str(repo), "not-a-hex-sha")
    assert refused_before.get("ok") is False
    assert _host_decisions(audit_state_dir) == []
    assert not _state_path(audit_state_dir, repo).exists()

    with bind_principal(ALICE):
        refused = psrv.record_security_audit(str(repo))
    assert refused.get("forbidden") is True
    assert _host_decisions(audit_state_dir) == [("config:write", "denied")]

    recorded = psrv.record_security_audit(str(repo))
    assert recorded.get("ok") is True
    assert _host_decisions(audit_state_dir)[-1] == ("config:write", "allowed")
    state_before = _state_path(audit_state_dir, repo).read_bytes()

    with bind_principal(ALICE):
        refused_after = psrv.record_security_audit(str(repo), "0" * 40)

    assert refused_after.get("forbidden") is True
    assert _host_decisions(audit_state_dir)[-1] == ("config:write", "denied")
    assert len(_host_decisions(audit_state_dir)) == 3
    assert _state_path(audit_state_dir, repo).read_bytes() == state_before


def test_kill_switch_lets_a_denied_principal_record_the_audit(
    audit_state_dir, repo, monkeypatch
):
    """PIPELINE_POLICY_ENFORCE=0 lets the write through and appends nothing."""
    monkeypatch.setenv(ENFORCE_ENV, "0")

    with bind_principal(ALICE):
        result = psrv.record_security_audit(str(repo))

    assert result["ok"] is True
    assert _host_decisions(audit_state_dir) == []
    assert security_audit.read_audit_state(audit_state_dir, str(repo)) is not None


# --------------------------------------------------------------------------- #
# Dashboard: a refusal must answer 403, not a generic failure
# --------------------------------------------------------------------------- #
class _FixedPrincipalProvider:
    """An identity provider that resolves every accepted credential to one
    fixed principal.

    The shared-secret gate has already accepted the credential; this stands in
    for a deployment whose verified caller is not the local operator.
    """

    def __init__(self, principal: Principal) -> None:
        self._principal = principal

    def authenticate(self, credential):
        return self._principal


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so no real key is read."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def denied_dashboard_principal(monkeypatch):
    """Make every authenticated dashboard request run as a denied principal."""
    provider = _FixedPrincipalProvider(ALICE)
    monkeypatch.setattr(dashboard_mod, "_identity", provider)
    return provider


def _client() -> TestClient:
    return TestClient(dashboard_mod.app, headers={_HEADER: auth.get_or_create_api_key()})


def test_review_route_answers_403_for_a_refused_caller(
    key_file, plan_dir, denied_dashboard_principal, monkeypatch
):
    """A refusal is a 403, not the route's generic "unknown error" branch."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_original_review_story", spy)

    response = _client().post(f"/api/plans/{PLAN}/stories/{STORY}/review")

    assert response.status_code == 403
    assert response.json()["detail"] == "not authorized: plan:dispatch"
    assert spy.count == 0


def test_review_route_still_answers_200_for_the_local_operator(
    key_file, plan_dir, monkeypatch
):
    """No-regression case: the local operator's review still goes through."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_original_review_story", spy)

    response = _client().post(f"/api/plans/{PLAN}/stories/{STORY}/review")

    assert (response.status_code, spy.count) == (200, 1)


def test_advance_route_answers_403_for_a_refused_caller(
    key_file, plan_dir, denied_dashboard_principal, monkeypatch
):
    """A refusal is a 403, not a 400 carrying the generic failure text."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_advance_pipeline_locked", spy)

    response = _client().post(f"/api/plans/{PLAN}/advance")

    assert response.status_code == 403
    assert response.json()["detail"] == "not authorized: plan:dispatch"
    assert spy.count == 0


def test_advance_route_still_answers_200_for_the_local_operator(
    key_file, plan_dir, monkeypatch
):
    """No-regression case: the scheduler's own advance route still works."""
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir, PLAN)
    spy = _Spy()
    monkeypatch.setattr(psrv, "_advance_pipeline_locked", spy)

    response = _client().post(f"/api/plans/{PLAN}/advance")

    assert (response.status_code, spy.count) == (200, 1)
