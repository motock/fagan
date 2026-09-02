"""Cloud dispatches are exempt from the MAX_CONCURRENT_AGENTS slot cap.

MAX_CONCURRENT_AGENTS (the dispatch section of
``pipeline/advance.py::_advance_pipeline_locked_impl``) was introduced as a
spend-bounding cap between /cost polls, but its slot math counts EVERY
in_progress agent via ``_count_in_progress_agents`` — including cloud-backed
dispatch that has no on-device footprint to protect. The slot accounting must
count only ON-DEVICE in_progress stories:

  - a story is on-device when its RAW ``story.get("backend")`` field is
    absent or NOT ``claude`` (independent of the ``PIPELINE_BACKEND_DISPATCH``
    env default — a story with no explicit backend counts even when the env
    default is ``claude``) AND its model tag (``story.get("model")``) does
    NOT end with ``:cloud``;
  - a story with NO explicit model tag on a local-family backend resolves to
    the env-default on-device model, so it COUNTS (conservative — same rule
    as the per-story interruption gate's no-tag branch);
  - claude-routed and ``:cloud``-tagged stories never consume a slot (they
    are gated by the usage pause thresholds in the per-story gate loop, which
    must stay independent of the cap).

``_count_in_progress_agents`` itself keeps its existing cross-plan,
backend-blind semantics — pipeline/dispatch.py's concurrent-dispatch warning
gates still call it and must keep seeing claude/:cloud agents. Only the cap's
slot math gets the on-device-aware count.

Mirrors the mocking style of the existing cap tests in
test_pipeline_mcp_server_usage_visibility_and_locks.py (patch
``p.MAX_CONCURRENT_AGENTS`` / tick seams via the pipeline.server module ref,
write manifests with _write_manifest, record dispatch_story calls).
"""

import os

from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _read_manifest,
    _write_manifest,
    plan_dir,
)

_CLOUD_TAG = "glm-5.2:cloud"
_DEVICE_TAG = "gpt-oss:20b"


class _FakeDispatchBackend:
    """Stand-in for ``app.backend`` as advance.py sees it via its _ServerRef.

    Every driver lookup resolves to a driver whose ``resource_status`` is ok,
    unless the test pins failing model tags (used to prove the per-story gate
    still applies to slot-exempt cloud stories).
    """

    def __init__(self, fail_tags=()):
        self._fail_tags = frozenset(fail_tags)

    def get_backend(self, role, name=None):
        return self

    def resource_status(self, model_tag=None):
        if model_tag in self._fail_tags:
            return {"ok": False, "reason": f"{model_tag} quota exhausted"}
        return {"ok": True, "reason": ""}


def _stub_tick_seams(monkeypatch, max_agents, fail_tags=()):
    """Stub everything around the dispatch section of the advance tick so a
    test exercises only the slot math + the per-story dispatch gate.

    Returns (dispatched, interrupted, notes) recorder lists.
    """
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", max_agents)
    # Blanket dispatch/review gates pass; the per-story gate is what's under
    # test (and it consults backend.get_backend(...).resource_status itself).
    monkeypatch.setattr(
        p, "_role_resource_ok",
        lambda role, plan_role_config=None: (True, ""),
    )
    monkeypatch.setattr(p, "backend", _FakeDispatchBackend(fail_tags))
    # Live pid so any pid-liveness check in the counting path sees the agent.
    # check_story_status is stubbed: polling must not transition the story.
    monkeypatch.setattr(
        p, "check_story_status", lambda plan, key: {"status": "running"},
    )
    dispatched = []
    monkeypatch.setattr(
        p, "dispatch_story", lambda plan, key: dispatched.append(key),
    )
    interrupted = []
    monkeypatch.setattr(
        p, "interrupt_story",
        lambda plan, key: interrupted.append(key) or {"ok": True},
    )
    notes = []
    monkeypatch.setattr(
        p, "_notify_user", lambda plan, msg, **kwargs: notes.append(msg)
    )
    return dispatched, interrupted, notes


# ---------- cloud READY stories are exempt from the slot cap ----------

def test_cloud_ready_story_dispatches_when_on_device_slots_are_exhausted(
    plan_dir, monkeypatch,
):
    """Cap 1 with one on-device agent running: the on-device count gates
    ON-DEVICE dispatch only. The ready :cloud story must dispatch in the same
    tick (its dispatch has no on-device footprint), while a ready on-device
    story stays deferred — proving the on-device count is what gates."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    dispatched, interrupted, _ = _stub_tick_seams(monkeypatch, max_agents=1)
    _write_manifest(plan_dir, "cloudslot_ready", {
        "R1": {"summary": "running on-device", "status": "in_progress",
               "pid": os.getpid(), "worktree": "/x",
               "model": _DEVICE_TAG, "dependencies": []},
        "C1": {"summary": "cloud story", "status": "todo",
               "model": _CLOUD_TAG, "dependencies": []},
        "L1": {"summary": "device story", "status": "todo",
               "model": _DEVICE_TAG, "dependencies": []},
    })

    result = p.advance_pipeline("cloudslot_ready")

    assert dispatched == ["C1"], (
        "the :cloud story must dispatch in the same tick (cloud dispatches "
        f"are exempt from the cap); got dispatched={dispatched}"
    )
    assert result["dispatched"] == ["C1"]
    # The on-device count still gates: L1 must NOT have been dispatched.
    assert interrupted == []


def test_claude_in_progress_story_does_not_consume_a_slot(
    plan_dir, monkeypatch,
):
    """A claude-routed in_progress story has no on-device footprint, so it
    must not consume a MAX_CONCURRENT_AGENTS slot: with cap 1 and only a
    claude agent running, a ready :cloud story still dispatches."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    dispatched, interrupted, _ = _stub_tick_seams(monkeypatch, max_agents=1)
    _write_manifest(plan_dir, "cloudslot_claude", {
        "R1": {"summary": "claude agent", "status": "in_progress",
               "pid": os.getpid(), "worktree": "/x",
               "backend": "claude", "dependencies": []},
        "C1": {"summary": "cloud story", "status": "todo",
               "model": _CLOUD_TAG, "dependencies": []},
    })

    result = p.advance_pipeline("cloudslot_claude")

    assert dispatched == ["C1"], (
        "claude-backed in_progress stories must not consume a slot; "
        f"got dispatched={dispatched}"
    )
    assert result["dispatched"] == ["C1"]
    assert interrupted == []


def test_cloud_in_progress_story_does_not_consume_a_slot(
    plan_dir, monkeypatch,
):
    """A :cloud-tagged in_progress story (served via Ollama with zero local
    VRAM footprint) must not consume a slot either: with cap 1 and only such
    an agent running, a ready on-device story still dispatches this tick."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    dispatched, interrupted, _ = _stub_tick_seams(monkeypatch, max_agents=1)
    _write_manifest(plan_dir, "cloudslot_running_cloud", {
        "R1": {"summary": "running cloud agent", "status": "in_progress",
               "pid": os.getpid(), "worktree": "/x",
               "model": _CLOUD_TAG, "dependencies": []},
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
    })

    result = p.advance_pipeline("cloudslot_running_cloud")

    assert dispatched == ["T1"], (
        ":cloud in_progress stories must not consume a slot; "
        f"got dispatched={dispatched}"
    )
    assert result["dispatched"] == ["T1"]
    assert interrupted == []


# ---------- conservative no-tag counting is preserved ----------

def test_no_model_tag_in_progress_story_still_counts_against_the_cap(
    plan_dir, monkeypatch,
):
    """A story with NO explicit model tag on a local-family backend resolves
    to the env-default on-device model, so it COUNTS against the cap. With
    cap 1 and such an agent running, a ready story is deferred this tick."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    dispatched, interrupted, _ = _stub_tick_seams(monkeypatch, max_agents=1)
    _write_manifest(plan_dir, "cloudslot_notag", {
        "R1": {"summary": "running, no model tag", "status": "in_progress",
               "pid": os.getpid(), "worktree": "/x", "dependencies": []},
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
    })

    result = p.advance_pipeline("cloudslot_notag")

    assert dispatched == [], (
        "a no-tag in_progress story resolves to the on-device default model "
        f"and must consume a slot; got dispatched={dispatched}"
    )
    assert result["dispatched"] == []
    assert interrupted == []


# ---------- cap boundary values ----------

def test_zero_max_concurrent_agents_still_means_uncapped(plan_dir, monkeypatch):
    """MAX_CONCURRENT_AGENTS=0 still means no cap, even with an on-device
    agent running: every ready story (cloud and on-device) dispatches."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    dispatched, interrupted, _ = _stub_tick_seams(monkeypatch, max_agents=0)
    _write_manifest(plan_dir, "cloudslot_zero", {
        "R1": {"summary": "running on-device", "status": "in_progress",
               "pid": os.getpid(), "worktree": "/x",
               "model": _DEVICE_TAG, "dependencies": []},
        "C1": {"summary": "cloud story", "status": "todo",
               "model": _CLOUD_TAG, "dependencies": []},
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
    })

    result = p.advance_pipeline("cloudslot_zero")

    assert dispatched == ["C1", "T1"]
    assert result["dispatched"] == ["C1", "T1"]
    assert interrupted == []


def test_negative_max_concurrent_agents_still_means_uncapped(
    plan_dir, monkeypatch,
):
    """MAX_CONCURRENT_AGENTS<0 still means no cap (the `> 0` guard), even
    with an on-device agent running."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    dispatched, interrupted, _ = _stub_tick_seams(monkeypatch, max_agents=-1)
    _write_manifest(plan_dir, "cloudslot_negative", {
        "R1": {"summary": "running on-device", "status": "in_progress",
               "pid": os.getpid(), "worktree": "/x",
               "model": _DEVICE_TAG, "dependencies": []},
        "C1": {"summary": "cloud story", "status": "todo",
               "model": _CLOUD_TAG, "dependencies": []},
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
    })

    result = p.advance_pipeline("cloudslot_negative")

    assert dispatched == ["C1", "T1"]
    assert result["dispatched"] == ["C1", "T1"]
    assert interrupted == []


def test_fully_consumed_on_device_cap_defers_ready_stories(
    plan_dir, monkeypatch,
):
    """Negative case: two on-device agents already hold both slots (cap 2) -
    nothing new dispatches. R2's model is an explicit JSON null, which must
    count exactly like a missing tag (on-device default), so BOTH running
    stories consume slots."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    dispatched, interrupted, _ = _stub_tick_seams(monkeypatch, max_agents=2)
    _write_manifest(plan_dir, "cloudslot_full", {
        "R1": {"summary": "running one", "status": "in_progress",
               "pid": os.getpid(), "worktree": "/x",
               "model": _DEVICE_TAG, "dependencies": []},
        "R2": {"summary": "running two", "status": "in_progress",
               "pid": os.getpid(), "worktree": "/y",
               "model": None, "dependencies": []},
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
        "T2": {"summary": "two", "status": "todo", "dependencies": []},
    })

    result = p.advance_pipeline("cloudslot_full")

    assert dispatched == [], (
        "two on-device agents must consume both slots; got "
        f"dispatched={dispatched}"
    )
    assert result["dispatched"] == []
    assert interrupted == []


# ---------- the cap and the per-story gate stay independent ----------

def test_cloud_slot_exempt_does_not_bypass_per_story_dispatch_gate(
    plan_dir, monkeypatch,
):
    """Slot exemption is not a gate bypass: with slots free (cap 5), a ready
    :cloud story whose OWN model gate is down is still deferred (recorded as
    gated, not dispatched, not failed), and the running on-device story is
    not interrupted by the local memory gate."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    dispatched, interrupted, _ = _stub_tick_seams(
        monkeypatch, max_agents=5, fail_tags={_CLOUD_TAG},
    )
    _write_manifest(plan_dir, "cloudslot_gate", {
        "R1": {"summary": "running on-device", "status": "in_progress",
               "pid": os.getpid(), "worktree": "/x",
               "model": _DEVICE_TAG, "dependencies": []},
        "C1": {"summary": "cloud story", "status": "todo",
               "model": _CLOUD_TAG, "dependencies": []},
    })

    result = p.advance_pipeline("cloudslot_gate")

    assert dispatched == [], (
        "a :cloud story with its own gate down must be deferred by the "
        f"per-story gate even though slots are free; got dispatched={dispatched}"
    )
    assert result["dispatched"] == []
    assert interrupted == []
    assert _read_manifest(plan_dir, "cloudslot_gate")["stories"]["C1"][
        "status"
    ] == "todo"


# ---------- the cap's count stays cross-plan ----------

def test_on_device_cap_counts_in_progress_stories_across_plans(
    plan_dir, monkeypatch,
):
    """The slot count keeps _count_in_progress_agents' cross-plan scope (the
    cap bounds this session's spend, not one plan's): an on-device agent
    running under a DIFFERENT plan's manifest consumes a slot for this tick
    too, so the ready story here is deferred."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    dispatched, interrupted, _ = _stub_tick_seams(monkeypatch, max_agents=1)
    _write_manifest(plan_dir, "cloudslot_other_plan", {
        "R1": {"summary": "running elsewhere", "status": "in_progress",
               "pid": os.getpid(), "worktree": "/x",
               "model": _DEVICE_TAG, "dependencies": []},
    })
    _write_manifest(plan_dir, "cloudslot_this_plan", {
        "T1": {"summary": "one", "status": "todo", "dependencies": []},
    })

    result = p.advance_pipeline("cloudslot_this_plan")

    assert dispatched == [], (
        "the cap's on-device count must span plans (the spend window is "
        f"session-wide); got dispatched={dispatched}"
    )
    assert result["dispatched"] == []
    assert interrupted == []


# ---------- _count_in_progress_agents keeps its old semantics ----------

def test_count_in_progress_agents_still_counts_cloud_and_claude_stories(
    plan_dir,
):
    """_count_in_progress_agents' signature and behavior are preserved for
    its other callers (pipeline/dispatch.py's concurrent-dispatch warning
    gates): it still counts EVERY live in_progress agent across plans,
    including claude-routed and :cloud ones — only the cap's slot math gets
    the on-device filter."""
    live_pid = os.getpid()
    _write_manifest(plan_dir, "ccount_claude", {
        "K1": {"summary": "claude agent", "status": "in_progress",
               "pid": live_pid, "backend": "claude", "dependencies": []},
    })
    _write_manifest(plan_dir, "ccount_cloud", {
        "K2": {"summary": "cloud agent", "status": "in_progress",
               "pid": live_pid, "model": _CLOUD_TAG, "dependencies": []},
    })
    _write_manifest(plan_dir, "ccount_device", {
        "K3": {"summary": "device agent", "status": "in_progress",
               "pid": live_pid, "model": _DEVICE_TAG, "dependencies": []},
    })

    # Zero-arg call pins the existing signature.
    assert p._count_in_progress_agents() == 3
