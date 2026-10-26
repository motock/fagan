"""Sizing must use a story's effective dispatch tier, not its own backend field.

`_story_sizing_warning` gates on ``story["backend"]``, so a story that inherits
the process-wide dispatch tier (registry ``roles.dispatch`` or
``PIPELINE_BACKEND_DISPATCH``) declared no provider and was never sized at all.
`_sizing_view` stamps the effective tier at the call site, leaving the sizing
function's signature and its ~40 direct-call tests untouched.

Repo rule (`.claude/rules/testing-config-gates.md`): test the resolution logic,
never today's configured values. Every test stubs
`role_registry.load_registry` with a synthetic payload and the autouse fixture
clears `PIPELINE_BACKEND_DISPATCH`, so neither the live model_registry.json nor
model_registry.local.json is ever consulted.
"""

from __future__ import annotations

import inspect

import pytest

from app import role_registry
from pipeline import ingest as ingest_mod
from pipeline.ingest import _sizing_view, _story_sizing_warning

THREE_PY_FILES = {"agent_instructions": "Touch `app/a.py`, `app/b.py` and `app/c.py`."}


@pytest.fixture(autouse=True)
def _isolate_dispatch_env(monkeypatch):
    """Keep a developer's exported PIPELINE_BACKEND_DISPATCH out of these tests."""
    monkeypatch.delenv("PIPELINE_BACKEND_DISPATCH", raising=False)


def _stub_registry(monkeypatch, payload):
    """Stub the registry seam with a synthetic payload (never the live file)."""
    monkeypatch.setattr(role_registry, "load_registry", lambda *a, **k: payload)


def test_inherited_registry_tier_makes_an_oversized_story_warn(monkeypatch, tmp_path):
    _stub_registry(
        monkeypatch, {"roles": {"dispatch": {"provider": "ollama", "model": "m"}}}
    )
    assert (
        _story_sizing_warning(_sizing_view(THREE_PY_FILES, None), str(tmp_path))
        is not None
    )


def test_no_dispatch_role_falls_through_to_claude_and_stays_silent(
    monkeypatch, tmp_path
):
    _stub_registry(monkeypatch, {"roles": {}})
    assert (
        _story_sizing_warning(_sizing_view(THREE_PY_FILES, None), str(tmp_path)) is None
    )


def test_explicit_claude_backend_is_returned_unchanged(monkeypatch):
    _stub_registry(
        monkeypatch, {"roles": {"dispatch": {"provider": "ollama", "model": "m"}}}
    )
    story = {**THREE_PY_FILES, "backend": "claude"}
    assert _sizing_view(story, None) is story


def test_explicit_claude_backend_never_warns_even_when_registry_says_ollama(
    monkeypatch, tmp_path
):
    _stub_registry(
        monkeypatch, {"roles": {"dispatch": {"provider": "ollama", "model": "m"}}}
    )
    story = {**THREE_PY_FILES, "backend": "claude"}
    assert _story_sizing_warning(_sizing_view(story, None), str(tmp_path)) is None


def test_case_and_whitespace_backend_is_returned_unchanged(monkeypatch):
    _stub_registry(
        monkeypatch, {"roles": {"dispatch": {"provider": "ollama", "model": "m"}}}
    )
    story = {**THREE_PY_FILES, "backend": "  OLLAMA "}
    assert _sizing_view(story, None) is story


def test_auto_env_tier_is_sized_as_a_weak_tier(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "auto")
    _stub_registry(monkeypatch, {"roles": {}})
    assert (
        _story_sizing_warning(_sizing_view(THREE_PY_FILES, None), str(tmp_path))
        is not None
    )


def test_claude_resolving_registry_keeps_auto_silent(monkeypatch, tmp_path):
    _stub_registry(
        monkeypatch, {"roles": {"dispatch": {"provider": "claude", "model": "m"}}}
    )
    assert (
        _story_sizing_warning(_sizing_view(THREE_PY_FILES, None), str(tmp_path)) is None
    )


def test_the_sizing_view_is_wired_into_ingest_plan():
    src = inspect.getsource(ingest_mod._ingest_plan_impl)
    assert "_sizing_view(story" in src
