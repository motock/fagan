"""The dashboard face of the plan guidance record.

``POST /api/plans/{plan}/guidance/resolve`` settles a plan's record and
``GET /api/plans/{plan}/review`` carries it to the approver. Requests run through
a real ``TestClient`` so the authentication middleware is exercised; the shared
dashboard key stands in for the caller, so no network call is made.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import dashboard as d
from pipeline import plan_approval, plan_guidance
from pipeline.plan_approval import canonical_digest

_HEADER = "X-Pipeline-Api-Key"
_PLAN = "gp"


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    """Point app.auth at a throwaway key file so no real key file is touched."""
    path = tmp_path / ".dashboard_api_key"
    monkeypatch.setattr(auth, "API_KEY_PATH", path)
    return path


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    from pipeline import persistence
    from pipeline import server as p

    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    monkeypatch.setenv(plan_approval.APPROVAL_ENV, "on")
    manifest = {
        "repo_root": "/tmp/repo",
        "stories": {"S1": {"summary": "do it", "risk": "low"}},
    }
    manifest["approval"] = {
        "status": "pending_approval",
        "digest": canonical_digest(manifest),
        "digest_version": plan_approval.APPROVAL_DIGEST_VERSION,
        "warnings_seen": [],
    }
    (tmp_path / f"{_PLAN}.manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def _client(key_file):
    return TestClient(d.app, headers={_HEADER: auth.get_or_create_api_key()})


def _raise_needed():
    result = plan_guidance.raise_guidance(
        _PLAN, ["Which database?"], "jira", "PROJ-7", "ticket text"
    )
    assert result["ok"] is True


def _manifest(plan_dir):
    return json.loads((plan_dir / f"{_PLAN}.manifest.json").read_text())


def test_resolve_settles_a_needed_record(plan_dir, key_file):
    _raise_needed()

    response = _client(key_file).post(
        f"/api/plans/{_PLAN}/guidance/resolve", json={"note": "use postgres"}
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "resolved_by", "resolved_at"}
    assert body["status"] == "resolved"
    record = _manifest(plan_dir)["guidance"]
    assert record["status"] == "resolved"
    assert record["note"] == "use postgres"


def test_resolve_on_a_plan_with_no_record_is_a_conflict(plan_dir, key_file):
    response = _client(key_file).post(f"/api/plans/{_PLAN}/guidance/resolve", json={})

    assert response.status_code == 409
    assert response.json()["detail"] == "plan has no guidance to resolve"


def test_resolve_twice_is_a_conflict(plan_dir, key_file):
    _raise_needed()
    client = _client(key_file)
    client.post(f"/api/plans/{_PLAN}/guidance/resolve", json={})

    response = client.post(f"/api/plans/{_PLAN}/guidance/resolve", json={})

    assert response.status_code == 409
    assert response.json()["detail"] == "plan guidance is already resolved"


def test_resolve_refuses_an_invalid_plan_name_as_not_found(plan_dir, key_file):
    response = _client(key_file).post("/api/plans/bad%20name/guidance/resolve", json={})

    assert response.status_code == 404
    assert response.json()["detail"] == "plan not found"


def test_resolve_refuses_an_overlong_note(plan_dir, key_file):
    _raise_needed()

    response = _client(key_file).post(
        f"/api/plans/{_PLAN}/guidance/resolve", json={"note": "x" * 501}
    )

    assert response.status_code == 422
    assert _manifest(plan_dir)["guidance"]["status"] == "needed"


def test_resolve_refuses_an_unexpected_body_field(plan_dir, key_file):
    _raise_needed()

    response = _client(key_file).post(
        f"/api/plans/{_PLAN}/guidance/resolve", json={"note": "", "status": "resolved"}
    )

    assert response.status_code == 422
    assert _manifest(plan_dir)["guidance"]["status"] == "needed"


def test_the_review_payload_carries_the_record_to_the_approver(plan_dir, key_file):
    _raise_needed()

    response = _client(key_file).get(f"/api/plans/{_PLAN}/review")

    assert response.status_code == 200
    guidance = response.json()["guidance"]
    assert guidance["status"] == "needed"
    assert guidance["questions"] == ["Which database?"]
    assert guidance["source"] == {"kind": "jira", "ref": "PROJ-7"}


def test_the_review_payload_reports_null_guidance_when_none_was_raised(plan_dir, key_file):
    response = _client(key_file).get(f"/api/plans/{_PLAN}/review")

    assert response.status_code == 200
    assert response.json()["guidance"] is None
