"""The in-progress interruption gate must not kill a LOCAL story on memory pressure.

``pipeline/advance.py::_advance_pipeline_locked_impl`` has an in-progress
interruption gate. Its claude-routed branch already refuses to interrupt on
local memory pressure (``if not dispatch_ok and not memory_pressure`` under the
comment "Never interrupt a claude story on local memory pressure."). The local
(non-``:cloud``) branch below it applied no such exception: it called
``interrupt_story()`` whenever the story's OWN backend reported
``resource_status(model_tag=tag)`` as not ok.

For an on-device model that reading is partly self-inflicted - the story's own
already-loaded weights are much of what depresses free memory - so the gate was
tripped by the very work it killed. Interrupting reclaims nothing (the weights
are already resident), and a live incident saw one local benchmark story
interrupted 26 times in ~16 minutes with zero net progress.

The fix extends the claude branch's exception to the local branch: the local
``if tag:`` block must NOT call ``interrupt_story()`` when the story's own gate
failure reason indicates insufficient free memory. Every other path stays
byte-for-byte unchanged: the ``:cloud`` continue, the no-tag ``else`` branch,
and the claude branch.

These tests stub the backend so ``resource_status`` is deterministic. They never
assert this host's real free-memory value - a test pinned to today's live
reading would break on the next legitimate host or config change.
"""

import os
import re
from pathlib import Path

import pytest

from pipeline import concurrency as pcon
from pipeline import persistence as ppers
from pipeline import server as p
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _read_manifest,
    _write_manifest,
)


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Isolated plans dir, mirroring the shared helper fixture.

    Defined locally rather than imported: an imported fixture name reused as a
    test-function parameter trips ruff's F811, and this file is not in
    pyproject's per-file-ignores list (the brief scopes edits to
    pipeline/advance.py plus this test file only).
    """
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    monkeypatch.setattr(pcon, "PLAN_DIR", d)
    return d

_DEVICE_TAG = "gpt-oss:20b"
_CLOUD_TAG = "glm-5.2:cloud"
# The exact reason string measured live 2026-09-15. The guard keys off the
# substring "insufficient free memory", not this whole sentence.
_MEMORY_REASON = "insufficient free memory (1816mb < 2048mb floor)"
_OTHER_REASON = "ollama server unreachable"

_ADVANCE_PY = Path(__file__).resolve().parents[2] / "pipeline" / "advance.py"


class _FakeDispatchBackend:
    """Stand-in for ``app.backend`` as advance.py sees it via its _ServerRef.

    ``resource_status(model_tag=...)`` returns the pinned status for that tag
    (or ``default``), and records every tag it was asked about so a test can
    prove a path never consulted the backend at all.
    """

    def __init__(self, statuses=None, default=None):
        self._statuses = dict(statuses or {})
        self._default = {"ok": True, "reason": ""} if default is None else default
        self.calls = []

    def get_backend(self, role, name=None):
        return self

    def resource_status(self, model_tag=None):
        self.calls.append(model_tag)
        return dict(self._statuses.get(model_tag, self._default))


def _stub_tick(
    monkeypatch, *, dispatch_ok=True, dispatch_reason="", statuses=None, default=None,
):
    """Stub the seams around the in-progress interruption gate.

    Returns ``(fake_backend, dispatched, interrupted)`` recorder lists.
    """
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    # 0 == no cap, so the dispatch section never interferes with the story
    # under test (which is in_progress and therefore never "ready" anyway).
    monkeypatch.setattr(p, "MAX_CONCURRENT_AGENTS", 0)
    monkeypatch.setattr(
        p, "_role_resource_ok",
        lambda role, plan_role_config=None: (dispatch_ok, dispatch_reason),
    )
    fake = _FakeDispatchBackend(statuses, default)
    monkeypatch.setattr(p, "backend", fake)
    # Polling must not transition the story; a live pid keeps it "running".
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
    monkeypatch.setattr(p, "_notify_user", lambda plan, msg, **kwargs: None)
    return fake, dispatched, interrupted


def _running_story(**extra):
    story = {
        "summary": "running",
        "status": "in_progress",
        "pid": os.getpid(),
        "worktree": "/x",
        "dependencies": [],
    }
    story.update(extra)
    return story


# ---------- 1. positive fix: local memory pressure must NOT interrupt ----------

def test_local_memory_pressure_does_not_interrupt_in_progress_story(
    plan_dir, monkeypatch,
):
    """A local non-:cloud in_progress story whose OWN resource_status reports
    insufficient free memory must survive the tick: interrupt_story is not
    called, the story stays in_progress, and summary['interrupted'] omits it."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _fake, dispatched, interrupted = _stub_tick(
        monkeypatch,
        statuses={_DEVICE_TAG: {"ok": False, "reason": _MEMORY_REASON}},
    )
    _write_manifest(plan_dir, "memguard_local", {
        "L1": _running_story(model=_DEVICE_TAG),
    })

    result = p.advance_pipeline("memguard_local")

    assert interrupted == [], (
        "a local story under memory pressure must NOT be interrupted - its own "
        "loaded weights are what depress free memory, so interrupting reclaims "
        f"nothing; got interrupted={interrupted}"
    )
    assert "L1" not in result["interrupted"], (
        "summary['interrupted'] must not list a story spared by the memory "
        f"exception; got {result['interrupted']}"
    )
    assert dispatched == []
    manifest = _read_manifest(plan_dir, "memguard_local")
    assert manifest["stories"]["L1"]["status"] == "in_progress", (
        "the spared story must stay in_progress; got "
        f"{manifest['stories']['L1']['status']}"
    )


# ---------- 2. negative control: non-memory failures still interrupt ----------

def test_local_non_memory_failure_still_interrupts_in_progress_story(
    plan_dir, monkeypatch,
):
    """The identical story whose own resource_status fails for a NON-memory
    reason (server unreachable) is still interrupted - the pre-existing
    behavior is preserved for every non-memory failure."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _fake, _dispatched, interrupted = _stub_tick(
        monkeypatch,
        statuses={_DEVICE_TAG: {"ok": False, "reason": _OTHER_REASON}},
    )
    _write_manifest(plan_dir, "memguard_other", {
        "L1": _running_story(model=_DEVICE_TAG),
    })

    result = p.advance_pipeline("memguard_other")

    assert interrupted == ["L1"], (
        "a non-memory gate failure must still interrupt the local story; got "
        f"interrupted={interrupted}"
    )
    assert "L1" in result["interrupted"]


# ---------- 3. boundary: a missing reason keeps existing behavior ----------

def test_local_failure_without_reason_still_interrupts_in_progress_story(
    plan_dir, monkeypatch,
):
    """{'ok': False} with NO 'reason' key at all is interrupted: an unknown
    reason keeps existing behavior, because the change is deliberately scoped
    to the documented memory-pressure case."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _fake, _dispatched, interrupted = _stub_tick(
        monkeypatch,
        statuses={_DEVICE_TAG: {"ok": False}},
    )
    _write_manifest(plan_dir, "memguard_noreason", {
        "L1": _running_story(model=_DEVICE_TAG),
    })

    result = p.advance_pipeline("memguard_noreason")

    assert interrupted == ["L1"], (
        "a gate failure with no reason string must keep the old interrupt "
        f"behavior; got interrupted={interrupted}"
    )
    assert "L1" in result["interrupted"]


# ---------- 4. regression guard: :cloud is never interrupted ----------

def test_cloud_in_progress_story_is_never_interrupted(plan_dir, monkeypatch):
    """A :cloud-tagged in_progress story is never interrupted by the local
    memory gate, even when its gate is not ok - and the backend is never even
    consulted for the cloud tag (the branch continues first)."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    fake, _dispatched, interrupted = _stub_tick(
        monkeypatch,
        statuses={_CLOUD_TAG: {"ok": False, "reason": _MEMORY_REASON}},
    )
    _write_manifest(plan_dir, "memguard_cloud", {
        "C1": _running_story(model=_CLOUD_TAG),
    })

    result = p.advance_pipeline("memguard_cloud")

    assert interrupted == [], (
        f"a :cloud story must never be interrupted; got interrupted={interrupted}"
    )
    assert "C1" not in result["interrupted"]
    assert _CLOUD_TAG not in fake.calls, (
        "the :cloud branch must continue before consulting resource_status; "
        f"got calls={fake.calls}"
    )


# ---------- unchanged paths: claude branch and no-tag else branch ----------

def test_claude_branch_still_interrupts_on_non_memory_blanket_failure(
    plan_dir, monkeypatch,
):
    """The claude branch is unchanged: a claude-routed in_progress story is
    interrupted when the blanket gate is down for a NON-memory reason."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _fake, _dispatched, interrupted = _stub_tick(
        monkeypatch, dispatch_ok=False, dispatch_reason="claude usage exhausted",
    )
    _write_manifest(plan_dir, "memguard_claude", {
        "K1": _running_story(backend="claude"),
    })

    result = p.advance_pipeline("memguard_claude")

    assert interrupted == ["K1"], (
        "the claude branch must still interrupt on a non-memory blanket "
        f"failure; got interrupted={interrupted}"
    )
    assert "K1" in result["interrupted"]


def test_claude_branch_does_not_interrupt_on_memory_pressure(
    plan_dir, monkeypatch,
):
    """The claude branch's pre-existing memory exception is preserved: a
    claude-routed story is NOT interrupted when the blanket gate is down for
    insufficient free memory."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _fake, _dispatched, interrupted = _stub_tick(
        monkeypatch, dispatch_ok=False, dispatch_reason=_MEMORY_REASON,
    )
    _write_manifest(plan_dir, "memguard_claude_mem", {
        "K1": _running_story(backend="claude"),
    })

    result = p.advance_pipeline("memguard_claude_mem")

    assert interrupted == [], (
        "the claude branch must never interrupt on local memory pressure; got "
        f"interrupted={interrupted}"
    )
    assert "K1" not in result["interrupted"]


def test_no_tag_branch_still_interrupts_on_non_memory_blanket_failure(
    plan_dir, monkeypatch,
):
    """The no-tag ``else`` branch is unchanged: a local story with no explicit
    model tag is interrupted when the blanket gate is down for a NON-memory
    reason."""
    monkeypatch.setenv("PIPELINE_BACKEND_DISPATCH", "local")
    _fake, _dispatched, interrupted = _stub_tick(
        monkeypatch, dispatch_ok=False, dispatch_reason="claude usage exhausted",
    )
    _write_manifest(plan_dir, "memguard_notag", {
        "N1": _running_story(),
    })

    result = p.advance_pipeline("memguard_notag")

    assert interrupted == ["N1"], (
        "the no-tag else branch must still interrupt on a non-memory blanket "
        f"failure; got interrupted={interrupted}"
    )
    assert "N1" in result["interrupted"]


# ---------- source-level guard: the exception lives in the local branch ----------

def test_local_branch_source_guards_interrupt_on_memory_pressure_reason():
    """The local ``if tag:`` block must itself test the per-model status reason
    for the memory-pressure substring before calling interrupt_story().

    This pins the placement the brief requires: the guard is in the local
    in-progress branch (not merely the blanket ``memory_pressure`` variable,
    which is computed from the dispatch gate's reason and is already used by
    the claude branch)."""
    src = _ADVANCE_PY.read_text()
    cloud_idx = src.index('if tag and tag.endswith(":cloud"):')
    tag_idx = src.index("if tag:", cloud_idx)
    else_idx = src.index("else:", tag_idx)
    local_block = src[tag_idx:else_idx]

    assert "reason" in local_block, (
        "the local guard must test the returned status's own 'reason' string, "
        "not the blanket _dispatch_reason"
    )
    # Accept either the inline literal or a memory-named marker/helper
    # (e.g. a module constant or a `_memory_pressure(...)` predicate) - both
    # are a guard on the per-model reason.
    assert (
        "insufficient free memory" in local_block
        or re.search(r"[A-Za-z_]*memory[A-Za-z_]*", local_block, re.IGNORECASE)
    ), (
        "the local `if tag:` block must skip interrupt_story() when the "
        "story's own resource_status reason mentions insufficient free "
        "memory; the guard is missing from that block"
    )
