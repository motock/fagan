"""Config WRITE surface for the two remaining override layers.

This story (B2) adds two write paths to ``PipelineService`` in
``pipeline/server.py``:

  1. ``set_plan_role_config(plan_name, role, provider=None, model=None)``
     writes a per-plan ``role_config`` block into the manifest. It validates
     the NEW role_config against ``role_registry.resolve_role`` BEFORE saving
     (a typo'd/undeclared model must never reach disk), writes atomically via
     ``_store.transaction`` (so it's atomic w.r.t. the scheduler's 60s tick),
     and takes effect IMMEDIATELY — no restart — because
     ``get_effective_config``/``_plan_role_config`` read the manifest fresh
     on every call.

  2. ``patch_story`` gains a ``backend`` field: ``backend`` is added to
     ``_PATCHABLE_STORY_FIELDS`` and validated against the documented
     allowlist ``{claude, local, ollama, lmstudio, mlx, auto}`` BEFORE
     ``_store.update_story``; an invalid value fails closed
     (``{ok: False, error: ...}``) and leaves the manifest unchanged.

These tests are self-contained (tmp ``PLAN_DIR`` + a tmp model registry) and
must fail for the right reason — ``AttributeError``/``AssertionError`` because
``set_plan_role_config`` does not exist yet and ``backend`` is not a patchable
field — until the implementation lands in a later dispatch on this branch.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from pipeline import server as srv
from pipeline.server import PipelineService

REGISTRY_PATH_ENV = "PIPELINE_MODEL_REGISTRY_PATH"

# The documented allowlist of valid story `backend` values
# (see pipeline-story-schema.md). Kept here as a literal so the test pins the
# exact set the implementation must accept, independent of where the
# implementation stores it.
_VALID_BACKENDS = frozenset({"claude", "local", "ollama", "lmstudio", "mlx", "auto"})


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """A tmp PLAN_DIR patched into server, persistence, and concurrency."""
    from pipeline import concurrency as pcon
    from pipeline import persistence as ppers

    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(srv, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
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


def _svc() -> PipelineService:
    return PipelineService()


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


# =========================================================================== #
# set_plan_role_config
# =========================================================================== #
def test_set_plan_role_config_persists_and_reflects_immediately(
    plan_dir, seeded_registry
):
    """(1) A valid per-plan override persists to the manifest and is reflected
    in get_effective_config(plan=...) IMMEDIATELY (no restart)."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(role_config={"review": {"provider": "ollama", "model": "glm"}}),
    )

    result = _svc().set_plan_role_config("demo", "review", provider="claude", model="sonnet")

    assert result["ok"] is True
    assert result["plan"] == "demo"
    assert result["role"] == "review"
    assert result["role_config"] == {"provider": "claude", "model": "sonnet"}

    # Persisted into the manifest's role_config block.
    manifest = _read_manifest(plan_dir, "demo")
    assert manifest["role_config"]["review"] == {"provider": "claude", "model": "sonnet"}

    # get_effective_config reflects the override immediately.
    eff = _svc().get_effective_config(plan_name="demo")
    roles = {r["role"]: r for r in eff["roles"]}
    assert roles["review"]["provider"] == "claude"
    assert roles["review"]["model"] == "sonnet"


def test_set_plan_role_config_invalid_model_rejected_manifest_unchanged(
    plan_dir, seeded_registry
):
    """(2) An undeclared model is rejected with ok:False carrying the
    RoleRegistryError message, and the manifest is byte-for-byte unchanged."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(role_config={"review": {"provider": "ollama", "model": "glm"}}),
    )
    before = _manifest_bytes(plan_dir, "demo")

    result = _svc().set_plan_role_config("demo", "review", provider="claude", model="nonexistent")

    assert result["ok"] is False
    assert "error" in result
    # The error must come from the RoleRegistryError path (names the bad model).
    assert "nonexistent" in result["error"]
    # Manifest untouched.
    assert _manifest_bytes(plan_dir, "demo") == before


def test_set_plan_role_config_setting_only_provider_persists_partial_entry(
    plan_dir, seeded_registry
):
    """(3) Setting only provider (model=None) persists a PARTIAL role_config
    entry containing just the provider key."""
    _write_manifest(plan_dir, "demo", _bare_manifest())

    result = _svc().set_plan_role_config("demo", "review", provider="ollama", model=None)

    assert result["ok"] is True
    assert result["role_config"] == {"provider": "ollama"}

    manifest = _read_manifest(plan_dir, "demo")
    assert manifest["role_config"]["review"] == {"provider": "ollama"}
    # No model key leaked into a partial entry.
    assert "model" not in manifest["role_config"]["review"]


def test_set_plan_role_config_preserves_other_role_entries(
    plan_dir, seeded_registry
):
    """Writing one role's override must preserve the other roles already in the
    role_config block verbatim."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(
            role_config={
                "review": {"provider": "ollama", "model": "glm"},
                "planner": {"provider": "claude", "model": "sonnet"},
            }
        ),
    )

    _svc().set_plan_role_config("demo", "review", provider="claude", model="sonnet")

    manifest = _read_manifest(plan_dir, "demo")
    assert manifest["role_config"]["planner"] == {"provider": "claude", "model": "sonnet"}
    assert manifest["role_config"]["review"] == {"provider": "claude", "model": "sonnet"}


def test_set_plan_role_config_nonexistent_plan_rejected(plan_dir, seeded_registry):
    """(7) set_plan_role_config on a non-existent plan returns ok:False."""
    result = _svc().set_plan_role_config("no-such-plan", "review", provider="claude", model="sonnet")
    assert result["ok"] is False
    assert "error" in result
    # No manifest file was created for the bogus plan.
    assert not _manifest_path(plan_dir, "no-such-plan").exists()


def test_set_plan_role_config_undeclared_model_does_not_mutate_manifest(
    plan_dir, seeded_registry
):
    """(6) NEGATIVE: a per-plan override resolving to an undeclared model does
    NOT mutate the manifest — the pre-save validation catches it and the
    existing role_config is preserved exactly."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(
            role_config={"review": {"provider": "ollama", "model": "glm"}}
        ),
    )
    before = _manifest_bytes(plan_dir, "demo")

    result = _svc().set_plan_role_config("demo", "review", provider="claude", model="totally-fake")

    assert result["ok"] is False
    # The pre-existing override is still the only thing on disk.
    assert _manifest_bytes(plan_dir, "demo") == before
    manifest = _read_manifest(plan_dir, "demo")
    assert manifest["role_config"] == {"review": {"provider": "ollama", "model": "glm"}}


def test_set_plan_role_config_uses_store_transaction(plan_dir, seeded_registry, monkeypatch):
    """Per-plan writes go through _store.transaction so they're atomic w.r.t.
    the scheduler's 60s tick. Assert the transaction context manager is
    actually entered for the plan."""
    _write_manifest(plan_dir, "demo", _bare_manifest())

    calls = []
    real_transaction = srv._store.transaction

    class _RecordingTxn:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            calls.append(self.name)
            return real_transaction(self.name).__enter__()

        def __exit__(self, *exc):
            return real_transaction(self.name).__exit__(*exc)

    def _wrapped(name):
        return _RecordingTxn(name)

    monkeypatch.setattr(srv._store, "transaction", _wrapped)

    result = _svc().set_plan_role_config("demo", "review", provider="claude", model="sonnet")

    assert result["ok"] is True
    assert calls == ["demo"]


def test_set_plan_role_config_skipped_when_locked(plan_dir, seeded_registry, monkeypatch):
    """If _store.transaction cannot be acquired (plan is locked by a tick),
    return {ok: True, skipped: 'locked'} without touching the manifest."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(role_config={"review": {"provider": "ollama", "model": "glm"}}),
    )
    before = _manifest_bytes(plan_dir, "demo")

    @contextmanager
    def _locked(name):
        yield False  # not acquired

    monkeypatch.setattr(srv._store, "transaction", _locked)

    result = _svc().set_plan_role_config("demo", "review", provider="claude", model="sonnet")

    assert result == {"ok": True, "skipped": "locked"}
    assert _manifest_bytes(plan_dir, "demo") == before


# =========================================================================== #
# patch_story: backend field
# =========================================================================== #
def test_patch_story_backend_is_patchable_field():
    """`backend` must be a member of _PATCHABLE_STORY_FIELDS so patch_story
    accepts it rather than rejecting it as an unknown field."""
    assert "backend" in srv._PATCHABLE_STORY_FIELDS


def test_patch_story_with_valid_backend_persists(plan_dir, seeded_registry):
    """(4) patch_story with backend=local persists the backend onto the story."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(stories={"S1": _story()}),
    )

    result = _svc().patch_story("demo", "S1", {"backend": "local"})

    assert result["ok"] is True
    assert result["story"]["backend"] == "local"
    manifest = _read_manifest(plan_dir, "demo")
    assert manifest["stories"]["S1"]["backend"] == "local"


@pytest.mark.parametrize("value", sorted(_VALID_BACKENDS))
def test_patch_story_accepts_every_documented_backend(plan_dir, seeded_registry, value):
    """Every value in the documented allowlist is accepted and persisted."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(stories={"S1": _story()}),
    )

    result = _svc().patch_story("demo", "S1", {"backend": value})

    assert result["ok"] is True
    assert result["story"]["backend"] == value


def test_patch_story_with_invalid_backend_rejected_story_unchanged(
    plan_dir, seeded_registry
):
    """(5) patch_story with backend=gibberish -> ok:False, story unchanged,
    manifest unchanged."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(stories={"S1": _story(backend="claude")}),
    )
    before = _manifest_bytes(plan_dir, "demo")

    result = _svc().patch_story("demo", "S1", {"backend": "gibberish"})

    assert result["ok"] is False
    assert "error" in result
    # The error names the bad value and references the allowlist.
    assert "gibberish" in result["error"]
    # Manifest untouched.
    assert _manifest_bytes(plan_dir, "demo") == before
    story = _read_manifest(plan_dir, "demo")["stories"]["S1"]
    assert story["backend"] == "claude"


def test_patch_story_invalid_backend_validates_before_update_story(
    plan_dir, seeded_registry, monkeypatch
):
    """An invalid backend must be rejected BEFORE _store.update_story is called
    (manifest unchanged). Patch update_story to blow up if it's reached, so a
    validation-after-write bug fails loudly instead of silently persisting."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(stories={"S1": _story()}),
    )

    def _boom(*a, **k):
        raise AssertionError("update_story must not be called for an invalid backend")

    monkeypatch.setattr(srv._store, "update_story", _boom)

    result = _svc().patch_story("demo", "S1", {"backend": "not-a-backend"})
    assert result["ok"] is False
    # The rejection must come from the NEW backend-validation path (which
    # names the allowlist), not the pre-implementation unknown-field path.
    assert "backend" in result["error"]
    assert "must be one of" in result["error"]


def test_patch_story_backend_empty_string_rejected(plan_dir, seeded_registry):
    """Boundary: an empty-string backend is not in the allowlist and must be
    rejected (not silently coerced to a default)."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(stories={"S1": _story(backend="claude")}),
    )
    before = _manifest_bytes(plan_dir, "demo")

    result = _svc().patch_story("demo", "S1", {"backend": ""})

    assert result["ok"] is False
    # Rejected by the backend allowlist (naming the allowlist), not the
    # unknown-field path.
    assert "backend" in result["error"]
    assert "must be one of" in result["error"]
    assert _manifest_bytes(plan_dir, "demo") == before


def test_patch_story_backend_none_is_noop_or_rejected_not_crash(
    plan_dir, seeded_registry
):
    """Boundary: backend=None must not crash and must not persist a None
    backend. Either it's rejected (ok:False) or treated as 'no change'; in
    neither case may a literal None land on the story.

    This only becomes meaningful once `backend` is a patchable field; before
    that it's rejected by the unknown-field path, so we additionally assert
    the field is accepted (i.e. the rejection, if any, is NOT the
    unknown-field rejection)."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(stories={"S1": _story(backend="claude")}),
    )

    # `backend` must be a recognized patchable field for this boundary to be
    # exercised at all.
    assert "backend" in srv._PATCHABLE_STORY_FIELDS

    result = _svc().patch_story("demo", "S1", {"backend": None})

    # If accepted, the story's backend must remain a valid string (not None).
    if result["ok"]:
        story = _read_manifest(plan_dir, "demo")["stories"]["S1"]
        assert story.get("backend") in _VALID_BACKENDS or "backend" not in story
    else:
        assert "error" in result


def test_patch_story_backend_alongside_other_patchable_field(
    plan_dir, seeded_registry
):
    """backend can be patched in the same call as another patchable field and
    both persist."""
    _write_manifest(
        plan_dir,
        "demo",
        _bare_manifest(stories={"S1": _story()}),
    )

    result = _svc().patch_story("demo", "S1", {"backend": "ollama", "risk": "high"})

    assert result["ok"] is True
    assert result["story"]["backend"] == "ollama"
    assert result["story"]["risk"] == "high"