"""Acceptance oracle: ingesting a plan whose stories dispatch to a local model
while test_author/planner resolve to a different provider must warn.

That exact configuration silently removed the TDD-split and tech-lead crutches
from two stories, both of which then parked.
"""
import inspect

import pipeline.server as srv
from pipeline.planner import _scaffolding_provider_mismatch_warning


def test_flags_local_dispatch_with_a_non_local_test_author():
    msg = _scaffolding_provider_mismatch_warning(
        dispatch_backend="ollama",
        role_config={"review": {"provider": "ollama", "model": "glm"}},
        registry_roles={"test_author": {"provider": "claude", "model": "sonnet"}},
    )
    assert msg is not None
    assert "test_author" in msg


def test_matched_providers_are_not_flagged():
    assert _scaffolding_provider_mismatch_warning(
        dispatch_backend="ollama",
        role_config={"test_author": {"provider": "ollama", "model": "glm"}},
        registry_roles={"test_author": {"provider": "claude", "model": "sonnet"}},
    ) is None


def test_claude_dispatch_is_not_flagged():
    assert _scaffolding_provider_mismatch_warning(
        dispatch_backend="claude",
        role_config={},
        registry_roles={"test_author": {"provider": "claude", "model": "sonnet"}},
    ) is None


def test_the_warning_is_wired_into_ingest_plan():
    # ingest_plan is a thin @mcp.tool() delegate onto _ingest_plan_impl (the
    # PipelineService W1a extraction); the warning call lives in the impl.
    src = inspect.getsource(srv._ingest_plan_impl)
    assert "_scaffolding_provider_mismatch_warning" in src
