"""AUTH-10: ``patch_plan`` is gated behind ``config:write``.

``patch_plan``'s only editable field is ``role_config``, the same field
``set_plan_role_config`` rewrites -- and ``set_plan_role_config`` gained a
``config:write`` guard in AUTH-08. Until now the identical change remained
reachable through ``patch_plan`` by any authenticated caller, so this tool now
asks ``pipeline.authorization.authorize`` for a decision before it mutates
anything (or even reads a manifest).

The contract these tests pin down:

* ``local-operator`` -- the identity the unattended scheduler and daemon run
  as -- still patches ``role_config``, so nothing about the automated paths
  changes;
* a denied principal gets the refusal dict unchanged and the manifest bytes on
  disk are identical before and after;
* each denial appends exactly one ``authorization_denied`` sidecar record
  naming ``config:write`` (``authorize`` writes it, the service must not add a
  second);
* ``PIPELINE_POLICY_ENFORCE=0`` is the operator's kill switch and is read per
  call, not cached at import;
* ``_validate_key`` runs FIRST, so a traversal is refused before ``authorize``
  reads a manifest or appends a sidecar record -- the same ordering
  ``_dispatch_story_impl`` uses;
* the pre-existing validation the move had to preserve survives: an unknown
  field is rejected fail-closed and a missing plan still reports
  ``No such plan``.

``plan_dir`` shadows the shared fixture from ``tests/unit/conftest.py`` with
the shape ``tests/unit/test_authorization.py`` uses: a per-test ``tmp_path``
with the ``pipeline.server``, ``pipeline.persistence`` and
``pipeline.concurrency`` PLAN_DIR bindings patched -- the bindings the store,
the manifest reads and the denial sidecar all resolve through.
"""

from __future__ import annotations

import json

import pytest

import pipeline.server as p
from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline.actor import ANONYMOUS, bind_principal
from pipeline.authorization import ENFORCE_ENV

PLAN = "demo"
DENIAL_EVENT = "authorization_denied"

#: The role config the tests write; a real provider/model pair is not needed
#: here because ``patch_plan`` only checks the field name, not its contents.
ROLE_CONFIG = {"review": {"provider": "claude", "model": "sonnet"}}


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point every PLAN_DIR binding at a throwaway directory."""
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d


def _manifest(**overrides):
    manifest = {
        "repo_root": "/tmp/repo",
        "version": 3,
        "role_config": {"default": "sonnet"},
        "stories": {},
    }
    manifest.update(overrides)
    return manifest


def _write_plan(plan_dir, **overrides):
    path = plan_dir / f"{PLAN}.manifest.json"
    path.write_text(json.dumps(_manifest(**overrides), indent=2))
    return path


def _patch(fields):
    return p.PipelineService().patch_plan(PLAN, fields)


def _denials(plan_dir, plan_name=PLAN):
    """Every ``authorization_denied`` record in the plan's JSONL sidecar."""
    path = plan_dir / f"{plan_name}.notifications.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in records if r.get("event") == DENIAL_EVENT]


def test_local_operator_still_patches_role_config(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    path = _write_plan(plan_dir)

    with bind_principal(None):
        assert _patch({"role_config": ROLE_CONFIG}) == {"ok": True, "plan_name": PLAN}

    assert json.loads(path.read_text())["role_config"] == ROLE_CONFIG


def test_denied_principal_is_refused_and_manifest_unchanged(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    path = _write_plan(plan_dir)
    before = path.read_bytes()

    with bind_principal(ANONYMOUS):
        result = _patch({"role_config": ROLE_CONFIG})

    assert result["ok"] is False and path.read_bytes() == before


def test_denial_appends_one_sidecar_record_naming_config_write(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    with bind_principal(ANONYMOUS):
        _patch({"role_config": ROLE_CONFIG})

    denials = _denials(plan_dir)
    assert len(denials) == 1 and "config:write" in json.dumps(denials[0])


def test_enforcement_disabled_allows_the_denied_principal(plan_dir, monkeypatch) -> None:
    monkeypatch.setenv(ENFORCE_ENV, "0")
    _write_plan(plan_dir)

    with bind_principal(ANONYMOUS):
        result = _patch({"role_config": ROLE_CONFIG})

    assert result == {"ok": True, "plan_name": PLAN}


def test_traversal_is_refused_before_authorize_runs(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    with (
        bind_principal(ANONYMOUS),
        pytest.raises(ValueError, match="invalid plan/story key"),
    ):
        p.PipelineService().patch_plan("../escape", {"role_config": ROLE_CONFIG})

    assert _denials(plan_dir, "../escape") == []


def test_unknown_field_is_still_rejected(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)
    _write_plan(plan_dir)

    with bind_principal(None):
        result = _patch({"repo_root": "/tmp/evil"})

    assert result["ok"] is False and "repo_root" in result["error"]


def test_missing_plan_still_reports_no_such_plan(plan_dir, monkeypatch) -> None:
    monkeypatch.delenv(ENFORCE_ENV, raising=False)

    with bind_principal(None):
        result = _patch({"role_config": ROLE_CONFIG})

    assert result == {"ok": False, "error": f"No such plan {PLAN!r}"}
