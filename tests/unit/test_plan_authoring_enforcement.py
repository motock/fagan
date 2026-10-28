"""ENF-02: ``save_plan`` asks ``plan:ingest``; ``request_decision`` asks ``story:patch``.

``save_plan`` writes ``PLAN_DIR/<plan>.json`` -- the content a later, gated
``ingest_plan`` reads. Without a gate any authenticated dashboard caller could
swap that stored content past the approval digest, so the write now asks
``pipeline.authorization.authorize`` for a decision before it parses the JSON
and before it touches the filesystem.

``request_decision`` runs an overlord model call and can park a story, so it
asks ``story:patch`` -- it changes the story's state (the ruling, a park).
A refused caller must reach neither the overlord, the decisions log nor the
manifest.

``decompose_plan`` is deliberately NOT gated: it returns a draft and writes no
state (recorded in docs/specs/IDENTITY_AND_POLICY.md section 4).

The contract these tests pin down:

* ``local-operator`` -- the identity the unattended scheduler and daemon run as
  -- still saves a plan and still reaches the overlord, so nothing about the
  automated paths changes;
* a denied principal gets the refusal dict unchanged, the plan file on disk is
  byte-identical before and after, and no overlord call / decision / park
  happens;
* each denial appends exactly one ``authorization_denied`` sidecar record
  naming the refused action (``authorize`` writes it, the service must not add
  a second);
* ``PIPELINE_POLICY_ENFORCE=0`` is the operator's kill switch, read per call;
* ``_validate_key`` runs FIRST, so a traversal is refused before ``authorize``
  can append a sidecar record -- the same ordering ``ingest_plan`` uses;
* the dashboard ``save`` route answers 403 for a refusal BEFORE the JSON branch
  (a refused caller posting invalid JSON must not get a 400), and still 400 for
  an allowed caller with invalid JSON.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline.actor import ANONYMOUS, bind_principal
from pipeline.authorization import ENFORCE_ENV

PLAN = "enfplan"
STORY = "ENF-S1"
DENIAL_EVENT = "authorization_denied"

#: A ruling the patched overlord returns; the shape ``_parse_ruling`` expects.
CANNED_RULING = (
    "RULING: Reuse the existing http client.\n"
    "TIER: routine\n"
    "RISK: low\n"
    "RATIONALE: The stack already includes httpx.\n"
    "NOTIFY_USER: no\n"
)

PLAN_BODY = {
    "epics": [
        {"key": "E1", "title": "epic one", "stories": [{"key": STORY, "summary": "s"}]}
    ]
}


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding at a throwaway directory.

    ``pipeline.service`` reads PLAN_DIR through a ``_ServerRef``, so patching
    ``pipeline.server`` covers the service; the persistence/concurrency
    bindings are patched too for the manifest and sidecar reads, and
    ``app.dashboard``'s own copy for the route's workspace lookup.
    """
    from app import dashboard as d

    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(d, "PLAN_DIR", directory)
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def client(plan_dir):
    from app import dashboard as d

    return TestClient(d.app)


def _write_manifest(plan_dir, stories):
    (plan_dir / f"{PLAN}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories}, indent=2)
    )


def _read_manifest(plan_dir):
    return json.loads((plan_dir / f"{PLAN}.manifest.json").read_text())


def _decisions(plan_dir):
    path = plan_dir / f"{PLAN}.decisions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _records(plan_dir, plan_name=PLAN):
    """Every ``authorization_denied`` record in the plan's JSONL sidecar."""
    path = plan_dir / f"{plan_name}.notifications.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == DENIAL_EVENT]


def _audit(plan_dir, plan_name=PLAN):
    """Every record in the plan's audit stream, in append order."""
    path = plan_dir / f"{plan_name}.audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _save(plan_json):
    return p.PipelineService().save_plan(PLAN, plan_json)


def _ask(monkeypatch, ruling=CANNED_RULING):
    """Ask the overlord a question with the backend patched out."""
    calls = []
    monkeypatch.setattr(p, "_invoke_overlord", lambda prompt, **k: calls.append(prompt) or ruling)
    result = p.PipelineService().request_decision(PLAN, STORY, "Which client?", ["add", "reuse"])
    return result, calls


# --------------------------------------------------------------------------- #
# Positive: the local operator is unaffected
# --------------------------------------------------------------------------- #
def test_local_operator_save_plan_returns_unchanged_result(plan_dir, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    body = json.dumps(PLAN_BODY)

    with bind_principal(None):
        result = _save(body)

    assert result["ok"] is True
    assert result["path"] == str(plan_dir / f"{PLAN}.json")
    assert result["epic_count"] == 1
    assert json.loads((plan_dir / f"{PLAN}.json").read_text()) == PLAN_BODY


def test_allowed_save_appends_one_allowed_plan_ingest_record(plan_dir, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with bind_principal(None):
        assert _save(json.dumps(PLAN_BODY))["ok"] is True

    allowed = [r for r in _audit(plan_dir) if r.get("outcome") == "allowed"]
    assert len(allowed) == 1
    assert allowed[0]["action"] == "plan:ingest"
    assert _records(plan_dir) == []


def test_local_operator_request_decision_still_reaches_overlord(plan_dir, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "in_progress"}})

    with bind_principal(None):
        result, calls = _ask(monkeypatch)

    assert len(calls) == 1
    assert isinstance(result, dict)
    assert result["ruling"] == "Reuse the existing http client."
    assert result["story_key"] == STORY


# --------------------------------------------------------------------------- #
# Negative: a bound non-operator
# --------------------------------------------------------------------------- #
def test_non_operator_save_plan_is_refused_and_writes_nothing(plan_dir, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    path = plan_dir / f"{PLAN}.json"
    path.write_text(json.dumps({"epics": [], "marker": "original"}))
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    with bind_principal(ANONYMOUS):
        result = _save(json.dumps(PLAN_BODY))

    assert result["ok"] is False and result.get("forbidden") is True
    assert result.get("action") == "plan:ingest"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert json.loads(path.read_text()) == {"epics": [], "marker": "original"}


def test_non_operator_save_plan_creates_no_plan_file(plan_dir, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with bind_principal(ANONYMOUS):
        result = _save(json.dumps(PLAN_BODY))

    assert result["ok"] is False
    assert not (plan_dir / f"{PLAN}.json").exists()


def test_refused_save_appends_exactly_one_denied_record(plan_dir, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with bind_principal(ANONYMOUS):
        _save(json.dumps(PLAN_BODY))

    denials = _records(plan_dir)
    assert len(denials) == 1
    assert "plan:ingest" in json.dumps(denials[0])


def test_denied_then_allowed_save_appends_two_records(plan_dir, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with bind_principal(ANONYMOUS):
        assert _save(json.dumps(PLAN_BODY))["ok"] is False
    with bind_principal(None):
        assert _save(json.dumps(PLAN_BODY))["ok"] is True

    sidecar = _audit(plan_dir)
    assert len([r for r in sidecar if r.get("outcome") == "denied"]) == 1
    assert len([r for r in sidecar if r.get("outcome") == "allowed"]) == 1
    assert len(_records(plan_dir)) == 1


def test_non_operator_request_decision_never_calls_overlord(plan_dir, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "in_progress"}})
    before_decisions = _decisions(plan_dir)

    with bind_principal(ANONYMOUS):
        result, calls = _ask(monkeypatch)

    assert result["ok"] is False and result.get("forbidden") is True
    assert result.get("action") == "story:patch"
    assert calls == []
    assert _decisions(plan_dir) == before_decisions
    assert _read_manifest(plan_dir)["stories"][STORY]["status"] == "in_progress"


def test_refused_decision_appends_exactly_one_denied_record(plan_dir, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "in_progress"}})

    with bind_principal(ANONYMOUS):
        _ask(monkeypatch)

    denials = _records(plan_dir)
    assert len(denials) == 1
    assert "story:patch" in json.dumps(denials[0])


def test_denied_then_allowed_decision_leaves_nothing_half_written(plan_dir, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "in_progress"}})

    with bind_principal(ANONYMOUS):
        assert _ask(monkeypatch)[0]["ok"] is False
    assert _decisions(plan_dir) == []

    with bind_principal(None):
        result, calls = _ask(monkeypatch)

    assert len(calls) == 1
    assert isinstance(result, dict) and result["story_key"] == STORY
    assert len(_decisions(plan_dir)) == 1


# --------------------------------------------------------------------------- #
# Kill switch
# --------------------------------------------------------------------------- #
def test_enforcement_disabled_allows_save_and_decision(plan_dir, monkeypatch):
    monkeypatch.setenv(ENFORCE_ENV, "0")
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "in_progress"}})

    with bind_principal(ANONYMOUS):
        saved = _save(json.dumps(PLAN_BODY))
        result, calls = _ask(monkeypatch)

    assert saved["ok"] is True
    assert (plan_dir / f"{PLAN}.json").exists()
    assert len(calls) == 1
    assert isinstance(result, dict)
    assert _records(plan_dir) == []


# --------------------------------------------------------------------------- #
# Ordering: validation before authorize
# --------------------------------------------------------------------------- #
def test_traversal_key_is_refused_before_authorize_records_anything(plan_dir, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with (
        bind_principal(ANONYMOUS),
        pytest.raises(ValueError, match="invalid plan/story key"),
    ):
        p.PipelineService().save_plan("../etc", json.dumps(PLAN_BODY))

    assert _records(plan_dir, "../etc") == []


def test_traversal_story_key_is_refused_before_authorize(plan_dir, monkeypatch):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_manifest(plan_dir, {STORY: {"summary": "s", "status": "in_progress"}})

    with (
        bind_principal(ANONYMOUS),
        pytest.raises(ValueError, match="invalid plan/story key"),
    ):
        p.PipelineService().request_decision(PLAN, "../x", "q", ["a"])

    assert _records(plan_dir) == []


# --------------------------------------------------------------------------- #
# Dashboard route: 403 before the 400 JSON branch
# --------------------------------------------------------------------------- #
def test_dashboard_save_route_answers_403_for_a_refused_caller(client, plan_dir, monkeypatch):
    from app import dashboard as d

    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    def refusing_save(plan_name, plan_json, workspace=None):
        with bind_principal(ANONYMOUS):
            return p.PipelineService().save_plan(plan_name, plan_json, workspace)

    monkeypatch.setattr(d._service, "save_plan", refusing_save)

    res = client.post("/api/plans/badplan/save", json={"plan_json": "not-json{"})

    assert res.status_code == 403
    assert "not authorized: plan:ingest" in res.json()["detail"]
    assert not (plan_dir / "badplan.json").exists()


def test_dashboard_save_route_still_answers_400_for_allowed_invalid_json(
    client, plan_dir, monkeypatch
):
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    res = client.post("/api/plans/badplan/save", json={"plan_json": "{not valid json"})

    assert res.status_code == 400
    assert "Invalid JSON" in res.json()["detail"]
