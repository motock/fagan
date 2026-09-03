"""Tests for ``PipelineService.decompose_plan`` gaining an optional ``workspace``
parameter that server-validates and injects ``repo_root`` (mirrors the WS-11
pattern already applied to ``save_plan`` -- see
``tests/unit/test_service_save_plan_repo_root.py``).

Scope (see the dispatch brief): ``pipeline/service.py``'s ``decompose_plan``
method only. The signature becomes
``decompose_plan(self, request: str, workspace: str | None = None) -> dict[str, Any]``.
When ``workspace`` is given, it must be validated via
``pipeline.workspace.validate_workspace`` (imported in service.py already, see
``save_plan``) AFTER the existing epics-shape check and BEFORE the final
success return:

* ``workspace is None`` -> today's behaviour, byte-identical (existing callers
  unaffected).
* valid workspace -> ``plan["repo_root"]`` is overwritten/injected with the
  VALIDATED RESOLVED path (never the raw, possibly-untrusted input).
* invalid workspace -> fail CLOSED: ``{"ok": False, "error": <sanitized>}``
  with NO ``"plan"`` key at all (never hand back a draft whose ``repo_root``
  was authored by the model).
* the backend-output / malformed-JSON / missing-epics failure paths return
  before ``validate_workspace`` is ever called, workspace or no.

``pipeline.service.validate_workspace`` is stubbed at its real integration
point (module-level name, per ``test_service_save_plan_repo_root.py``'s
established convention) with the real dict contract
``{"ok": bool, "path": str, "error": str | None}`` that never raises -- the
real backend (LLM decompose call, real workspace filesystem/git checks) is
never exercised here.

None of these tests exist to change ``tests/unit/test_decompose_plan_migration.py``
-- that file pins the free-variable ``_run_decompose(request)`` call shape and
must stay green, unmodified, throughout.

Every test here is expected to fail RED against the current implementation:
``PipelineService.decompose_plan`` does not yet accept a ``workspace`` keyword,
so any test passing one fails with a ``TypeError`` (unexpected keyword
argument) -- the correct RED reason, not a bug in this file's logic.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from pipeline import server as p

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _method():
    return getattr(p.PipelineService, "decompose_plan", None)


def _patch_run_decompose(monkeypatch, return_value):
    """Stub the free-variable ``_run_decompose`` (mirrors
    test_decompose_plan_migration.py's ``_patch_run_decompose``)."""
    monkeypatch.setattr(p, "_run_decompose", lambda request, **k: return_value)


def _install_fake_validate_workspace(monkeypatch, *, valid_path=None, calls=None):
    """Stub ``pipeline.service.validate_workspace`` with the real dict
    convention (never raises), routed by substring match on the input --
    mirrors ``test_service_save_plan_repo_root.py``'s ``_install_fake_validate``.

    ``calls`` (optional list) records every raw argument passed in, so tests
    can assert the function was/was not invoked -- used to prove the
    short-circuit ordering (validation must not run before the epics check).
    """

    def fake(raw):
        if calls is not None:
            calls.append(raw)
        if raw is None:
            raise AssertionError(
                "validate_workspace must never be called with workspace=None"
            )
        if "etc" in raw or "denied" in raw:
            return {"ok": False, "path": "", "error": "workspace is not allowed"}
        if "nonexistent" in raw:
            return {"ok": False, "path": "", "error": "path does not exist"}
        return {"ok": True, "path": valid_path or raw, "error": None}

    monkeypatch.setattr("pipeline.service.validate_workspace", fake, raising=False)
    return fake


def _plan_json(**extra):
    import json

    body: dict[str, Any] = {"epics": [{"summary": "E1", "stories": []}]}
    body.update(extra)
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------


def test_signature_gains_workspace_param_defaulting_to_none():
    method = _method()
    assert method is not None, "PipelineService.decompose_plan does not exist"
    sig = inspect.signature(method)
    params = list(sig.parameters.keys())
    assert params == ["self", "request", "workspace"], (
        f"expected (self, request, workspace), got {params}"
    )
    ws_param = sig.parameters["workspace"]
    assert ws_param.default is None, "workspace must default to None"
    ann = ws_param.annotation
    assert ann is not inspect.Signature.empty, "workspace must be annotated"
    assert str(ann) in ("str | None", "typing.Optional[str]"), (
        f"workspace annotation changed: {ann!r}"
    )


def test_return_annotation_unchanged():
    method = _method()
    assert method is not None
    sig = inspect.signature(method)
    assert str(sig.return_annotation) == "dict[str, typing.Any]"


def test_callable_with_only_request_positional_arg_binds_workspace_none():
    """Regression: the server.py tool wrapper calls
    ``_service.decompose_plan(request)`` with exactly one positional arg
    (pinned by test_decompose_plan_migration.py) -- this must keep working,
    with workspace implicitly defaulting to None."""
    sig = inspect.signature(_method())
    bound = sig.bind(object(), "some request")
    bound.apply_defaults()
    assert bound.arguments["workspace"] is None


def test_callable_with_only_request_positional_arg_end_to_end(monkeypatch):
    """Same regression, exercised through an actual call (not just signature
    binding) to prove no TypeError is raised in practice."""
    plan_json = _plan_json()
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is True


# ---------------------------------------------------------------------------
# workspace=None / omitted -> byte-identical passthrough (regression)
# ---------------------------------------------------------------------------


def test_workspace_omitted_returns_plan_with_repo_root_untouched(monkeypatch):
    plan_json = _plan_json(repo_root="/model/authored/path")
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is True
    assert result["plan"]["repo_root"] == "/model/authored/path"


def test_workspace_omitted_returns_plan_without_repo_root_key(monkeypatch):
    """Boundary: when the parsed plan has no repo_root at all, omitting
    workspace must not inject one (today's behaviour, unchanged)."""
    plan_json = _plan_json()
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.")

    assert result["ok"] is True
    assert "repo_root" not in result["plan"]


def test_workspace_explicit_none_matches_omitted_behaviour(monkeypatch):
    plan_json = _plan_json(repo_root="/model/authored/path")
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace=None)

    assert result["ok"] is True
    assert result["plan"]["repo_root"] == "/model/authored/path"


def test_workspace_omitted_result_has_exactly_ok_and_plan_keys(monkeypatch):
    plan_json = _plan_json()
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.")

    assert set(result.keys()) == {"ok", "plan"}


# ---------------------------------------------------------------------------
# valid workspace -> repo_root overwritten/injected with the RESOLVED path
# ---------------------------------------------------------------------------


def test_valid_workspace_injects_repo_root_when_absent(monkeypatch, tmp_path):
    ws_valid = str(tmp_path / "real_repo")
    _install_fake_validate_workspace(monkeypatch, valid_path=ws_valid)
    plan_json = _plan_json()  # boundary: empty stories list, no repo_root
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace=ws_valid)

    assert result["ok"] is True
    assert result["plan"]["repo_root"] == ws_valid


def test_valid_workspace_overwrites_hallucinated_repo_root(monkeypatch, tmp_path):
    ws_valid = str(tmp_path / "real_repo")
    _install_fake_validate_workspace(monkeypatch, valid_path=ws_valid)
    plan_json = _plan_json(repo_root="/model/hallucinated")
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace=ws_valid)

    assert result["ok"] is True
    assert result["plan"]["repo_root"] == ws_valid
    assert result["plan"]["repo_root"] != "/model/hallucinated"


def test_valid_workspace_injects_resolved_path_not_raw_input(monkeypatch, tmp_path):
    """The RESOLVED path from validate_workspace is used, never the raw,
    possibly-untrusted input string (mirrors save_plan's WS-11 contract)."""
    ws_valid = str(tmp_path / "real_repo")
    _install_fake_validate_workspace(monkeypatch, valid_path=ws_valid)
    raw = ws_valid + "/../real_repo"
    assert "/.." in raw
    plan_json = _plan_json()
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace=raw)

    assert result["ok"] is True
    assert result["plan"]["repo_root"] == ws_valid
    assert "/.." not in result["plan"]["repo_root"]


def test_valid_workspace_result_has_exactly_ok_and_plan_keys(monkeypatch, tmp_path):
    ws_valid = str(tmp_path / "real_repo")
    _install_fake_validate_workspace(monkeypatch, valid_path=ws_valid)
    plan_json = _plan_json()
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace=ws_valid)

    assert set(result.keys()) == {"ok", "plan"}


# ---------------------------------------------------------------------------
# invalid workspace -> fail CLOSED: ok=False, sanitized error, NO 'plan' key
# ---------------------------------------------------------------------------


def test_denylisted_workspace_returns_ok_false(monkeypatch):
    _install_fake_validate_workspace(monkeypatch)
    plan_json = _plan_json(repo_root="/model/hallucinated")
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace="/etc/x")

    assert result["ok"] is False


def test_denylisted_workspace_response_has_no_plan_key(monkeypatch):
    """Fail closed: never hand back a draft whose repo_root was
    model-authored -- the 'plan' key must be entirely absent, not just
    falsy, on a rejected workspace."""
    _install_fake_validate_workspace(monkeypatch)
    plan_json = _plan_json(repo_root="/model/hallucinated")
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace="/etc/x")

    assert "plan" not in result


def test_denylisted_workspace_error_is_sanitized_passthrough(monkeypatch):
    _install_fake_validate_workspace(monkeypatch)
    plan_json = _plan_json()
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace="/etc/denied")

    assert result["error"] == "workspace is not allowed"
    assert "/" not in result["error"], (
        "error must be the sanitized message, not leak a resolved absolute path"
    )
    assert "/etc/denied" not in result["error"]


def test_denylisted_workspace_response_has_exactly_ok_and_error_keys(monkeypatch):
    _install_fake_validate_workspace(monkeypatch)
    plan_json = _plan_json()
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace="/etc/x")

    assert set(result.keys()) == {"ok", "error"}


def test_nonexistent_workspace_returns_ok_false(monkeypatch):
    _install_fake_validate_workspace(monkeypatch)
    plan_json = _plan_json()
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan(
        "Build a CLI todo app.", workspace="/nonexistent/xyz"
    )

    assert result["ok"] is False
    assert "plan" not in result
    assert result["error"] == "path does not exist"


# ---------------------------------------------------------------------------
# ordering: validate_workspace must run AFTER the epics-shape check, and must
# never be invoked when an earlier failure path already short-circuits.
# ---------------------------------------------------------------------------


def test_workspace_not_validated_when_backend_returns_no_output(monkeypatch):
    calls: list = []
    _install_fake_validate_workspace(monkeypatch, calls=calls)
    _patch_run_decompose(monkeypatch, None)

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace="/etc/x")

    assert result["ok"] is False
    assert result["error"] == "decompose backend returned no output"
    assert calls == [], "validate_workspace must not run when the backend yields no text"


def test_workspace_not_validated_on_malformed_json(monkeypatch):
    calls: list = []
    _install_fake_validate_workspace(monkeypatch, calls=calls)
    _patch_run_decompose(monkeypatch, "not json at all")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace="/etc/x")

    assert result["ok"] is False
    assert "raw" in result
    assert calls == [], "validate_workspace must not run on malformed backend JSON"


def test_workspace_not_validated_when_epics_list_missing(monkeypatch):
    calls: list = []
    _install_fake_validate_workspace(monkeypatch, calls=calls)
    _patch_run_decompose(monkeypatch, '{"not_epics": []}')

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace="/etc/x")

    assert result["ok"] is False
    assert "epics" in result["error"]
    assert calls == [], "validate_workspace must not run when the epics list is missing"


def test_workspace_validated_exactly_once_on_success_path(monkeypatch, tmp_path):
    ws_valid = str(tmp_path / "real_repo")
    calls: list = []
    _install_fake_validate_workspace(monkeypatch, valid_path=ws_valid, calls=calls)
    plan_json = _plan_json()
    _patch_run_decompose(monkeypatch, f"```json\n{plan_json}\n```")

    svc = p.PipelineService()
    result = svc.decompose_plan("Build a CLI todo app.", workspace=ws_valid)

    assert result["ok"] is True
    assert calls == [ws_valid], "validate_workspace must be called exactly once, with the raw workspace input"
