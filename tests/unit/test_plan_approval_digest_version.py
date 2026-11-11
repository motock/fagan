"""Every approval record carries the digest version it was made under.

A change to *what the digest covers* must never leave an old approval
silently in force: an approval binds to a digest, and the digest's meaning
changes with the version that produced it.  So every approval record carries
a ``digest_version`` and only a record written under the *current* version is
honoured.

Contract graded here (``pipeline/plan_approval.py`` unless named):

* ``APPROVAL_DIGEST_VERSION`` is the version this build writes; a record
  written before the field existed reads as the legacy version 1, so while
  the current version is still 1 no live plan changes state.
* ``digest_version_is_current(approval)`` is True only for the current
  version.  Anything else fails closed: a bool, a string, a float, ``None``,
  a lower version and a *higher* version (a rolled-back build must not honour
  a record written by a newer one).
* ``on_ingest``, ``reset_if_changed`` and ``_decide_plan`` write
  ``digest_version`` on every record they write, the decisions-log entry
  included.
* ``on_ingest`` keeps ``approved`` only when the digest matches *and* the
  previous record's version is current; ``reset_if_changed`` resets when the
  digest differs *or* the version is not current.
* ``is_activatable`` requires ``approved`` *and* a current version.
* ``outdated_reason(manifest)`` reports ``approval_digest_outdated`` for the
  advance/dispatch refusals, so the operator learns *why* the plan stopped.
* ``_decide_plan`` refuses a decision on an out-of-version record instead of
  upgrading it: approving in the same call would approve content the caller
  may never have been shown.  The refusal writes nothing and records one
  refused audit event.
* the dashboard maps that refusal to 409 with the same literal, never to the
  404 it would otherwise fall through to.

``docs/specs/PLAN_APPROVAL.md`` pins the refusal literal and the
``approval_digest_outdated`` literal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline.advance as advance_module
import pipeline.config as config_module
import pipeline.dispatch as pdispatch
from pipeline import persistence as ppers
from pipeline import plan_approval as pa
from pipeline import server as p

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "specs" / "PLAN_APPROVAL.md"

PLAN = "dv1"
STORY = "S-1"

#: The literal ``_decide_plan`` refuses with when the record's digest version
#: is not the current one.  Pinned verbatim: the dashboard shows it and the
#: doc repeats it.
OUTDATED_ERROR = (
    "plan approval was recorded under an older digest version; run "
    "`python -m pipeline.tools.reset_outdated_approvals`, review the plan, "
    "then decide"
)

#: The reason ``outdated_reason`` reports and advance/dispatch spread into
#: their refusal dicts.
OUTDATED_REASON = "approval_digest_outdated"

#: Values that are not the current digest version, whatever it is.
NOT_A_VERSION = [True, "1", 1.0, None, 0, -1]


# ---------------------------------------------------------------------------
# Helpers


def _manifest(**overrides):
    story = {
        "key": STORY,
        "title": f"story {STORY}",
        "summary": "s",
        "status": "todo",
        "risk": "low",
        "correlation_id": "c0ffee000001",
        "dispatch_attempts": 0,
    }
    manifest = {"name": PLAN, "paused": False, "stories": {STORY: story}}
    manifest.update(overrides)
    return manifest


#: Distinguishes "no digest_version field" from "digest_version is None".
_ABSENT = object()


def _approved(manifest, digest_version=_ABSENT):
    """An approval record made against exactly this manifest's content."""
    record = {"status": "approved", "digest": pa.canonical_digest(manifest)}
    if digest_version is not _ABSENT:
        record["digest_version"] = digest_version
    return record


def _pending(manifest, digest_version=_ABSENT):
    """A pending record made against exactly this manifest's content."""
    record = {"status": "pending_approval", "digest": pa.canonical_digest(manifest)}
    if digest_version is not _ABSENT:
        record["digest_version"] = digest_version
    return record


@pytest.fixture(autouse=True)
def _gate_off_by_default(monkeypatch):
    """Start with the gate unset; each test opts in explicitly."""
    monkeypatch.delenv(pa.APPROVAL_ENV, raising=False)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")


@pytest.fixture
def newer_version(monkeypatch):
    """Pretend the current digest version is 2 (a newer build than today).

    The tests below must not depend on today's value of
    ``APPROVAL_DIGEST_VERSION``: they stub it, so they keep their meaning
    when the version is bumped.
    """
    monkeypatch.setattr(pa, "APPROVAL_DIGEST_VERSION", 2, raising=False)


# ---------------------------------------------------------------------------
# The constants


def test_should_expose_a_positive_integer_digest_version():
    assert type(pa.APPROVAL_DIGEST_VERSION) is int
    assert pa.APPROVAL_DIGEST_VERSION >= 1


def test_should_treat_a_record_without_the_field_as_current_at_version_one(
    monkeypatch,
):
    monkeypatch.setattr(pa, "APPROVAL_DIGEST_VERSION", 1, raising=False)

    assert pa.digest_version_is_current({"status": "approved"}) is True


def test_should_keep_an_approved_record_without_the_field_activatable_at_version_one(
    monkeypatch,
):
    """A record written before the field existed stays valid: no live plan
    changes state when this ships."""
    monkeypatch.setattr(pa, "APPROVAL_DIGEST_VERSION", 1, raising=False)
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    manifest = _manifest(approval={"status": "approved"})

    assert pa.is_activatable(manifest) is True


def test_should_not_treat_a_record_without_the_field_as_current_at_version_two(
    newer_version,
):
    assert pa.digest_version_is_current({"status": "approved"}) is False


def test_should_treat_the_current_version_as_current():
    record = {"status": "approved", "digest_version": pa.APPROVAL_DIGEST_VERSION}

    assert pa.digest_version_is_current(record) is True


@pytest.mark.parametrize("value", NOT_A_VERSION)
def test_should_not_treat_a_malformed_version_as_current(value):
    record = {"status": "approved", "digest_version": value}

    assert pa.digest_version_is_current(record) is False


def test_should_not_treat_a_lower_version_as_current(newer_version):
    record = {"status": "approved", "digest_version": 1}

    assert pa.digest_version_is_current(record) is False


def test_should_not_treat_a_higher_version_as_current(newer_version):
    """A rolled-back build must not honour a record written by a newer one."""
    record = {"status": "approved", "digest_version": 3}

    assert pa.digest_version_is_current(record) is False


# ---------------------------------------------------------------------------
# on_ingest / reset_if_changed / _decide_plan write the version


def test_should_write_the_current_version_on_ingest(gate_on):
    record = pa.on_ingest(_manifest())

    assert record["digest_version"] == pa.APPROVAL_DIGEST_VERSION


def test_should_write_the_current_version_on_reset(gate_on):
    manifest = _manifest(approval={"status": "approved", "digest": "stale"})

    assert pa.reset_if_changed(manifest) is True
    assert manifest["approval"]["digest_version"] == pa.APPROVAL_DIGEST_VERSION


def test_should_keep_approved_on_ingest_when_the_version_is_current(gate_on):
    manifest = _manifest()
    manifest["approval"] = _approved(
        manifest, digest_version=pa.APPROVAL_DIGEST_VERSION
    )

    record = pa.on_ingest(manifest)

    assert record["status"] == "approved"
    assert record["digest_version"] == pa.APPROVAL_DIGEST_VERSION


def test_should_not_keep_approved_on_ingest_when_the_version_is_not_current(gate_on):
    """A matching digest is not enough: the version must match too."""
    manifest = _manifest()
    manifest["approval"] = _approved(manifest, digest_version=99)

    record = pa.on_ingest(manifest)

    assert record["status"] == "pending_approval"
    assert record["digest_version"] == pa.APPROVAL_DIGEST_VERSION


def test_should_reset_when_the_digest_matches_but_the_version_is_not_current(
    gate_on, newer_version
):
    """``reset_if_changed`` resets on a version change as well as a digest one."""
    manifest = _manifest()
    manifest["approval"] = _approved(manifest, digest_version=1)

    assert pa.reset_if_changed(manifest) is True
    assert manifest["approval"]["status"] == "pending_approval"
    assert manifest["approval"]["digest_version"] == pa.APPROVAL_DIGEST_VERSION


def test_should_not_reset_when_the_digest_and_the_version_both_match(gate_on):
    manifest = _manifest()
    manifest["approval"] = _approved(
        manifest, digest_version=pa.APPROVAL_DIGEST_VERSION
    )

    assert pa.reset_if_changed(manifest) is False
    assert manifest["approval"]["status"] == "approved"


# ---------------------------------------------------------------------------
# is_activatable


def test_should_be_activatable_when_the_gate_is_off_even_with_an_old_version(
    monkeypatch,
):
    monkeypatch.setenv(pa.APPROVAL_ENV, "off")
    manifest = _manifest(approval={"status": "approved", "digest_version": 99})

    assert pa.is_activatable(manifest) is True


def test_should_be_activatable_for_an_approved_record_at_the_current_version(
    gate_on,
):
    manifest = _manifest()
    manifest["approval"] = _approved(
        manifest, digest_version=pa.APPROVAL_DIGEST_VERSION
    )

    assert pa.is_activatable(manifest) is True


@pytest.mark.parametrize("value", NOT_A_VERSION)
def test_should_not_be_activatable_for_a_malformed_version(gate_on, value):
    manifest = _manifest()
    manifest["approval"] = _approved(manifest, digest_version=value)

    assert pa.is_activatable(manifest) is False


def test_should_not_be_activatable_for_a_record_without_the_field_at_version_two(
    gate_on, newer_version
):
    manifest = _manifest()
    manifest["approval"] = _approved(manifest)

    assert pa.is_activatable(manifest) is False


def test_should_not_be_activatable_for_a_higher_version(gate_on, newer_version):
    manifest = _manifest()
    manifest["approval"] = _approved(manifest, digest_version=3)

    assert pa.is_activatable(manifest) is False


def test_should_not_be_activatable_for_a_pending_record(gate_on):
    manifest = _manifest()
    manifest["approval"] = _pending(
        manifest, digest_version=pa.APPROVAL_DIGEST_VERSION
    )

    assert pa.is_activatable(manifest) is False


# ---------------------------------------------------------------------------
# outdated_reason


def test_should_report_no_reason_for_a_current_record(gate_on):
    manifest = _manifest()
    manifest["approval"] = _approved(
        manifest, digest_version=pa.APPROVAL_DIGEST_VERSION
    )

    assert pa.outdated_reason(manifest) == {}


def test_should_report_no_reason_when_the_gate_is_off(monkeypatch):
    monkeypatch.setenv(pa.APPROVAL_ENV, "off")
    manifest = _manifest(approval={"status": "approved", "digest_version": 99})

    assert pa.outdated_reason(manifest) == {}


def test_should_report_no_reason_for_a_non_dict_approval(gate_on):
    manifest = _manifest(approval="approved")

    assert pa.outdated_reason(manifest) == {}


def test_should_report_no_reason_for_a_plan_without_an_approval(gate_on):
    assert pa.outdated_reason(_manifest()) == {}


def test_should_report_the_outdated_reason_for_an_old_version(gate_on):
    manifest = _manifest(approval={"status": "approved", "digest_version": 99})

    assert pa.outdated_reason(manifest) == {"reason": OUTDATED_REASON}


def test_should_report_the_outdated_reason_for_a_missing_version(
    gate_on, newer_version
):
    manifest = _manifest(approval={"status": "approved"})

    assert pa.outdated_reason(manifest) == {"reason": OUTDATED_REASON}


# ---------------------------------------------------------------------------
# advance: the refusal names the reason


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    return d


class _FakeBackend:
    def get_backend(self, *_args, **_kwargs):
        return self

    def resource_status(self, **_kwargs):
        return {"ok": True}


class _FakeAutonomy:
    def __init__(self, value="gated"):
        self._value_ = value

    def _value(self):
        return self._value_


class _FakeStore:
    def __init__(self, path):
        self.path = path

    def manifest_path(self, plan_name):
        return self.path

    def get_manifest(self, *args):
        return json.loads(self.path.read_text())

    def put_manifest(self, namespace, plan_name, manifest):
        self.path.write_text(json.dumps(manifest))

    def records(self):
        return self

    def get_manifests(self, namespace):
        return {}


@pytest.fixture(autouse=True)
def _pin_tick_environment(monkeypatch):
    for name, value in (
        ("MAX_CONCURRENT_AGENTS", 8),
        ("DISPATCH_MAX_ATTEMPTS", 3),
        ("SESSION_PAUSE_THRESHOLD", 101),
        ("SESSION_RESUME_THRESHOLD", 101),
        ("WEEK_PAUSE_THRESHOLD", 101),
        ("WEEK_RESUME_THRESHOLD", 101),
    ):
        monkeypatch.setattr(config_module, name, value, raising=False)
        if hasattr(advance_module, name):
            monkeypatch.setattr(advance_module, name, value, raising=False)


@pytest.fixture
def tick(monkeypatch, tmp_path):
    """Wire the tick's seams; returns (dispatched, install(manifest))."""
    dispatched = []

    def _recorder(plan_name, key):
        dispatched.append((plan_name, key))
        return {"ok": True}

    monkeypatch.setattr(advance_module, "backend", _FakeBackend())
    monkeypatch.setattr(advance_module, "_role_resource_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(
        advance_module, "_count_on_device_in_progress_agents", lambda: 0, raising=True
    )
    monkeypatch.setattr(
        advance_module, "PIPELINE_AUTONOMY", _FakeAutonomy("gated"), raising=True
    )
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)
    monkeypatch.setattr(advance_module, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(advance_module, "interrupt_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "check_story_status", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "review_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "dispatch_story", _recorder)

    def _install(manifest):
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest))
        monkeypatch.setattr(advance_module, "_store", _FakeStore(path))
        return path

    return dispatched, _install


def _run_tick():
    return advance_module._advance_pipeline_locked_impl(PLAN)


def test_should_skip_the_tick_with_the_outdated_reason(tick, gate_on):
    dispatched, install = tick
    manifest = _manifest()
    manifest["approval"] = _approved(manifest, digest_version=99)
    install(manifest)

    result = _run_tick()

    assert result.get("skipped") == "plan_not_approved"
    assert result.get("reason") == OUTDATED_REASON
    assert dispatched == []


def test_should_skip_the_tick_without_a_reason_for_a_current_version(tick, gate_on):
    """A plan with a current-version record gets exactly today's dict."""
    dispatched, install = tick
    manifest = _manifest()
    manifest["approval"] = _pending(
        manifest, digest_version=pa.APPROVAL_DIGEST_VERSION
    )
    install(manifest)

    result = _run_tick()

    assert result == {"ok": True, "skipped": "plan_not_approved"}
    assert dispatched == []


# ---------------------------------------------------------------------------
# dispatch_story: the refusal names the reason and creates no worktree


@pytest.fixture
def worktree_root(tmp_path, monkeypatch):
    d = tmp_path / "worktrees"
    d.mkdir()
    monkeypatch.setattr(p, "WORKTREE_ROOT", d)
    return d


@pytest.fixture
def launcher(monkeypatch):
    """Stub the first launch step; records calls and stops before git."""
    calls = []

    def _stub(plan_name, branch, worktree_path, **kwargs):
        calls.append((plan_name, branch, str(worktree_path)))
        return {"ok": False, "error": "stub: stop before git"}

    monkeypatch.setattr(pdispatch, "_create_fresh_worktree", _stub)
    monkeypatch.setattr(
        pdispatch, "_refuse_if_remote_branch_taken", lambda *a, **k: None
    )
    return calls


def _write(plan_dir, manifest):
    path = plan_dir / f"{PLAN}.manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


def test_should_refuse_dispatch_with_the_outdated_reason(
    plan_dir, worktree_root, launcher, gate_on
):
    manifest = _manifest()
    manifest["approval"] = _approved(manifest, digest_version=99)
    path = _write(plan_dir, manifest)
    before = path.read_bytes()

    result = p.dispatch_story(PLAN, STORY)

    assert result["error"] == "plan is not approved"
    assert result["reason"] == OUTDATED_REASON
    assert launcher == []
    assert list(worktree_root.iterdir()) == []
    assert path.read_bytes() == before


def test_should_refuse_dispatch_without_a_reason_for_a_current_version(
    plan_dir, worktree_root, launcher, gate_on
):
    """A plan with a current-version record gets exactly today's dict."""
    manifest = _manifest()
    manifest["approval"] = _pending(
        manifest, digest_version=pa.APPROVAL_DIGEST_VERSION
    )
    _write(plan_dir, manifest)

    result = p.dispatch_story(PLAN, STORY)

    assert result == {"ok": False, "error": "plan is not approved", "plan": PLAN}


# ---------------------------------------------------------------------------
# _decide_plan: the refusal


@pytest.fixture
def decision_plan(tmp_path, monkeypatch):
    """A plan with a pending approval record and PLAN_DIR pointed at it."""
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(ppers, "PLAN_DIR", tmp_path)
    manifest = _manifest()
    manifest["approval"] = {
        "status": "pending_approval",
        "digest": pa.canonical_digest(manifest),
        "digest_version": pa.APPROVAL_DIGEST_VERSION,
        "warnings_seen": ["w1"],
    }
    (tmp_path / f"{PLAN}.manifest.json").write_text(json.dumps(manifest))
    return tmp_path


@pytest.fixture
def decide_enabled(monkeypatch):
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    monkeypatch.setenv("PIPELINE_POLICY_ENFORCE", "0")


@pytest.fixture
def notices(monkeypatch):
    """Record every delivery ``plan_approval_notice`` makes."""
    recorded = []

    def _stub(plan_name, message, **kwargs):
        recorded.append({"plan_name": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(p, "_notify_user", _stub)
    return recorded


def _set_version(plan_dir, version):
    """Rewrite the stored approval record's digest version (absent -> legacy)."""
    path = plan_dir / f"{PLAN}.manifest.json"
    manifest = json.loads(path.read_text())
    if version is None:
        manifest["approval"].pop("digest_version", None)
    else:
        manifest["approval"]["digest_version"] = version
    path.write_text(json.dumps(manifest))


def _stored_manifest(plan_dir):
    return json.loads((plan_dir / f"{PLAN}.manifest.json").read_text())


def _manifest_bytes(plan_dir):
    return (plan_dir / f"{PLAN}.manifest.json").read_bytes()


def _decisions(plan_dir):
    path = plan_dir / f"{PLAN}.decisions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _audit_records(plan_dir):
    path = plan_dir / f"{PLAN}.audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _approve(plan_name, **kwargs):
    from pipeline.service import PipelineService

    return PipelineService().approve_plan(plan_name, **kwargs)


def _reject(plan_name, reason="", **kwargs):
    from pipeline.service import PipelineService

    return PipelineService().reject_plan(plan_name, reason, **kwargs)


def test_should_refuse_to_approve_an_outdated_record(
    decision_plan, decide_enabled, notices
):
    _set_version(decision_plan, 99)
    before = _manifest_bytes(decision_plan)

    result = _approve(PLAN)

    assert result["ok"] is False
    assert result["error"] == OUTDATED_ERROR
    assert result.get("outdated_digest_version") is True
    assert _manifest_bytes(decision_plan) == before
    assert _decisions(decision_plan) == []
    assert notices == []


def test_should_refuse_to_reject_an_outdated_record(
    decision_plan, decide_enabled, notices
):
    _set_version(decision_plan, 99)
    before = _manifest_bytes(decision_plan)

    result = _reject(PLAN, "no")

    assert result["ok"] is False
    assert result["error"] == OUTDATED_ERROR
    assert result.get("outdated_digest_version") is True
    assert _manifest_bytes(decision_plan) == before
    assert _decisions(decision_plan) == []
    assert notices == []


def test_should_record_one_refused_audit_event_for_an_outdated_record(
    decision_plan, decide_enabled
):
    _set_version(decision_plan, 99)

    _approve(PLAN)

    records = _audit_records(decision_plan)

    assert len(records) == 1
    assert records[0]["action"] == "plan.approved"
    assert records[0]["outcome"] == "refused"
    assert records[0]["detail"]["reason"] == "outdated_digest_version"


def test_should_not_upgrade_the_record_it_refuses(decision_plan, decide_enabled):
    """Approving in the same call would approve content the caller never saw."""
    _set_version(decision_plan, 99)

    _approve(PLAN)

    stored = _stored_manifest(decision_plan)["approval"]

    assert stored["status"] == "pending_approval"
    assert stored["digest_version"] == 99


def test_should_refuse_an_outdated_record_before_the_digest_check(
    decision_plan, decide_enabled
):
    """The version check comes before the stored-digest check."""
    path = decision_plan / f"{PLAN}.manifest.json"
    manifest = json.loads(path.read_text())
    manifest["approval"]["digest"] = "a digest that does not match"
    manifest["approval"]["digest_version"] = 99
    path.write_text(json.dumps(manifest))

    result = _approve(PLAN)

    assert result["ok"] is False
    assert result["error"] == OUTDATED_ERROR
    assert result.get("outdated_digest_version") is True


def test_should_refuse_a_stale_expected_digest_before_the_version_check(
    decision_plan, decide_enabled
):
    """The stale-expected-digest check comes first."""
    _set_version(decision_plan, 99)

    result = _approve(PLAN, expected_digest="0" * 64)

    assert result["ok"] is False
    assert result.get("stale_digest") is True
    assert result.get("outdated_digest_version") is not True


def test_should_approve_and_record_the_current_version(decision_plan, decide_enabled):
    result = _approve(PLAN)

    assert result["ok"] is True
    assert result["digest_version"] == pa.APPROVAL_DIGEST_VERSION
    stored = _stored_manifest(decision_plan)["approval"]
    assert stored["digest_version"] == pa.APPROVAL_DIGEST_VERSION
    assert _decisions(decision_plan)[0]["digest_version"] == pa.APPROVAL_DIGEST_VERSION


# ---------------------------------------------------------------------------
# The dashboard maps the refusal to 409


@pytest.fixture
def dashboard_plan(tmp_path, monkeypatch):
    """A plan alice owns, with an approval record under an old digest version."""
    from app import auth, oidc_login
    from app import dashboard as d
    from app.identity import IdentityError, Principal
    from app.oidc import OidcProvider
    from pipeline import authorization

    class _StubOidcProvider(OidcProvider):
        def __init__(self) -> None:
            pass

        def authenticate(self, credential):
            if credential == "tok-alice":
                return Principal(subject="alice", auth_method="oidc")
            raise IdentityError("invalid token")

    monkeypatch.setattr(auth, "API_KEY_PATH", tmp_path / ".dashboard_api_key")
    monkeypatch.setattr(d, "_identity", _StubOidcProvider())
    monkeypatch.setattr(
        d,
        "_oidc_login_config",
        oidc_login.OidcLoginConfig(
            enabled=True,
            client_id="pipeline-dashboard",
            redirect_uri="https://dashboard.example/auth/callback",
            issuer="https://issuer.example",
        ),
    )
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(ppers, "PLAN_DIR", tmp_path)
    monkeypatch.setenv(pa.APPROVAL_ENV, "on")
    monkeypatch.setenv(authorization.ENFORCE_ENV, "on")
    oidc_login.reset_state()

    manifest = _manifest()
    manifest.update(
        {
            "repo_root": "/repo",
            "context": {},
            "role_config": {"reviewer": "alice"},
            "owner": "alice",
            "delegates": ["bob"],
            "ingested_by": "alice",
        }
    )
    manifest["approval"] = {
        "status": "pending_approval",
        "digest": pa.canonical_digest(manifest),
        "warnings_seen": [],
        "digest_version": 99,
    }
    (tmp_path / f"{PLAN}.manifest.json").write_text(json.dumps(manifest))

    yield tmp_path, pa.canonical_digest(manifest)

    oidc_login.reset_state()


def test_should_answer_409_with_the_outdated_literal(dashboard_plan):
    """The dashboard must not fall through to the 404 'plan not found'."""
    from fastapi.testclient import TestClient

    from app import dashboard as d

    plan_dir, digest = dashboard_plan
    client = TestClient(d.app)

    response = client.post(
        f"/api/plans/{PLAN}/review/approve",
        json={"digest": digest},
        headers={"Authorization": "Bearer tok-alice"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == OUTDATED_ERROR
    assert _stored_manifest(plan_dir)["approval"]["status"] == "pending_approval"
    assert _decisions(plan_dir) == []


# ---------------------------------------------------------------------------
# The docs pin the literals and the section


def test_should_document_the_digest_version_and_the_reason():
    doc = DOC_PATH.read_text()

    assert OUTDATED_ERROR in doc
    assert OUTDATED_REASON in doc


def test_should_document_a_digest_version_section():
    headings = [
        line for line in DOC_PATH.read_text().splitlines() if line.startswith("#")
    ]

    assert any(
        line.lstrip("#").strip().startswith("3.1") and "digest version" in line.lower()
        for line in headings
    )


def test_should_document_a_409_row_for_the_outdated_refusal():
    doc = DOC_PATH.read_text()
    section = doc.split("## 9. Dashboard surfacing", 1)[-1]
    rows = [line for line in section.splitlines() if line.strip().startswith("| `409`")]

    assert rows, "section 9 must document the 409 statuses"
    assert any(
        "digest version" in row.lower() or OUTDATED_REASON in row for row in rows
    )


# ---------------------------------------------------------------------------
# The default-off path stays byte-identical


def test_should_write_no_approval_key_when_the_gate_is_off(monkeypatch):
    monkeypatch.delenv(pa.APPROVAL_ENV, raising=False)
    manifest = _manifest()

    assert "approval" not in manifest
    assert pa.reset_if_changed(manifest) is False
    assert "approval" not in manifest


def test_should_write_a_byte_identical_manifest_when_the_gate_is_off(
    tmp_path, monkeypatch
):
    """The default-off path writes no approval key at all."""
    from pipeline import ingest as ingest_mod
    from pipeline import ticketing as pt

    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(ppers, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(
        pt, "plane_request", lambda *a, **kw: pytest.fail("Plane must not be called")
    )
    monkeypatch.delenv(pa.APPROVAL_ENV, raising=False)
    monkeypatch.setattr(
        ingest_mod,
        "on_ingest",
        lambda manifest: pytest.fail("on_ingest must not run while disabled"),
    )
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "E", "stories": [{"summary": "Do it", "key": "S1"}]}],
    }
    (plan_dir / "off.json").write_text(json.dumps(plan))

    result = p.ingest_plan("off")

    written = json.loads((plan_dir / "off.manifest.json").read_text())
    assert result["ok"] is True
    assert "approval" not in written
