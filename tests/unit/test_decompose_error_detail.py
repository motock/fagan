"""Decompose's swallowed backend error must reach the API response.

_run_decompose's fail-open (return None on ANY exception) is deliberate —
a broken/slow backend must never raise past the planner — but the chat
dashboard showed only "decompose backend returned no output" with zero
diagnostic value: a claude-CLI auth/cap error, a proxy model-not-found 404,
and a slow-but-working call all look identical to the user (observed live
2026-09-08: the glm env-strip/model failure surfaced as an empty message).

Contract introduced here: `_run_decompose_detailed(request) -> (text, error)`
carries the underlying cause (or None on success); `_run_decompose` keeps its
existing None-on-failure contract and delegates; PipelineService.decompose_plan
appends the cause to its error string.
"""

from __future__ import annotations

from pipeline import planner
from pipeline import service as service_module


def _stub_backend(monkeypatch, *, outcome):
    """Stub app.backend.get_backend so planner's complete() call raises or returns."""
    from app import backend

    class _FakeDriver:
        def complete(self, *args, **kwargs):
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    monkeypatch.setattr(backend, "get_backend", lambda role, *, name: _FakeDriver())


def test_run_decompose_detailed_returns_cause_when_backend_raises(monkeypatch):
    _stub_backend(
        monkeypatch,
        outcome=RuntimeError("claude CLI call failed (returncode=1): session limit"),
    )
    text, err = planner._run_decompose_detailed("goal")
    assert text is None
    assert err is not None
    assert "claude CLI call failed" in err


def test_run_decompose_detailed_returns_none_error_on_success(monkeypatch):
    _stub_backend(monkeypatch, outcome='{"epics": []}')
    text, err = planner._run_decompose_detailed("goal")
    assert text == '{"epics": []}'
    assert err is None


def test_run_decompose_keeps_none_contract_and_clears_error(monkeypatch):
    _stub_backend(monkeypatch, outcome=RuntimeError("boom"))
    assert planner._run_decompose("goal") is None
    _stub_backend(monkeypatch, outcome="ok text")
    assert planner._run_decompose("goal") == "ok text"
    # A later success must not inherit the earlier failure's error.
    assert planner._run_decompose_detailed("goal")[1] is None


def test_service_error_includes_backend_cause(monkeypatch):
    # Stub the service-level free var exactly the way the migration tests do.
    monkeypatch.setattr(
        service_module,
        "_run_decompose_detailed",
        lambda request, **k: (None, "RuntimeError: claude CLI call failed: no access"),
    )
    result = service_module.PipelineService().decompose_plan("Build a CLI todo app.")
    assert result["ok"] is False
    assert "decompose backend returned no output" in result["error"]
    assert "claude CLI call failed" in result["error"]


def test_service_error_stays_bare_when_no_cause_available(monkeypatch):
    # Backward-compat: a stubbed (None, None) backend must produce the exact
    # pre-existing message the existing suites assert on.
    monkeypatch.setattr(
        service_module, "_run_decompose_detailed", lambda request, **k: (None, None)
    )
    result = service_module.PipelineService().decompose_plan("Build a CLI todo app.")
    assert result["ok"] is False
    assert result["error"] == "decompose backend returned no output"
