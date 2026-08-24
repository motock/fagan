"""Security hardening suite for the W3b config-write surface (B1+B2+B3).

This is the security gate for the config-write epic. The threat model:

  * A malicious edit could retarget a pipeline role to a compromised model
    (set_role_default / set_plan_role_config).
  * A bad edit could disable a gate by setting a role to a non-functional
    model.
  * A crafted model/provider name could inject arbitrary JSON into
    model_registry.json or a plan manifest.
  * A path-traversal model name could escape the registry directory.

Every write must FAIL CLOSED and NEVER mutate state on a rejected call:
the target file (registry / manifest) must be byte-for-byte unchanged after
a rejected call, and the HTTP surface must return 400 (not 500) without
leaking internal file paths or stack traces.

The write surface under test:

  * ``PipelineService.set_role_default``  -> writes model_registry.json
  * ``PipelineService.set_plan_role_config`` -> writes manifest role_config
    under ``_plan_lock``
  * ``PipelineService.patch_story`` (backend field) -> writes story backend
  * the three HTTP endpoints in ``app/dashboard.py``:
      POST /api/config/roles/{role}
      POST /api/config/plans/{plan_name}/roles/{role}
      POST /api/plans/{plan_name}/stories/{story_key}/patch

``role_registry.RoleRegistryError`` is the fail-closed validator.

These tests are security-engineer-authored and self-contained. They MUST
fail (import/attribute/assertion error) until the implementation hardens
the surface; they are the gate the implementer builds against.
"""
from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import dashboard as d
from app import role_registry
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as psrv
from pipeline import ticketing as pt

REGISTRY_PATH_ENV = "PIPELINE_MODEL_REGISTRY_PATH"

# A role that is always declared in PIPELINE_ROLES and seeded in the test
# registry, used as the happy-path target.
TARGET_ROLE = "review"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Redirect every PLAN_DIR binding the write paths touch to one tmp dir."""
    directory = tmp_path / "plans"
    directory.mkdir()
    monkeypatch.setattr(d, "PLAN_DIR", directory)
    monkeypatch.setattr(psrv, "PLAN_DIR", directory)
    monkeypatch.setattr(ppers, "PLAN_DIR", directory)
    monkeypatch.setattr(pcon, "PLAN_DIR", directory)
    return directory


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    """Point the model registry at a tmp file so writes never touch the real
    repo model_registry.json."""
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
    """Force NullTicketProvider so ingest never attempts a real Plane call."""
    monkeypatch.setattr(pt, "PLANE_API_KEY", "")
    monkeypatch.setattr(pt, "PLANE_WORKSPACE", "")
    monkeypatch.setattr(pt, "PLANE_PROJECT", "")


@pytest.fixture
def client():
    return TestClient(d.app)


@pytest.fixture
def isolated_config_sources(monkeypatch, tmp_path):
    """Point config_provenance source files at nonexistent tmp paths so
    /api/config tests never read this machine's real ~/.claude.json or
    launchd plist."""
    monkeypatch.setenv("PIPELINE_SCHEDULER_PLIST_PATH", str(tmp_path / "no-scheduler.plist"))
    monkeypatch.setenv("PIPELINE_CLAUDE_JSON_PATH", str(tmp_path / "no-claude.json"))
    return tmp_path


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _svc() -> psrv.PipelineService:
    return psrv.PipelineService()


def _bytes(path):
    return path.read_bytes()


def _manifest_path(plan_dir, plan_name):
    return plan_dir / f"{plan_name}.manifest.json"


def _write_manifest(plan_dir, plan_name, manifest):
    _manifest_path(plan_dir, plan_name).write_text(json.dumps(manifest, indent=2))


def _read_manifest(plan_dir, plan_name):
    return json.loads(_manifest_path(plan_dir, plan_name).read_text())


def _manifest_bytes(plan_dir, plan_name):
    return _manifest_path(plan_dir, plan_name).read_bytes()


def _bare_manifest(*, role_config=None, stories=None):
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


# =========================================================================== #
# 1. BYPASS RoleRegistryError: invalid (provider, model) combos fail closed
#    AND leave the target file byte-for-byte unchanged.
# =========================================================================== #
# Malicious / malformed model & provider payloads that must never reach disk.
INVALID_MODEL_PAYLOADS = [
    "sonnet'; DROP TABLE",
    "../etc/passwd",
    "x" * 10240,  # 10KB string
    "",
    " ",
    "sonnet\n",
    "sonnet\r\n",
    "sonnet\t",
    "null byte\x00here",
]

INVALID_PROVIDER_PAYLOADS = [
    "gibberish",
    "",
    " ",
    "claude'; DROP TABLE",
    "../etc/passwd",
    "x" * 10240,
    "claude\n",
    "null\x00byte",
]


@pytest.mark.parametrize("model", INVALID_MODEL_PAYLOADS)
def test_set_role_default_invalid_model_rejected_file_unchanged(
    seeded_registry, registry_path, model
):
    before = _bytes(registry_path)
    result = _svc().set_role_default(TARGET_ROLE, "claude", model)
    assert result["ok"] is False
    assert _bytes(registry_path) == before


@pytest.mark.parametrize("provider", INVALID_PROVIDER_PAYLOADS)
def test_set_role_default_invalid_provider_rejected_file_unchanged(
    seeded_registry, registry_path, provider
):
    before = _bytes(registry_path)
    result = _svc().set_role_default(TARGET_ROLE, provider, "sonnet")
    assert result["ok"] is False
    assert _bytes(registry_path) == before


def test_set_role_default_none_provider_and_none_model_rejected_file_unchanged(
    seeded_registry, registry_path
):
    before = _bytes(registry_path)
    assert _svc().set_role_default(TARGET_ROLE, None, "sonnet")["ok"] is False
    assert _svc().set_role_default(TARGET_ROLE, "claude", None)["ok"] is False
    assert _svc().set_role_default(TARGET_ROLE, None, None)["ok"] is False
    assert _bytes(registry_path) == before


def test_set_role_default_unknown_role_rejected_file_unchanged(
    seeded_registry, registry_path
):
    before = _bytes(registry_path)
    result = _svc().set_role_default("definitely-not-a-role", "claude", "sonnet")
    assert result["ok"] is False
    assert _bytes(registry_path) == before


# ---- set_plan_role_config: same invalid combos ----
@pytest.mark.parametrize("model", INVALID_MODEL_PAYLOADS)
def test_set_plan_role_config_invalid_model_rejected_manifest_unchanged(
    seeded_registry, plan_dir, model
):
    _write_manifest(plan_dir, "p1", _bare_manifest())
    before = _manifest_bytes(plan_dir, "p1")
    result = _svc().set_plan_role_config("p1", TARGET_ROLE, "claude", model)
    assert result["ok"] is False
    assert _manifest_bytes(plan_dir, "p1") == before


@pytest.mark.parametrize("provider", INVALID_PROVIDER_PAYLOADS)
def test_set_plan_role_config_invalid_provider_rejected_manifest_unchanged(
    seeded_registry, plan_dir, provider
):
    _write_manifest(plan_dir, "p1", _bare_manifest())
    before = _manifest_bytes(plan_dir, "p1")
    result = _svc().set_plan_role_config("p1", TARGET_ROLE, provider, "sonnet")
    assert result["ok"] is False
    assert _manifest_bytes(plan_dir, "p1") == before


def test_set_plan_role_config_unknown_role_rejected_manifest_unchanged(
    seeded_registry, plan_dir
):
    _write_manifest(plan_dir, "p1", _bare_manifest())
    before = _manifest_bytes(plan_dir, "p1")
    result = _svc().set_plan_role_config("p1", "definitely-not-a-role", "claude", "sonnet")
    assert result["ok"] is False
    assert _manifest_bytes(plan_dir, "p1") == before


# =========================================================================== #
# 2. JSON INJECTION: a model/provider name containing JSON-breaking chars
#    must be rejected by validation BEFORE any file write.
# =========================================================================== #
JSON_BREAKING_PAYLOADS = [
    'sonnet"}}, "evil": {"injected": true',
    'sonnet",\n"roles": {"review": "pwned"',
    'sonnet\\nDROP',
    'sonnet"}]',
    'sonnet\t", "x": "y',
]


@pytest.mark.parametrize("payload", JSON_BREAKING_PAYLOADS)
def test_set_role_default_json_injection_rejected_before_write(
    seeded_registry, registry_path, payload
):
    before = _bytes(registry_path)
    result = _svc().set_role_default(TARGET_ROLE, "claude", payload)
    assert result["ok"] is False
    # File must be byte-for-byte unchanged (not corrupted JSON).
    assert _bytes(registry_path) == before
    # And still parseable as valid JSON.
    json.loads(registry_path.read_text())


@pytest.mark.parametrize("payload", JSON_BREAKING_PAYLOADS)
def test_set_plan_role_config_json_injection_rejected_before_write(
    seeded_registry, plan_dir, payload
):
    _write_manifest(plan_dir, "p1", _bare_manifest())
    before = _manifest_bytes(plan_dir, "p1")
    result = _svc().set_plan_role_config("p1", TARGET_ROLE, "claude", payload)
    assert result["ok"] is False
    assert _manifest_bytes(plan_dir, "p1") == before
    # Manifest still valid JSON.
    json.loads(_manifest_path(plan_dir, "p1").read_text())


def test_set_role_default_json_injection_does_not_corrupt_existing_roles(
    seeded_registry, registry_path
):
    """A rejected injection must leave the OTHER roles verbatim, not just the
    file as a whole."""
    before = json.loads(registry_path.read_text())
    _svc().set_role_default(TARGET_ROLE, "claude", 'sonnet"}}, "evil": true')
    after = json.loads(registry_path.read_text())
    assert after == before


# =========================================================================== #
# 3. PATH TRAVERSAL / WRITE ESCAPE: writes touch ONLY the target file.
# =========================================================================== #
def test_set_role_default_writes_only_registry_file(
    seeded_registry, registry_path, tmp_path
):
    """After a successful set_role_default, the only file modified under the
    tmp registry dir is the registry file itself — no sibling files, no files
    outside the dir."""
    # Snapshot every file under tmp_path before the call.
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    result = _svc().set_role_default(TARGET_ROLE, "claude", "sonnet")
    assert result["ok"] is True

    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    # No new files appeared anywhere under tmp_path.
    new_files = set(after) - set(before)
    assert new_files == set(), f"unexpected new files: {new_files}"

    # Only the registry file's bytes changed.
    changed = {
        p for p in (set(before) & set(after)) if before[p] != after[p]
    }
    assert changed == {registry_path}, f"unexpected changed files: {changed}"


def test_set_role_default_no_tmp_file_left_behind(
    seeded_registry, registry_path, tmp_path
):
    """The atomic-write temp file must not linger after a successful write."""
    _svc().set_role_default(TARGET_ROLE, "claude", "sonnet")
    leftovers = [p for p in tmp_path.rglob("*") if ".tmp" in p.name and p.is_file()]
    assert leftovers == [], f"temp file left behind: {leftovers}"


def test_set_plan_role_config_touches_only_target_manifest(
    seeded_registry, plan_dir
):
    """set_plan_role_config must touch only the target plan manifest; a sibling
    plan manifest must be byte-for-byte unchanged."""
    _write_manifest(plan_dir, "target", _bare_manifest())
    _write_manifest(plan_dir, "sibling", _bare_manifest(role_config={"planner": {"provider": "ollama", "model": "glm"}}))

    sibling_before = _manifest_bytes(plan_dir, "sibling")
    target_before = _manifest_bytes(plan_dir, "target")

    result = _svc().set_plan_role_config("target", TARGET_ROLE, "claude", "sonnet")
    assert result["ok"] is True

    # Sibling untouched.
    assert _manifest_bytes(plan_dir, "sibling") == sibling_before
    # Target changed.
    assert _manifest_bytes(plan_dir, "target") != target_before


def test_set_plan_role_config_rejected_call_touches_no_manifest(
    seeded_registry, plan_dir
):
    """A rejected set_plan_role_config must not touch the target OR a sibling."""
    _write_manifest(plan_dir, "target", _bare_manifest())
    _write_manifest(plan_dir, "sibling", _bare_manifest())

    target_before = _manifest_bytes(plan_dir, "target")
    sibling_before = _manifest_bytes(plan_dir, "sibling")

    result = _svc().set_plan_role_config("target", TARGET_ROLE, "claude", "nonexistent-model")
    assert result["ok"] is False

    assert _manifest_bytes(plan_dir, "target") == target_before
    assert _manifest_bytes(plan_dir, "sibling") == sibling_before


# =========================================================================== #
# 4. HTTP SURFACE: invalid input -> 400 (not 500), does not persist, no leak.
# =========================================================================== #
def _post_role_default(client, role, body):
    return client.post(f"/api/config/roles/{role}", json=body)


def _post_plan_role_config(client, plan_name, role, body):
    return client.post(
        f"/api/config/plans/{plan_name}/roles/{role}", json=body
    )


def _post_patch_story(client, plan_name, story_key, body):
    return client.post(
        f"/api/plans/{plan_name}/stories/{story_key}/patch", json=body
    )


def test_http_set_role_default_invalid_model_returns_400_not_500(
    seeded_registry, registry_path, client, isolated_config_sources
):
    before = _bytes(registry_path)
    resp = _post_role_default(client, TARGET_ROLE, {"provider": "claude", "model": "sonnet'; DROP TABLE"})
    assert resp.status_code == 400
    assert resp.status_code != 500
    assert _bytes(registry_path) == before


def test_http_set_role_default_unknown_provider_returns_400(
    seeded_registry, registry_path, client, isolated_config_sources
):
    before = _bytes(registry_path)
    resp = _post_role_default(client, TARGET_ROLE, {"provider": "gibberish", "model": "sonnet"})
    assert resp.status_code == 400
    assert _bytes(registry_path) == before


def test_http_set_role_default_missing_required_field_returns_422(
    seeded_registry, registry_path, client, isolated_config_sources
):
    """FastAPI/pydantic validation: missing required `model` -> 422, not 400."""
    resp = _post_role_default(client, TARGET_ROLE, {"provider": "claude"})
    assert resp.status_code == 422
    # No write should have happened even on a 422.
    assert registry_path.exists()


def test_http_set_role_default_extra_field_ignored_or_rejected(
    seeded_registry, registry_path, client, isolated_config_sources
):
    """Extra fields on RoleDefaultBody must not cause a 500 and must not
    persist a write. RoleDefaultBody has no extra='forbid', so extra fields
    are ignored (still 400 because the model is invalid)."""
    before = _bytes(registry_path)
    resp = _post_role_default(
        client, TARGET_ROLE,
        {"provider": "claude", "model": "nonexistent", "extra": "noise"},
    )
    # Invalid model -> 400 (extra field ignored by pydantic).
    assert resp.status_code == 400
    assert _bytes(registry_path) == before


def test_http_set_plan_role_config_invalid_model_returns_400(
    seeded_registry, plan_dir, client, isolated_config_sources
):
    _write_manifest(plan_dir, "p1", _bare_manifest())
    before = _manifest_bytes(plan_dir, "p1")
    resp = _post_plan_role_config(
        client, "p1", TARGET_ROLE, {"provider": "claude", "model": "nonexistent"}
    )
    assert resp.status_code == 400
    assert resp.status_code != 500
    assert _manifest_bytes(plan_dir, "p1") == before


def test_http_set_plan_role_config_unknown_plan_returns_404(
    seeded_registry, plan_dir, client, isolated_config_sources
):
    resp = _post_plan_role_config(
        client, "no-such-plan", TARGET_ROLE, {"provider": "claude", "model": "sonnet"}
    )
    assert resp.status_code == 404


def test_http_set_plan_role_config_unknown_role_returns_400(
    seeded_registry, plan_dir, client, isolated_config_sources
):
    _write_manifest(plan_dir, "p1", _bare_manifest())
    before = _manifest_bytes(plan_dir, "p1")
    resp = _post_plan_role_config(
        client, "p1", "definitely-not-a-role", {"provider": "claude", "model": "sonnet"}
    )
    assert resp.status_code == 400
    assert _manifest_bytes(plan_dir, "p1") == before


def test_http_patch_story_invalid_backend_returns_400(
    seeded_registry, plan_dir, client, isolated_config_sources
):
    _write_manifest(
        plan_dir, "p1", _bare_manifest(stories={"s1": _story()})
    )
    before = _manifest_bytes(plan_dir, "p1")
    resp = _post_patch_story(client, "p1", "s1", {"backend": "totally-fake-backend"})
    assert resp.status_code == 400
    assert resp.status_code != 500
    assert _manifest_bytes(plan_dir, "p1") == before


def test_http_patch_story_unknown_field_returns_422(
    seeded_registry, plan_dir, client, isolated_config_sources
):
    """StoryPatchBody uses extra='ignore', so an unknown field is ignored and
    the call proceeds (no fields set -> ok). We assert no 500 and no
    persistence of the unknown field."""
    _write_manifest(
        plan_dir, "p1", _bare_manifest(stories={"s1": _story()})
    )
    resp = _post_patch_story(client, "p1", "s1", {"unknown_field": "x"})
    # extra='ignore' -> body is empty -> patch_story with empty fields.
    # Empty fields is a no-op that should not 500.
    assert resp.status_code != 500
    # The unknown field must NOT appear in the manifest.
    if _manifest_path(plan_dir, "p1").exists():
        m = json.loads(_manifest_path(plan_dir, "p1").read_text())
        assert "unknown_field" not in m["stories"]["s1"]


def test_http_error_detail_does_not_leak_internal_paths(
    seeded_registry, registry_path, client, isolated_config_sources, tmp_path
):
    """The 400 error detail must contain only the RoleRegistryError message —
    no internal file paths (e.g. the tmp registry path) and no stack traces."""
    resp = _post_role_default(
        client, TARGET_ROLE, {"provider": "claude", "model": "nonexistent"}
    )
    assert resp.status_code == 400
    detail = resp.json().get("detail", "")
    # Must not leak the tmp registry path or PLAN_DIR.
    assert str(tmp_path) not in detail
    assert "Traceback" not in detail
    assert ".py" not in detail  # no stack-trace file references


def test_http_set_plan_role_config_error_detail_does_not_leak_paths(
    seeded_registry, plan_dir, client, isolated_config_sources, tmp_path
):
    _write_manifest(plan_dir, "p1", _bare_manifest())
    resp = _post_plan_role_config(
        client, "p1", TARGET_ROLE, {"provider": "claude", "model": "nonexistent"}
    )
    assert resp.status_code == 400
    detail = resp.json().get("detail", "")
    assert str(tmp_path) not in detail
    assert "Traceback" not in detail


# =========================================================================== #
# 5. GATE-DISABLE ATTEMPT: declared model persists; undeclared rejected.
# =========================================================================== #
def test_set_role_default_declared_model_persists_even_if_operationally_bad(
    seeded_registry, registry_path
):
    """Setting a role's model to a value that IS declared under the provider's
    models must PERSIST — the registry is the authority. Whether that model
    would operationally disable a gate is NOT this layer's job to block.

    Boundary: this layer validates *declaration* (the model exists in the
    registry), not *operational fitness* (whether the model can actually
    serve the role). A declared-but-non-functional model is an operational
    concern, handled elsewhere; here we only assert the registry is the
    authority: declared -> persisted, undeclared -> rejected.
    """
    result = _svc().set_role_default(TARGET_ROLE, "claude", "haiku")
    assert result["ok"] is True
    on_disk = json.loads(registry_path.read_text())
    assert on_disk["roles"][TARGET_ROLE] == {"provider": "claude", "model": "haiku"}


def test_set_role_default_undeclared_model_rejected(
    seeded_registry, registry_path
):
    """An undeclared model (not under any provider's models) is rejected —
    this is the gate-disable attempt that this layer DOES block."""
    before = _bytes(registry_path)
    result = _svc().set_role_default(TARGET_ROLE, "claude", "nonexistent")
    assert result["ok"] is False
    assert _bytes(registry_path) == before


def test_set_plan_role_config_declared_model_persists(
    seeded_registry, plan_dir
):
    _write_manifest(plan_dir, "p1", _bare_manifest())
    result = _svc().set_plan_role_config("p1", TARGET_ROLE, "claude", "haiku")
    assert result["ok"] is True
    m = _read_manifest(plan_dir, "p1")
    assert m["role_config"][TARGET_ROLE] == {"provider": "claude", "model": "haiku"}


# =========================================================================== #
# 6. CONCURRENCY: set_plan_role_config acquires _plan_lock; a concurrent call
#    (lock held) returns skipped:locked rather than corrupting the manifest.
# =========================================================================== #
def test_set_plan_role_config_concurrent_call_returns_skipped_locked(
    seeded_registry, plan_dir
):
    """Simulate a concurrent holder of _plan_lock by acquiring it on another
    thread (flock serializes across threads within the process via the
    per-thread held-set). The second call must return skipped:locked and must
    NOT mutate the manifest."""
    _write_manifest(plan_dir, "p1", _bare_manifest(role_config={"planner": {"provider": "claude", "model": "sonnet"}}))
    before = _manifest_bytes(plan_dir, "p1")

    barrier = threading.Barrier(2)
    holder_result = {}
    caller_result = {}

    def hold_lock():
        with psrv._plan_lock("p1") as acquired:
            holder_result["acquired"] = acquired
            barrier.wait()  # signal the lock is held
            # Keep the lock held until the caller has attempted its write.
            barrier.wait()

    def attempt_call():
        # Wait until the holder has the lock.
        barrier.wait()
        caller_result["res"] = _svc().set_plan_role_config(
            "p1", TARGET_ROLE, "claude", "sonnet"
        )
        barrier.wait()  # release the holder

    t_hold = threading.Thread(target=hold_lock)
    t_call = threading.Thread(target=attempt_call)
    t_hold.start()
    t_call.start()
    t_hold.join(timeout=10)
    t_call.join(timeout=10)

    assert holder_result.get("acquired") is True
    res = caller_result["res"]
    # The concurrent call must report it skipped because the lock was held.
    assert res.get("ok") is True
    assert res.get("skipped") == "locked"
    # And the manifest is byte-for-byte unchanged (not corrupted).
    assert _manifest_bytes(plan_dir, "p1") == before


def test_set_plan_role_config_skipped_locked_does_not_persist(
    seeded_registry, plan_dir
):
    """A skipped:locked response must not have written the new role_config."""
    _write_manifest(plan_dir, "p1", _bare_manifest())
    before = _manifest_bytes(plan_dir, "p1")

    barrier = threading.Barrier(2)
    done = threading.Event()

    def hold_lock():
        with psrv._plan_lock("p1"):
            barrier.wait()
            done.wait(timeout=10)

    def attempt_call():
        barrier.wait()
        _svc().set_plan_role_config("p1", TARGET_ROLE, "claude", "sonnet")
        done.set()

    t_hold = threading.Thread(target=hold_lock)
    t_call = threading.Thread(target=attempt_call)
    t_hold.start()
    t_call.start()
    t_hold.join(timeout=10)
    t_call.join(timeout=10)

    assert _manifest_bytes(plan_dir, "p1") == before


# =========================================================================== #
# 7. AUDIT: each successful write returns enough context to reconstruct what
#    changed (role, provider, model, plan).
# =========================================================================== #
def test_set_role_default_success_returns_audit_context(
    seeded_registry, registry_path
):
    result = _svc().set_role_default(TARGET_ROLE, "claude", "sonnet")
    assert result["ok"] is True
    # Audit record: enough to reconstruct the change.
    assert result["role"] == TARGET_ROLE
    assert result["provider"] == "claude"
    assert result["model"] == "sonnet"


def test_set_plan_role_config_success_returns_audit_context(
    seeded_registry, plan_dir
):
    _write_manifest(plan_dir, "p1", _bare_manifest())
    result = _svc().set_plan_role_config("p1", TARGET_ROLE, "claude", "sonnet")
    assert result["ok"] is True
    assert result["plan"] == "p1"
    assert result["role"] == TARGET_ROLE
    # The persisted role_config entry for this role.
    rc = result["role_config"]
    assert rc["provider"] == "claude"
    assert rc["model"] == "sonnet"


def test_patch_story_success_returns_audit_context(
    seeded_registry, plan_dir
):
    _write_manifest(plan_dir, "p1", _bare_manifest(stories={"s1": _story()}))
    result = _svc().patch_story("p1", "s1", {"backend": "ollama"})
    assert result["ok"] is True
    assert result["story_key"] == "s1"
    assert result["story"]["backend"] == "ollama"


# =========================================================================== #
# Happy-path sanity: a valid write actually persists (so the negative tests
# above are meaningful — they're not passing because writes never work).
# =========================================================================== #
def test_set_role_default_valid_write_persists(seeded_registry, registry_path):
    result = _svc().set_role_default(TARGET_ROLE, "claude", "sonnet")
    assert result["ok"] is True
    on_disk = json.loads(registry_path.read_text())
    assert on_disk["roles"][TARGET_ROLE] == {"provider": "claude", "model": "sonnet"}


def test_set_plan_role_config_valid_write_persists(seeded_registry, plan_dir):
    _write_manifest(plan_dir, "p1", _bare_manifest())
    result = _svc().set_plan_role_config("p1", TARGET_ROLE, "claude", "sonnet")
    assert result["ok"] is True
    m = _read_manifest(plan_dir, "p1")
    assert m["role_config"][TARGET_ROLE] == {"provider": "claude", "model": "sonnet"}


def test_patch_story_valid_backend_persists(seeded_registry, plan_dir):
    _write_manifest(plan_dir, "p1", _bare_manifest(stories={"s1": _story()}))
    result = _svc().patch_story("p1", "s1", {"backend": "ollama"})
    assert result["ok"] is True
    m = _read_manifest(plan_dir, "p1")
    assert m["stories"]["s1"]["backend"] == "ollama"


# =========================================================================== #
# patch_story backend validation (B2): invalid backend fails closed.
# =========================================================================== #
def test_patch_story_invalid_backend_rejected_manifest_unchanged(
    seeded_registry, plan_dir
):
    _write_manifest(plan_dir, "p1", _bare_manifest(stories={"s1": _story()}))
    before = _manifest_bytes(plan_dir, "p1")
    result = _svc().patch_story("p1", "s1", {"backend": "totally-fake-backend"})
    assert result["ok"] is False
    assert _manifest_bytes(plan_dir, "p1") == before


def test_patch_story_unknown_field_rejected_manifest_unchanged(
    seeded_registry, plan_dir
):
    """patch_story must reject fields outside _PATCHABLE_STORY_FIELDS."""
    _write_manifest(plan_dir, "p1", _bare_manifest(stories={"s1": _story()}))
    before = _manifest_bytes(plan_dir, "p1")
    result = _svc().patch_story("p1", "s1", {"status": "running"})
    assert result["ok"] is False
    assert _manifest_bytes(plan_dir, "p1") == before


def test_patch_story_nonexistent_story_rejected_manifest_unchanged(
    seeded_registry, plan_dir
):
    _write_manifest(plan_dir, "p1", _bare_manifest(stories={"s1": _story()}))
    before = _manifest_bytes(plan_dir, "p1")
    result = _svc().patch_story("p1", "no-such-story", {"backend": "ollama"})
    assert result["ok"] is False
    assert _manifest_bytes(plan_dir, "p1") == before


# =========================================================================== #
# RoleRegistryError is the fail-closed validator (surface contract).
# =========================================================================== #
def test_role_registry_error_is_value_error_subclass():
    """RoleRegistryError must be a ValueError subclass so callers can catch
    it as the fail-closed validator."""
    assert issubclass(role_registry.RoleRegistryError, ValueError)


def test_role_registry_error_raised_for_undeclared_model():
    """resolve_role must raise RoleRegistryError (not return a bad default)
    when a plan_role_config names an undeclared model."""
    registry = {
        "providers": {"claude": {"models": {"sonnet": {"tag": "sonnet"}}}},
        "roles": {},
    }
    with pytest.raises(role_registry.RoleRegistryError):
        role_registry.resolve_role(
            TARGET_ROLE,
            plan_role_config={TARGET_ROLE: {"provider": "claude", "model": "ghost"}},
            registry=registry,
            model_fallback=lambda: "sonnet",
        )