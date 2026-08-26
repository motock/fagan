"""Tests for W3b-B3: HTTP config-write routes on app/dashboard.py.

This story exposes the W3b-B1/B2 config write service methods over HTTP so the
dashboard UI (W3b-B4) can drive them, and adds the per-story ``backend`` field
to the PATCH story route's request body. It touches ONLY ``app/dashboard.py``;
the service-layer validation lives in B2.

New routes (all POST, all delegate to the module-level ``_service`` singleton):

  * POST /api/config/roles/{role}
        body RoleDefaultBody {provider: str, model: str}
        -> _service.set_role_default(role, body.provider, body.model)
        400 on failure (with the service error message as detail), 200 returns
        the service result PLUS the updated effective config for that role
        (config_provenance.resolve_role_provenance(role, registry=...)).

  * POST /api/config/plans/{plan_name}/roles/{role}
        body PlanRoleConfigBody {provider: str | None = None, model: str | None = None}
        -> _service.set_plan_role_config(plan_name, role, body.provider, body.model)
        404 if plan_name not in _store.list_manifests(); 400 on failure; 200
        returns the service result PLUS the updated per-plan effective config.

  * PATCH /api/plans/{plan}/stories/{story}/patch gains a ``backend`` field on
    its StoryPatchBody so the route forwards ``backend`` to
    _service.patch_story (B2 validates it).

All write endpoints surface validation failures as HTTP 400 with the error
message (NOT 500), and return the updated effective config for the affected
scope on success.

These tests describe behavior for code that does not exist yet on this branch
and must fail (route 404 / AttributeError / AssertionError) until app/dashboard.py
is updated.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from app import role_registry
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as psrv
from pipeline import ticketing as pt

REGISTRY_PATH_ENV = "PIPELINE_MODEL_REGISTRY_PATH"

# The documented allowlist of valid story `backend` values (mirrors
# pipeline/server.py's _VALID_STORY_BACKENDS = backend._DRIVERS | {"auto"}).
_VALID_BACKENDS = frozenset({"claude", "local", "ollama", "lmstudio", "mlx", "auto"})


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect every PLAN_DIR binding the write paths touch (the dashboard's
    own copy, plus pipeline.server/persistence/concurrency's) to the same tmp
    directory, mirroring tests/unit/conftest.py's shared `plan_dir` fixture
    (shadowed here since this module must also patch app.dashboard.PLAN_DIR)."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(d, "PLAN_DIR", directory)
    monkeypatch.setattr(psrv, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    """Point the model registry at a tmp file so set_role_default writes never
    touch the real repo model_registry.json."""
    path = tmp_path / "registry.json"
    monkeypatch.setenv(REGISTRY_PATH_ENV, str(path))
    return path


@pytest.fixture
def seeded_registry(registry_path):
    """A registry with two providers and a couple of declared roles."""
    registry = {
        "providers": {
            "claude": {
                "models": {
                    "opus": {"tag": "opus"},
                    "sonnet": {"tag": "sonnet"},
                    "haiku": {"tag": "haiku"},
                }
            },
            "ollama": {
                "models": {
                    "glm": {"tag": "glm-5.2:cloud"},
                    "devstral": {"tag": "devstral:24b"},
                }
            },
        },
        "roles": {
            "review": {"provider": "ollama", "model": "glm"},
            "planner": {"provider": "claude", "model": "sonnet"},
        },
    }
    registry_path.write_text(json.dumps(registry, indent=2))
    return registry


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


@pytest.fixture
def isolated_config_sources(monkeypatch, tmp_path):
    """Point config_provenance's source files at nonexistent paths under
    tmp_path so /api/config tests never read this machine's real ~/.claude.json
    or launchd plist (which may hold real secrets)."""
    monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(tmp_path / "no-scheduler.plist"))
    monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(tmp_path / "no-claude.json"))
    return tmp_path


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _manifest_path(plan_dir, plan_name):
    return plan_dir / f"{plan_name}.manifest.json"


def _write_manifest(plan_dir, plan_name, manifest):
    _manifest_path(plan_dir, plan_name).write_text(json.dumps(manifest, indent=2))


def _read_manifest(plan_dir, plan_name):
    return json.loads(_manifest_path(plan_dir, plan_name).read_text())


def _manifest_bytes(plan_dir, plan_name):
    return _manifest_path(plan_dir, plan_name).read_bytes()


def _bare_manifest(*, role_config=None, stories=None):
    """A minimal valid manifest with an optional role_config and stories."""
    return {
        "epics": {},
        "stories": stories if stories is not None else {},
        "repo_root": "/tmp",
        **({"role_config": role_config} if role_config is not None else {}),
    }


def _story(**overrides):
    base = {
        "summary": "do the thing",
        "status": "ready",
        "model": "sonnet",
        "persona": "coder",
    }
    base.update(overrides)
    return base


def _routes():
    return {
        (route.path, method)
        for route in d.app.routes
        for method in (getattr(route, "methods", None) or set())
        if method != "HEAD"
    }


# =========================================================================== #
# Route registration
# =========================================================================== #
def test_role_default_route_registered_as_post():
    assert ("/api/config/roles/{role}", "POST") in _routes()


def test_plan_role_config_route_registered_as_post():
    assert ("/api/config/plans/{plan_name}/roles/{role}", "POST") in _routes()


# =========================================================================== #
# GET /api/config/providers
# =========================================================================== #
def test_config_providers_returns_stubbed_registry_providers_exactly(client, monkeypatch):
    stub_registry = {
        "providers": {
            "claude": {
                "models": {
                    "opus": {"tag": "opus"},
                    "sonnet": {"tag": "sonnet"},
                }
            },
            "ollama": {
                "models": {
                    "glm": {"tag": "glm-5.2:cloud"},
                    "devstral": {"tag": "devstral:24b"},
                }
            },
        },
        "roles": {"review": {"provider": "ollama", "model": "glm"}},
    }
    monkeypatch.setattr(d.role_registry, "load_registry", lambda *a, **k: stub_registry)

    res = client.get("/api/config/providers")

    assert res.status_code == 200
    assert res.json() == {"providers": stub_registry["providers"]}


def test_config_providers_returns_empty_catalog_on_registry_error(client, monkeypatch):
    def _raise(*a, **k):
        raise role_registry.RoleRegistryError("boom")

    monkeypatch.setattr(d.role_registry, "load_registry", _raise)

    res = client.get("/api/config/providers")

    assert res.status_code == 200
    assert res.json() == {"providers": {}}


# =========================================================================== #
# Body model classes exist on the dashboard module
# =========================================================================== #
def test_role_default_body_model_exists_with_required_fields():
    """RoleDefaultBody(BaseModel) must declare provider: str and model: str
    (both required, not optional)."""
    body = d.RoleDefaultBody(provider="claude", model="sonnet")
    assert body.provider == "claude"
    assert body.model == "sonnet"


def test_plan_role_config_body_model_exists_with_optional_fields():
    """PlanRoleConfigBody(BaseModel) must declare provider: str | None = None
    and model: str | None = None (both optional)."""
    body = d.PlanRoleConfigBody()
    assert body.provider is None
    assert body.model is None
    body2 = d.PlanRoleConfigBody(provider="claude", model="sonnet")
    assert body2.provider == "claude"
    assert body2.model == "sonnet"


def test_story_patch_body_has_backend_field():
    """StoryPatchBody must now declare backend: str | None = None so the PATCH
    route can forward it to _service.patch_story (B2 validates it)."""
    body = d.StoryPatchBody(backend="local")
    assert body.backend == "local"
    # default is None
    assert d.StoryPatchBody().backend is None


# =========================================================================== #
# POST /api/config/roles/{role}  (set_role_default)
# =========================================================================== #
def test_set_role_default_valid_returns_200_and_reflects_in_get_config(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """(1) POST valid body to /api/config/roles/review -> 200, subsequent
    GET /api/config reflects the new role default."""
    res = client.post(
        "/api/config/roles/review",
        json={"provider": "claude", "model": "sonnet"},
    )

    assert res.status_code == 200
    body = res.json()
    # The service result is included.
    assert body["ok"] is True
    assert body["role"] == "review"
    assert body["provider"] == "claude"
    assert body["model"] == "sonnet"
    # The updated effective config for that role is included.
    assert "effective_config" in body, "response must include updated effective config for the role"
    eff = body["effective_config"]
    assert eff["role"] == "review"
    assert eff["provider"] == "claude"
    assert eff["model"] == "sonnet"

    # GET /api/config reflects the new role default.
    cfg = client.get("/api/config").json()
    review = next(r for r in cfg["roles"] if r["role"] == "review")
    assert review["provider"] == "claude"
    assert review["model"] == "sonnet"


def test_set_role_default_delegates_to_service_with_provider_and_model(
    client, plan_dir, monkeypatch
):
    """The route must call _service.set_role_default(role, body.provider,
    body.model) — positional provider/model, not a dict."""
    calls = []

    def fake_set_role_default(role, provider, model):
        calls.append((role, provider, model))
        return {"ok": True, "role": role, "provider": provider, "model": model}

    monkeypatch.setattr(d._service, "set_role_default", fake_set_role_default)
    monkeypatch.setattr(d.role_registry, "load_registry", lambda *a, **k: {"providers": {}, "roles": {}})

    res = client.post(
        "/api/config/roles/review",
        json={"provider": "claude", "model": "sonnet"},
    )

    assert res.status_code == 200
    assert calls == [("review", "claude", "sonnet")]


def test_set_role_default_invalid_model_returns_400_with_message_and_leaves_registry_unchanged(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """(2) POST invalid model -> 400 with detail mentioning the unknown model,
    AND assert model_registry.json unchanged on disk."""
    before = registry_path_bytes(seeded_registry)

    res = client.post(
        "/api/config/roles/review",
        json={"provider": "claude", "model": "totally-fake-model"},
    )

    assert res.status_code == 400, "validation failure must be 400, not 500"
    detail = res.json()["detail"]
    assert "totally-fake-model" in detail
    # Registry file byte-identical before/after.
    after = registry_path_bytes(seeded_registry)
    assert after == before


def test_set_role_default_unknown_provider_returns_400(client, plan_dir, seeded_registry, isolated_config_sources):
    before = registry_path_bytes(seeded_registry)
    res = client.post(
        "/api/config/roles/review",
        json={"provider": "no-such-provider", "model": "sonnet"},
    )
    assert res.status_code == 400
    assert "no-such-provider" in res.json()["detail"]
    assert registry_path_bytes(seeded_registry) == before


def test_set_role_default_unknown_role_returns_400(client, plan_dir, seeded_registry, isolated_config_sources):
    before = registry_path_bytes(seeded_registry)
    res = client.post(
        "/api/config/roles/not-a-real-role",
        json={"provider": "claude", "model": "sonnet"},
    )
    assert res.status_code == 400
    assert "not-a-real-role" in res.json()["detail"]
    assert registry_path_bytes(seeded_registry) == before


def test_set_role_default_missing_provider_returns_422(client, plan_dir, seeded_registry, isolated_config_sources):
    """provider is a required field of RoleDefaultBody; FastAPI must reject an
    entirely absent field with its standard 422."""
    res = client.post(
        "/api/config/roles/review",
        json={"model": "sonnet"},
    )
    assert res.status_code == 422


def test_set_role_default_missing_model_returns_422(client, plan_dir, seeded_registry, isolated_config_sources):
    res = client.post(
        "/api/config/roles/review",
        json={"provider": "claude"},
    )
    assert res.status_code == 422


def test_set_role_default_empty_body_returns_422(client, plan_dir, seeded_registry, isolated_config_sources):
    res = client.post("/api/config/roles/review", json={})
    assert res.status_code == 422


def test_set_role_default_wrong_type_provider_returns_422(client, plan_dir, seeded_registry, isolated_config_sources):
    """provider must be a string, not a number — FastAPI body validation."""
    res = client.post(
        "/api/config/roles/review",
        json={"provider": 42, "model": "sonnet"},
    )
    assert res.status_code == 422


def test_set_role_default_400_does_not_mutate_registry(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """(8) NEGATIVE: a 400 response must NOT have mutated any file — registry
    byte-identical before/after."""
    before = registry_path_bytes(seeded_registry)
    res = client.post(
        "/api/config/roles/review",
        json={"provider": "claude", "model": "does-not-exist"},
    )
    assert res.status_code == 400
    assert registry_path_bytes(seeded_registry) == before


def test_set_role_default_success_includes_resolve_role_provenance(
    client, plan_dir, seeded_registry, isolated_config_sources, monkeypatch
):
    """The success response's effective_config must come from
    config_provenance.resolve_role_provenance(role, registry=role_registry.load_registry())
    — pin the provenance keys so the implementer wires the real resolver, not a
    hand-rolled dict."""
    res = client.post(
        "/api/config/roles/review",
        json={"provider": "claude", "model": "sonnet"},
    )
    assert res.status_code == 200
    eff = res.json()["effective_config"]
    # resolve_role_provenance returns these keys.
    for key in ("role", "provider", "model", "provider_source", "model_source"):
        assert key in eff, f"effective_config missing {key!r}"


# =========================================================================== #
# POST /api/config/plans/{plan_name}/roles/{role}  (set_plan_role_config)
# =========================================================================== #
def test_set_plan_role_config_valid_returns_200_and_reflects_in_get_config(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """(3) POST valid per-plan override to /api/config/plans/{plan}/roles/dispatch
    -> 200, GET /api/config?plan={plan} reflects it."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(role_config={"review": {"provider": "ollama", "model": "glm"}}),
    )

    res = client.post(
        "/api/config/plans/demo/roles/dispatch",
        json={"provider": "claude", "model": "sonnet"},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["plan"] == "demo"
    assert body["role"] == "dispatch"
    # The updated per-plan effective config is included.
    assert "effective_config" in body, "response must include updated per-plan effective config"
    eff = body["effective_config"]
    assert eff["role"] == "dispatch"
    assert eff["provider"] == "claude"
    assert eff["model"] == "sonnet"

    # GET /api/config?plan=demo reflects the override.
    cfg = client.get("/api/config?plan=demo").json()
    dispatch = next(r for r in cfg["roles"] if r["role"] == "dispatch")
    assert dispatch["provider"] == "claude"
    assert dispatch["model"] == "sonnet"
    assert dispatch["provider_source"] == "plan_role_config"


def test_set_plan_role_config_delegates_to_service_with_provider_and_model(
    client, plan_dir, monkeypatch
):
    """The route must call _service.set_plan_role_config(plan_name, role,
    body.provider, body.model) — positional provider/model."""
    _write_manifest(plan_dir, "demo", _bare_manifest())
    calls = []

    def fake_set_plan_role_config(plan_name, role, provider=None, model=None):
        calls.append((plan_name, role, provider, model))
        return {"ok": True, "plan": plan_name, "role": role, "role_config": {"provider": provider, "model": model}}

    monkeypatch.setattr(d._service, "set_plan_role_config", fake_set_plan_role_config)
    monkeypatch.setattr(d.role_registry, "load_registry", lambda *a, **k: {"providers": {}, "roles": {}})

    res = client.post(
        "/api/config/plans/demo/roles/dispatch",
        json={"provider": "claude", "model": "sonnet"},
    )

    assert res.status_code == 200
    assert calls == [("demo", "dispatch", "claude", "sonnet")]


def test_set_plan_role_config_delegates_none_when_fields_omitted(
    client, plan_dir, monkeypatch
):
    """PlanRoleConfigBody fields are optional; omitting them must forward None
    to the service (not be required)."""
    _write_manifest(plan_dir, "demo", _bare_manifest())
    calls = []

    def fake_set_plan_role_config(plan_name, role, provider=None, model=None):
        calls.append((plan_name, role, provider, model))
        return {"ok": True, "plan": plan_name, "role": role, "role_config": None}

    monkeypatch.setattr(d._service, "set_plan_role_config", fake_set_plan_role_config)
    monkeypatch.setattr(d.role_registry, "load_registry", lambda *a, **k: {"providers": {}, "roles": {}})

    res = client.post("/api/config/plans/demo/roles/dispatch", json={})

    assert res.status_code == 200
    assert calls == [("demo", "dispatch", None, None)]


def test_set_plan_role_config_invalid_override_returns_400_and_manifest_unchanged(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """(4) POST invalid per-plan override -> 400, manifest unchanged."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(role_config={"review": {"provider": "ollama", "model": "glm"}}),
    )
    before = _manifest_bytes(plan_dir, "demo")

    res = client.post(
        "/api/config/plans/demo/roles/dispatch",
        json={"provider": "claude", "model": "totally-fake-model"},
    )

    assert res.status_code == 400, "validation failure must be 400, not 500"
    assert "totally-fake-model" in res.json()["detail"]
    # Manifest byte-identical before/after.
    assert _manifest_bytes(plan_dir, "demo") == before


def test_set_plan_role_config_nonexistent_plan_returns_404(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """(5) POST to non-existent plan -> 404 (the route guards with
    _store.list_manifests() before delegating)."""
    # No manifest written for "ghost".
    res = client.post(
        "/api/config/plans/ghost/roles/dispatch",
        json={"provider": "claude", "model": "sonnet"},
    )
    assert res.status_code == 404


def test_set_plan_role_config_400_does_not_mutate_manifest(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """(8) NEGATIVE: a 400 response must NOT have mutated any file — manifest
    byte-identical before/after."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(role_config={"review": {"provider": "ollama", "model": "glm"}}),
    )
    before = _manifest_bytes(plan_dir, "demo")

    res = client.post(
        "/api/config/plans/demo/roles/dispatch",
        json={"provider": "claude", "model": "does-not-exist"},
    )
    assert res.status_code == 400
    assert _manifest_bytes(plan_dir, "demo") == before


def test_set_plan_role_config_success_includes_resolve_role_provenance(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """The success response's effective_config must come from
    config_provenance.resolve_role_provenance with the plan's role_config
    layered in — pin the provenance keys."""
    _write_manifest(plan_dir, "demo", _bare_manifest())
    res = client.post(
        "/api/config/plans/demo/roles/review",
        json={"provider": "claude", "model": "sonnet"},
    )
    assert res.status_code == 200
    eff = res.json()["effective_config"]
    for key in ("role", "provider", "model", "provider_source", "model_source"):
        assert key in eff, f"effective_config missing {key!r}"


def test_set_plan_role_config_unknown_role_returns_400(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """An unknown role must surface as 400 (service rejects it), not 500."""
    _write_manifest(plan_dir, "demo", _bare_manifest())
    before = _manifest_bytes(plan_dir, "demo")
    res = client.post(
        "/api/config/plans/demo/roles/not-a-real-role",
        json={"provider": "claude", "model": "sonnet"},
    )
    assert res.status_code == 400
    assert _manifest_bytes(plan_dir, "demo") == before


def test_set_plan_role_config_wrong_type_provider_returns_422(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """provider must be a string or null — a number must be rejected by body
    validation, not reach the service."""
    _write_manifest(plan_dir, "demo", _bare_manifest())
    res = client.post(
        "/api/config/plans/demo/roles/dispatch",
        json={"provider": 42, "model": "sonnet"},
    )
    assert res.status_code == 422


# =========================================================================== #
# PATCH /api/plans/{plan}/stories/{story}/patch  (backend field)
# =========================================================================== #
def test_patch_story_with_backend_local_returns_200(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """(6) PATCH /api/plans/{plan}/stories/{story}/patch with {backend: local}
    -> 200."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(stories={"S1": _story()}),
    )
    res = client.post(
        "/api/plans/demo/stories/S1/patch",
        json={"backend": "local"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    # The story's backend was updated.
    assert body["story"]["backend"] == "local"


def test_patch_story_with_backend_gibberish_returns_400_mentioning_valid_backends(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """(7) PATCH with {backend: gibberish} -> 400 with detail mentioning valid
    backends."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(stories={"S1": _story()}),
    )
    before = _manifest_bytes(plan_dir, "demo")

    res = client.post(
        "/api/plans/demo/stories/S1/patch",
        json={"backend": "gibberish"},
    )

    assert res.status_code == 400, "invalid backend must be 400, not 500"
    detail = res.json()["detail"]
    # The error must mention valid backends (the allowlist).
    assert "backend" in detail.lower()
    # Manifest unchanged.
    assert _manifest_bytes(plan_dir, "demo") == before


def test_patch_story_forwards_backend_to_service(
    client, plan_dir, monkeypatch
):
    """The route must forward backend through to _service.patch_story as part
    of the fields dict (B2 validates it)."""
    _write_manifest(plan_dir, "demo", _bare_manifest(stories={"S1": _story()}))
    calls = []

    def fake_patch_story(plan_name, story_key, fields):
        calls.append((plan_name, story_key, fields))
        return {"ok": True, "story_key": story_key, "story": {"backend": fields.get("backend")}}

    monkeypatch.setattr(d._service, "patch_story", fake_patch_story)

    res = client.post(
        "/api/plans/demo/stories/S1/patch",
        json={"backend": "local"},
    )

    assert res.status_code == 200
    assert len(calls) == 1
    plan_name, story_key, fields = calls[0]
    assert plan_name == "demo"
    assert story_key == "S1"
    assert fields.get("backend") == "local"


def test_patch_story_with_each_valid_backend_is_accepted(
    client, plan_dir, monkeypatch
):
    """Every value in the documented backend allowlist must be accepted and
    forwarded (boundary: each member of the allowlist)."""
    _write_manifest(plan_dir, "demo", _bare_manifest(stories={"S1": _story()}))
    seen = []

    def fake_patch_story(plan_name, story_key, fields):
        seen.append(fields.get("backend"))
        return {"ok": True, "story_key": story_key, "story": {"backend": fields.get("backend")}}

    monkeypatch.setattr(d._service, "patch_story", fake_patch_story)

    for backend in sorted(_VALID_BACKENDS):
        res = client.post(
            "/api/plans/demo/stories/S1/patch",
            json={"backend": backend},
        )
        assert res.status_code == 200, f"backend {backend!r} should be accepted"

    assert set(seen) == _VALID_BACKENDS


def test_patch_story_backend_none_is_accepted_as_noop(
    client, plan_dir, monkeypatch
):
    """backend defaults to None on the body model; an explicit null must be
    forwarded (or dropped) without erroring — it must NOT be a 400."""
    _write_manifest(plan_dir, "demo", _bare_manifest(stories={"S1": _story()}))

    def fake_patch_story(plan_name, story_key, fields):
        return {"ok": True, "story_key": story_key, "story": {}}

    monkeypatch.setattr(d._service, "patch_story", fake_patch_story)

    res = client.post(
        "/api/plans/demo/stories/S1/patch",
        json={"backend": None},
    )
    assert res.status_code == 200


def test_patch_story_backend_400_does_not_mutate_manifest(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """(8) NEGATIVE: a 400 response must NOT have mutated any file — manifest
    byte-identical before/after."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(stories={"S1": _story(backend="claude")}),
    )
    before = _manifest_bytes(plan_dir, "demo")

    res = client.post(
        "/api/plans/demo/stories/S1/patch",
        json={"backend": "gibberish"},
    )
    assert res.status_code == 400
    assert _manifest_bytes(plan_dir, "demo") == before


def test_patch_story_empty_backend_string_returns_400(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """Boundary: an empty-string backend is not in the allowlist and must be
    rejected as 400 (not silently accepted)."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(stories={"S1": _story()}),
    )
    res = client.post(
        "/api/plans/demo/stories/S1/patch",
        json={"backend": ""},
    )
    assert res.status_code == 400


# =========================================================================== #
# Cross-cutting: 400 (not 500) on every validation failure
# =========================================================================== #
def test_no_write_endpoint_returns_500_on_validation_failure(
    client, plan_dir, seeded_registry, isolated_config_sources
):
    """Every write endpoint must surface validation failures as 400 (with the
    error message), NEVER 500. This is the headline contract: a typo must never
    produce an unhandled 500."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(stories={"S1": _story()}),
    )

    # role default - bad model
    r1 = client.post("/api/config/roles/review", json={"provider": "claude", "model": "fake"})
    assert r1.status_code != 500
    assert r1.status_code == 400

    # plan role config - bad model
    r2 = client.post(
        "/api/config/plans/demo/roles/dispatch",
        json={"provider": "claude", "model": "fake"},
    )
    assert r2.status_code != 500
    assert r2.status_code == 400

    # patch story - bad backend
    r3 = client.post("/api/plans/demo/stories/S1/patch", json={"backend": "fake"})
    assert r3.status_code != 500
    assert r3.status_code == 400


# --------------------------------------------------------------------------- #
# Helper for registry bytes
# --------------------------------------------------------------------------- #
def registry_path_bytes(seeded_registry):
    """Read the on-disk registry bytes from the env-overridden path.

    ``seeded_registry`` is the in-memory dict fixture; we re-resolve the path
    via role_registry._registry_path() so we always read the actual file the
    service wrote (or didn't)."""
    return role_registry._registry_path().read_bytes()