"""Acceptance oracle: when the test-author phase falls open, the operator must
be NOTIFIED, not only logged at.

The fail-open itself is deliberate and must be preserved (it is the feature's
single most safety-critical property); only the silence is the defect. Two
stories lost their TDD-split crutch this way with no operator-visible signal.
"""
import inspect

from pipeline import planner


def test_the_phase_takes_a_plan_name():
    params = inspect.signature(planner._run_test_author_phase).parameters
    assert "plan_name" in params, (
        "_notify_user is keyed by plan, so the phase must know its plan name"
    )


def test_dispatch_start_failure_notifies(monkeypatch):
    seen = []
    monkeypatch.setattr(planner, "_notify_user", lambda plan, msg: seen.append((plan, msg)))
    monkeypatch.setattr(
        planner, "_resolve_test_author_backend", lambda *a, **k: ("claude", "sonnet")
    )

    class Boom:
        def dispatch(self, **kwargs):
            raise RuntimeError("cli missing")

    monkeypatch.setattr(planner.backend, "get_backend", lambda *a, **k: Boom())

    result = planner._run_test_author_phase(
        {"agent_instructions": "x"},
        story_key="S1",
        worktree_path=planner.Path("/tmp"),
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        plan_name="plan",
    )
    assert result is False, "the fail-open contract must be preserved"
    assert seen, "the fall-open must emit a _notify_user notification"
    assert "S1" in seen[0][1]


def test_unconfigured_role_notifies(monkeypatch):
    seen = []
    monkeypatch.setattr(planner, "_notify_user", lambda plan, msg: seen.append((plan, msg)))
    monkeypatch.setattr(planner, "_resolve_test_author_backend", lambda *a, **k: (None, None))

    result = planner._run_test_author_phase(
        {"agent_instructions": "x"},
        story_key="S1",
        worktree_path=planner.Path("/tmp"),
        dispatch_backend="ollama",
        local_model="gpt-oss:20b",
        plan_name="plan",
    )
    assert result is False
    assert seen, "an unconfigured test_author role must be visible to the operator"


def test_the_dispatch_call_site_passes_plan_name():
    import pipeline.server as srv

    src = inspect.getsource(srv._dispatch_story_impl)
    # Anchor on the actual call (with its opening paren), not an earlier
    # comment mentioning the function name.
    call = src.index("_run_test_author_phase(")
    assert "plan_name" in src[call:call + 600], (
        "dispatch_story must pass plan_name to the test-author phase"
    )
