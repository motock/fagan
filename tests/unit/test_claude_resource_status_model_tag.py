"""ClaudeCliDriver.resource_status(model_tag=...) — interface parity with the
Backend protocol.

pipeline/usage.py's review gate calls
backend.get_backend("review", name=...).resource_status(model_tag=resolution.model),
and the base protocol (app/backend.py) declares `model_tag`. OllamaDriver
implements it; ClaudeCliDriver did not, so any plan whose review resolves to
the claude provider raised TypeError out of advance_pipeline and aborted the
whole scheduler tick. These tests pin the widened signature, the ignored-
model_tag semantics, and the end-to-end review gate through the real caller
(pipeline.server._role_resource_ok, the same handle the neighbouring
test_pipeline_mcp_server_* tests use).
"""
import functools
import inspect
import json

import pytest

from app import backend, role_registry
from app.backend_claude import ClaudeCliDriver
from pipeline import server as p
from pipeline import usage as pusage

_NOT_PAUSED = {"session_pct": 5, "week_pct": 5, "paused": False}
_PAUSED = {"session_pct": 100, "week_pct": 100, "paused": True}

# plan role_config names claude/sonnet, and role_registry.resolve_role
# validates the friendly model name against providers.<provider>.models, so
# the synthetic registry must declare it. Never rely on the live
# model_registry.json (see .claude/rules/testing-config-gates.md).
_SYNTHETIC_REGISTRY = {
    "providers": {"claude": {"models": {"sonnet": {"tag": "claude-sonnet-4-5"}}}},
    "roles": {},
}

# The drift guard below parametrizes over the backend registry
# (app/backend.py, "Registry of available drivers by config name"). Entries
# may be functools.partial constructors, so resolve the class behind each.
_REGISTRY = getattr(backend, "_DRIVERS", {})

_MODEL_TAGS = ["sonnet", None, "", "anything"]


@pytest.fixture
def usage_state_path(tmp_path, monkeypatch):
    """The same isolation the neighbouring suite's autouse fixture provides:
    point the usage gate at a tmp file no poller rewrites, so these tests
    never read the developer's live ~/.claude/usage_state.json. A missing
    file reads as "not paused"."""
    path = tmp_path / "usage_state.json"
    monkeypatch.setattr(pusage, "USAGE_STATE_PATH", path)
    monkeypatch.setattr(p, "USAGE_STATE_PATH", path)
    return path


@pytest.fixture
def identity_unchecked(monkeypatch):
    """Force the provider-identity preflight cache to "never probed" so a
    cache value left by an earlier test in this process cannot flip the gate
    these tests grade (an unchecked cache fails open, like missing state)."""
    from app import backend_claude as bc

    monkeypatch.setattr(bc, "_claude_identity_status", None)


def _driver_class(entry):
    return entry.func if isinstance(entry, functools.partial) else entry


# ---------- the exact failing call ----------
def test_resource_status_accepts_model_tag_keyword(
    usage_state_path, identity_unchecked,
):
    """The exact call the review gate makes must return the gate dict, not
    raise TypeError: resource_status(model_tag="sonnet")."""
    usage_state_path.write_text(json.dumps(_NOT_PAUSED))

    status = ClaudeCliDriver().resource_status(model_tag="sonnet")

    assert isinstance(status, dict)
    assert "ok" in status
    assert "reason" in status


@pytest.mark.parametrize("model_tag", _MODEL_TAGS, ids=["sonnet", "none", "empty", "other"])
def test_resource_status_not_paused_ok_and_ignores_model_tag(
    usage_state_path, identity_unchecked, model_tag,
):
    """With the usage gate not paused, Claude is available whatever model_tag
    is passed (including the default None and an empty string): Claude's gate
    is the usage state, not a per-model check."""
    usage_state_path.write_text(json.dumps(_NOT_PAUSED))

    assert ClaudeCliDriver().resource_status(model_tag=model_tag) == {
        "ok": True, "reason": "",
    }


@pytest.mark.parametrize("model_tag", _MODEL_TAGS, ids=["sonnet", "none", "empty", "other"])
def test_resource_status_paused_gates_whatever_model_tag(
    usage_state_path, identity_unchecked, model_tag,
):
    """With the usage gate paused, Claude is gated with the poller's reason,
    whatever model_tag is passed."""
    usage_state_path.write_text(json.dumps(_PAUSED))

    assert ClaudeCliDriver().resource_status(model_tag=model_tag) == {
        "ok": False, "reason": "Claude usage gate tripped",
    }


def test_resource_status_no_args_still_works(usage_state_path, identity_unchecked):
    """Back-compat boundary: model_tag must be optional (defaulted), because
    the env-based gate path and collect_backend_status() still call
    resource_status() with no arguments."""
    usage_state_path.write_text(json.dumps(_NOT_PAUSED))

    assert ClaudeCliDriver().resource_status() == {"ok": True, "reason": ""}


def test_resource_status_missing_state_fails_open(usage_state_path, identity_unchecked):
    """Boundary: no usage-state file at all reads as "not paused" (fail open),
    with model_tag accepted exactly as in the paused/not-paused cases."""
    # usage_state_path's isolated file was never written.

    assert ClaudeCliDriver().resource_status(model_tag="sonnet") == {
        "ok": True, "reason": "",
    }


def test_resource_status_rejects_unknown_keyword(usage_state_path, identity_unchecked):
    """Negative: the signature was widened by exactly one parameter — an
    unknown keyword must still raise TypeError (loud, not swallowed)."""
    usage_state_path.write_text(json.dumps(_NOT_PAUSED))

    with pytest.raises(TypeError, match="bogus"):
        ClaudeCliDriver().resource_status(bogus=1)


def test_resource_status_docstring_documents_model_tag():
    """Structural: the widened parameter is documented on the method
    (membership only — never the exact wording)."""
    doc = inspect.getdoc(ClaudeCliDriver.resource_status) or ""

    assert "model_tag" in doc


# ---------- integration through the real caller ----------
def test_role_resource_ok_review_claude_not_paused(
    usage_state_path, identity_unchecked, monkeypatch,
):
    """A plan pinning review to claude/sonnet must be gated by Claude's
    resource_status(model_tag=...) through the real caller, and return ok when
    the usage gate is not paused — not raise TypeError out of the gate."""
    usage_state_path.write_text(json.dumps(_NOT_PAUSED))
    monkeypatch.setattr(
        role_registry, "load_registry", lambda *a, **k: _SYNTHETIC_REGISTRY,
    )

    ok, reason = p._role_resource_ok(
        "review",
        plan_role_config={"review": {"provider": "claude", "model": "sonnet"}},
    )

    assert (ok, reason) == (True, "")


def test_role_resource_ok_review_claude_paused(
    usage_state_path, identity_unchecked, monkeypatch,
):
    """Negative side of the same path: a paused usage gate defers review with
    Claude's own reason, without raising."""
    usage_state_path.write_text(json.dumps(_PAUSED))
    monkeypatch.setattr(
        role_registry, "load_registry", lambda *a, **k: _SYNTHETIC_REGISTRY,
    )

    ok, reason = p._role_resource_ok(
        "review",
        plan_role_config={"review": {"provider": "claude", "model": "sonnet"}},
    )

    assert (ok, reason) == (False, "Claude usage gate tripped")


# ---------- drift guard: no driver can reintroduce this ----------
def test_drift_guard_found_the_backend_registry():
    """Belt: if the registry the drift guard parametrizes over was renamed or
    emptied, the parametrized test below would silently collect zero cases —
    fail loudly here instead."""
    assert isinstance(_REGISTRY, dict) and len(_REGISTRY) > 0


@pytest.mark.parametrize("name", sorted(_REGISTRY))
def test_registered_driver_resource_status_declares_model_tag(name):
    cls = _driver_class(_REGISTRY[name])
    params = inspect.signature(cls.resource_status).parameters

    assert "model_tag" in params, (
        f"{cls.__name__}.resource_status (registered as {name!r}) does not "
        "declare model_tag, so the review gate's "
        "resource_status(model_tag=...) raises TypeError for this driver"
    )