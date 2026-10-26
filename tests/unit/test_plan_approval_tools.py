"""Tests for the approve_plan / reject_plan MCP tools (ENT-LC-02b).

The tools live in ``pipeline.server_tools_lifecycle`` and are registered onto the
FastMCP instance by the loop in ``pipeline.server``. Each tool is a thin
delegating wrapper over the mixed-in service method, reached through the
``_service`` module proxy, so these tests pin:

* membership in ``TOOLS`` and registration on ``mcp`` with an identical ``.fn``;
* re-export on the ``pipeline.server`` module by the registration loop;
* exactly one delegation per call, with ``reason`` always passed positionally;
* the service's result is returned unchanged (fail-closed skipped payloads are
  not wrapped, cached, or mutated).
"""

import inspect
from unittest.mock import Mock

import pipeline.server_tools_lifecycle as lifecycle
from pipeline import server
from pipeline.server_tools_lifecycle import TOOLS


def test_tools_contains_approval_tools():
    # TOOLS holds the tool functions themselves (server.py's loop reads
    # ``_tool.__name__``), so membership is checked by name.
    names = [tool.__name__ for tool in TOOLS]
    assert "approve_plan" in names
    assert "reject_plan" in names


def test_registered_with_identical_fn():
    tools = server.mcp._tool_manager._tools
    assert tools["approve_plan"].fn is lifecycle.approve_plan
    assert tools["reject_plan"].fn is lifecycle.reject_plan


def test_server_module_exposes_tools():
    assert server.approve_plan is lifecycle.approve_plan
    assert server.reject_plan is lifecycle.reject_plan


def test_approve_plan_delegates_once(monkeypatch):
    svc = Mock()
    svc.approve_plan.return_value = {"status": "approved"}
    monkeypatch.setattr(lifecycle, "_service", svc)

    assert lifecycle.approve_plan("alpha") == {"status": "approved"}
    svc.approve_plan.assert_called_once_with("alpha")


def test_reject_plan_delegates_once_with_default_reason(monkeypatch):
    svc = Mock()
    svc.reject_plan.return_value = {"status": "rejected"}
    monkeypatch.setattr(lifecycle, "_service", svc)

    assert lifecycle.reject_plan("alpha") == {"status": "rejected"}
    svc.reject_plan.assert_called_once_with("alpha", "")


def test_reject_plan_delegates_reason(monkeypatch):
    svc = Mock()
    svc.reject_plan.return_value = {"status": "rejected"}
    monkeypatch.setattr(lifecycle, "_service", svc)

    assert lifecycle.reject_plan("alpha", "duplicate") == {"status": "rejected"}
    svc.reject_plan.assert_called_once_with("alpha", "duplicate")


def test_reject_plan_signature():
    assert inspect.signature(lifecycle.reject_plan).parameters["reason"].default == ""


def test_disabled_approval_returns_skipped_unchanged(monkeypatch):
    """A disabled approval must return the service's skipped dict untouched.

    Catches: caching the first result in a module global and returning it for a
    later plan, wrapping the result (``{"approved": bool(result)}`` /
    ``result.get("approved", False)``), and mutating the service's dict (e.g.
    adding ``"plan_name"``).
    """
    skipped1 = {"status": "skipped", "reason": "plan approval disabled"}
    skipped2 = {"status": "skipped", "reason": "plan approval disabled"}
    svc = Mock()
    svc.approve_plan.side_effect = [skipped1, skipped2]
    monkeypatch.setattr(lifecycle, "_service", svc)

    r1 = lifecycle.approve_plan("alpha")
    assert r1 is skipped1

    r2 = lifecycle.approve_plan("beta")
    assert r2 is skipped2
    assert svc.approve_plan.call_count == 2
    assert skipped1 == {"status": "skipped", "reason": "plan approval disabled"}


def test_docstrings_state_fail_closed_and_self_approval():
    for name in ("approve_plan", "reject_plan"):
        doc = getattr(lifecycle, name).__doc__.lower()
        assert "fail-closed" in doc, name
        assert "self-approval" in doc, name
        assert "recorded" in doc, name
