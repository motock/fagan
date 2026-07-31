"""Acceptance oracle: the pre-dispatch oracle gate must be WIRED INTO the
registered dispatch_story tool, after fixture materialization and before the
executor launches.

A unit test of validate_acceptance_fixtures cannot catch an unwired gate, so
this grades the registration path and the call ordering in the production
source, plus the refusal status the gate must set.
"""
import inspect

import pipeline.server as srv


def _dispatch_source():
    tool = srv.mcp._tool_manager._tools["dispatch_story"]
    return inspect.getsource(tool.fn)


def test_dispatch_story_is_still_registered_as_a_tool():
    assert "dispatch_story" in srv.mcp._tool_manager._tools


def test_dispatch_story_calls_the_oracle_gate():
    assert "validate_acceptance_fixtures" in _dispatch_source(), (
        "dispatch_story must call validate_acceptance_fixtures; a gate that is "
        "only defined but never called grades nothing"
    )


def test_the_gate_runs_after_materialization_and_before_the_executor():
    src = _dispatch_source()
    materialize = src.index('target.write_text(entry["source"])')
    gate = src.index("validate_acceptance_fixtures")
    assert materialize < gate, (
        "the gate must run AFTER the fixtures are materialized into the worktree"
    )
    marker = "_run_test_author_phase"
    assert marker in src and gate < src.index(marker), (
        "the gate must run BEFORE any executor/test-author dispatch is launched"
    )


def test_blocked_oracle_status_is_a_recognized_refusal():
    src = _dispatch_source()
    assert "blocked_oracle" in src, (
        "a rejected oracle must park the story with an explicit blocked_oracle "
        "status rather than dispatching anyway"
    )


def test_the_operator_is_notified_when_the_gate_refuses():
    src = _dispatch_source()
    gate = src.index("validate_acceptance_fixtures")
    assert "_notify_user" in src[gate:], (
        "the refusal must emit a _notify_user notification, not only a log line"
    )
