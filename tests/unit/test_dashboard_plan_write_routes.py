"""Tests for W1c-02: two POST routes on app/dashboard.py that delegate to
the `_service` PipelineService singleton (added in W1c-01):

  POST /api/plans/{plan_name}/save    -> _service.save_plan(plan_name, plan_json)
  POST /api/plans/{plan_name}/ingest  -> _service.ingest_plan(plan_name, only_epics=..., overwrite=...)

Per the existing GET routes' convention in this file (e.g. get_plan / get_story_journal
around line 758+), a service result with ok: False must raise HTTPException with an
appropriate status code and the service's own error message as the detail; a
successful result is returned as-is (no reshaping).

Neither route exists yet on this branch - these tests must fail (404 route-not-found
from the TestClient, or AttributeError on `_service.save_plan`/`_service.ingest_plan`
in the delegation tests) until app/dashboard.py is updated.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p
from pipeline import ticketing as pt


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect every PLAN_DIR binding the save/ingest path touches (the
    dashboard's own copy, plus pipeline.server/persistence/concurrency's)
    to the same tmp directory, mirroring tests/unit/conftest.py's shared
    `plan_dir` fixture (shadowed here since this module needs to also patch
    app.dashboard.PLAN_DIR)."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(d, "PLAN_DIR", directory)
    monkeypatch.setattr(p, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


@pytest.fixture(autouse=True)
def _plane_disabled(monkeypatch):
    """Force NullTicketProvider so ingest_plan never attempts a real Plane
    HTTP call in this test module."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")


@pytest.fixture
def client():
    return TestClient(d.app)


def _write_plan_file(plan_dir, name, plan):
    (plan_dir / f"{name}.json").write_text(json.dumps(plan))


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def test_save_and_ingest_routes_are_registered_as_post():
    routes = {
        (route.path, method)
        for route in d.app.routes
        for method in getattr(route, "methods", set()) or set()
    }
    assert ("/api/plans/{plan_name}/save", "POST") in routes
    assert ("/api/plans/{plan_name}/ingest", "POST") in routes


# ---------------------------------------------------------------------------
# POST /api/plans/{plan_name}/save
# ---------------------------------------------------------------------------

def test_save_plan_valid_payload_returns_200_and_persists_file(client, plan_dir):
    plan = {"epics": [{"summary": "Epic 1", "stories": [{"summary": "Story 1"}]}]}

    res = client.post(
        "/api/plans/newplan/save",
        json={"plan_json": json.dumps(plan)},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["epic_count"] == 1
    assert body["story_count"] == 1
    assert body["path"] == str(plan_dir / "newplan.json")
    saved = json.loads((plan_dir / "newplan.json").read_text())
    assert saved == plan


def test_save_plan_delegates_to_service_with_plan_name_and_plan_json(client, plan_dir, monkeypatch):
    calls = []

    def fake_save_plan(plan_name, plan_json, workspace=None):
        calls.append((plan_name, plan_json, workspace))
        return {"ok": True, "path": "irrelevant", "epic_count": 0, "story_count": 0}

    monkeypatch.setattr(d._service, "save_plan", fake_save_plan)

    res = client.post("/api/plans/somename/save", json={"plan_json": "{\"epics\": []}"})

    assert res.status_code == 200
    assert calls == [("somename", "{\"epics\": []}", None)]


def test_save_plan_returns_service_result_as_is(client, plan_dir, monkeypatch):
    sentinel = {"ok": True, "path": "/x/y.json", "epic_count": 7, "story_count": 42}
    monkeypatch.setattr(d._service, "save_plan", lambda plan_name, plan_json, workspace=None: sentinel)

    res = client.post("/api/plans/anything/save", json={"plan_json": "{}"})

    assert res.status_code == 200
    assert res.json() == sentinel


def test_save_plan_malformed_json_returns_4xx_and_does_not_write_file(client, plan_dir):
    res = client.post(
        "/api/plans/badplan/save",
        json={"plan_json": "{not valid json"},
    )

    assert 400 <= res.status_code < 500
    assert "Invalid JSON" in res.json()["detail"]
    assert not (plan_dir / "badplan.json").exists()


def test_save_plan_empty_string_plan_json_returns_4xx(client, plan_dir):
    res = client.post("/api/plans/emptyjson/save", json={"plan_json": ""})

    assert 400 <= res.status_code < 500
    # An empty string is invalid JSON too, so this must route through the
    # same "Invalid JSON: ..." branch as the malformed-JSON case above -
    # pinned explicitly so this doesn't accidentally pass on a 405 from a
    # not-yet-implemented route (405 is also in the 4xx range).
    assert "Invalid JSON" in res.json()["detail"]
    assert not (plan_dir / "emptyjson.json").exists()


def test_save_plan_missing_epics_key_returns_4xx_with_service_error_message(client, plan_dir):
    res = client.post(
        "/api/plans/noepics/save",
        json={"plan_json": json.dumps({"not_epics": []})},
    )

    assert 400 <= res.status_code < 500
    assert res.json()["detail"] == "Plan must contain 'epics' key"
    assert not (plan_dir / "noepics.json").exists()


def test_save_plan_missing_plan_json_field_returns_422(client, plan_dir):
    # plan_json is a required field of the documented body shape
    # {"plan_json": <string>}; FastAPI must reject an entirely absent field
    # with its standard 422 request-validation response.
    res = client.post("/api/plans/whatever/save", json={})

    assert res.status_code == 422


def test_save_plan_wrong_type_plan_json_returns_422(client, plan_dir):
    # Boundary/negative: plan_json must be a JSON string per the contract,
    # not an object - sending the plan object directly (not JSON-encoded as
    # a string) must fail FastAPI's own type validation, not reach the
    # service at all.
    res = client.post(
        "/api/plans/wrongtype/save",
        json={"plan_json": {"epics": []}},
    )

    assert res.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/plans/{plan_name}/ingest
# ---------------------------------------------------------------------------

def test_ingest_plan_valid_payload_returns_200_and_creates_manifest(client, plan_dir, tmp_path):
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{
            "summary": "Epic1",
            "stories": [{"summary": "Story1", "key": "S1"}],
        }],
    }
    _write_plan_file(plan_dir, "ingestme", plan)

    res = client.post("/api/plans/ingestme/ingest", json={})

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "S1" in body["stories"]
    assert body["stories"]["S1"]["summary"] == "Story1"
    manifest = json.loads((plan_dir / "ingestme.manifest.json").read_text())
    assert "S1" in manifest["stories"]


def test_ingest_plan_omitted_body_uses_defaults(client, plan_dir, tmp_path):
    # The body is documented as optional - a client must be able to POST
    # with no JSON body at all and still get a full ingest (only_epics=None,
    # overwrite=False).
    plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "Epic1", "stories": [{"summary": "Story1", "key": "S1"}]}],
    }
    _write_plan_file(plan_dir, "nobody", plan)

    res = client.post("/api/plans/nobody/ingest")

    assert res.status_code == 200
    assert res.json()["ok"] is True
    manifest = json.loads((plan_dir / "nobody.manifest.json").read_text())
    assert "S1" in manifest["stories"]


def test_ingest_plan_only_epics_filters_stories(client, plan_dir, tmp_path):
    plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {"summary": "EpicA", "stories": [{"summary": "StoryA", "key": "SA"}]},
            {"summary": "EpicB", "stories": [{"summary": "StoryB", "key": "SB"}]},
        ],
    }
    _write_plan_file(plan_dir, "filterme", plan)

    res = client.post(
        "/api/plans/filterme/ingest",
        json={"only_epics": ["EpicA"]},
    )

    assert res.status_code == 200
    body = res.json()
    assert "SA" in body["stories"]
    assert "SB" not in body["stories"]


def test_ingest_plan_overwrite_true_drops_prior_untouched_stories(client, plan_dir, tmp_path):
    first_plan = {
        "repo_root": str(tmp_path),
        "epics": [
            {"summary": "EpicA", "stories": [{"summary": "StoryA", "key": "SA"}]},
            {"summary": "EpicB", "stories": [{"summary": "StoryB", "key": "SB"}]},
        ],
    }
    _write_plan_file(plan_dir, "overwriteme", first_plan)
    first = client.post("/api/plans/overwriteme/ingest", json={})
    assert first.status_code == 200
    assert "SB" in first.json()["stories"]

    second_plan = {
        "repo_root": str(tmp_path),
        "epics": [{"summary": "EpicA", "stories": [{"summary": "StoryA", "key": "SA"}]}],
    }
    _write_plan_file(plan_dir, "overwriteme", second_plan)

    second = client.post(
        "/api/plans/overwriteme/ingest",
        json={"only_epics": ["EpicA"], "overwrite": True},
    )

    assert second.status_code == 200
    assert "SB" not in second.json()["stories"]
    assert "SA" in second.json()["stories"]


def test_ingest_plan_delegates_to_service_with_kwargs(client, plan_dir, monkeypatch):
    calls = []

    def fake_ingest_plan(plan_name, only_epics=None, overwrite=False):
        calls.append((plan_name, only_epics, overwrite))
        return {"ok": True}

    monkeypatch.setattr(d._service, "ingest_plan", fake_ingest_plan)

    res = client.post(
        "/api/plans/deleg/ingest",
        json={"only_epics": ["E1"], "overwrite": True},
    )

    assert res.status_code == 200
    assert calls == [("deleg", ["E1"], True)]


def test_ingest_plan_delegates_defaults_when_body_omitted(client, plan_dir, monkeypatch):
    calls = []

    def fake_ingest_plan(plan_name, only_epics=None, overwrite=False):
        calls.append((plan_name, only_epics, overwrite))
        return {"ok": True}

    monkeypatch.setattr(d._service, "ingest_plan", fake_ingest_plan)

    res = client.post("/api/plans/deleg2/ingest")

    assert res.status_code == 200
    assert calls == [("deleg2", None, False)]


def test_ingest_plan_returns_service_result_as_is(client, plan_dir, monkeypatch):
    sentinel = {"ok": True, "manifest_path": "/x/y.manifest.json", "stories": {}, "epics": {}}
    monkeypatch.setattr(
        d._service, "ingest_plan",
        lambda plan_name, only_epics=None, overwrite=False: sentinel,
    )

    res = client.post("/api/plans/anything/ingest", json={})

    assert res.status_code == 200
    assert res.json() == sentinel


def test_ingest_plan_unknown_plan_returns_4xx_with_service_error(client, plan_dir):
    res = client.post("/api/plans/doesnotexist/ingest", json={})

    assert 400 <= res.status_code < 500
    assert res.json()["detail"] == "No plan named doesnotexist"


def test_ingest_plan_nonexistent_repo_root_returns_4xx(client, plan_dir):
    repo_root = str(plan_dir / "does-not-exist-dir")
    plan = {
        "repo_root": repo_root,
        "epics": [{"summary": "E1", "stories": [{"summary": "S1", "key": "S1"}]}],
    }
    _write_plan_file(plan_dir, "badrepo", plan)

    res = client.post("/api/plans/badrepo/ingest", json={})

    assert 400 <= res.status_code < 500
    assert res.json()["detail"] == (
        f"Plan repo_root is missing or not a directory: {repo_root!r}"
    )
    assert not (plan_dir / "badrepo.manifest.json").exists()
