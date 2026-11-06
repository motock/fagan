"""Tests that the story-scoped service methods bind the usage scope.

Every dispatch entry point (the MCP tool, the dashboard story route and the
scheduler tick) funnels through ``PipelineService.dispatch_story``, and every
review entry point funnels through ``PipelineService.review_story``. Backend
calls made underneath them read ``usage_record.current_scope()`` to attribute
token usage to a plan/story, so those two methods must bind the scope for the
duration of the call and unbind it on every exit path - a normal return, a
refusal result, or an input-validation ``ValueError``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# pipeline.server is imported explicitly first: the pipeline package has a
# pre-existing import cycle, and importing the server module resolves it (see
# tests/unit/test_triage_cap_silent_skip.py).
import pipeline.server  # noqa: F401  # import-order cycle breaker
from pipeline import usage_record
from pipeline.service import PipelineService


def test_dispatch_story_binds_scope_around_the_delegated_call():
    """The delegated dispatch call sees the plan/story it is running for."""
    captured = []

    def fake_impl(*args, **kwargs):
        captured.append(usage_record.current_scope())
        return {"ok": True}

    assert usage_record.current_scope() is None
    with patch("pipeline.service._dispatch_story_impl", fake_impl):
        service = PipelineService()
        first = service.dispatch_story("demo-plan", "S-1")
        second = service.dispatch_story("other-plan", "S-2")

    assert first == {"ok": True}
    assert second == {"ok": True}
    # Each call binds its own scope: the second must not inherit the first.
    assert [(scope or {}).get("plan") for scope in captured] == [
        "demo-plan",
        "other-plan",
    ]
    assert [(scope or {}).get("story_key") for scope in captured] == ["S-1", "S-2"]
    assert usage_record.current_scope() is None


def test_review_story_binds_scope_and_unbinds_after_a_refusal():
    """A refusal is a normal return, so the scope must not survive it."""
    captured = []

    def fake_authorize(*args, **kwargs):
        captured.append(usage_record.current_scope())
        return {"ok": False, "error": "denied"}

    assert usage_record.current_scope() is None
    with patch("pipeline.authorization.authorize", fake_authorize):
        service = PipelineService()
        first = service.review_story("demo-plan", "S-1")
        second = service.review_story("next-plan", "S-9")

    assert first == {"ok": False, "error": "denied"}
    assert second == {"ok": False, "error": "denied"}
    assert [(scope or {}).get("plan") for scope in captured] == [
        "demo-plan",
        "next-plan",
    ]
    assert [(scope or {}).get("story_key") for scope in captured] == ["S-1", "S-9"]
    assert usage_record.current_scope() is None


def test_invalid_story_key_raises_and_leaves_no_scope_bound():
    """Rejected input must not leave a stale scope behind for the next call."""
    captured = []

    def fake_impl(*args, **kwargs):
        captured.append(usage_record.current_scope())
        return {"ok": True}

    service = PipelineService()
    assert usage_record.current_scope() is None
    with pytest.raises(ValueError):
        service.dispatch_story("demo-plan", "../x")
    assert usage_record.current_scope() is None

    # A well-formed follow-up call still binds its own scope: the rejected one
    # left nothing behind.
    with patch("pipeline.service._dispatch_story_impl", fake_impl):
        assert service.dispatch_story("demo-plan", "S-1") == {"ok": True}
    assert (captured[0] or {}).get("plan") == "demo-plan"
    assert (captured[0] or {}).get("story_key") == "S-1"
    assert usage_record.current_scope() is None
