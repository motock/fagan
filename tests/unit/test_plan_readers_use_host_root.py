"""Plan-manifest readers no longer pass their own PLAN_DIR binding.

``pipeline.checkpoint``, ``pipeline.event_wiring`` and ``pipeline.tick_checks``
used to hand their ``PLAN_DIR`` binding to the shared manifest readers. They now
omit ``plans_root`` so :mod:`pipeline.manifest_source` resolves the host-state
root itself, which is the same directory the binding named.
"""

from __future__ import annotations

import pytest

from pipeline import checkpoint, event_wiring, manifest_source, tick_checks
from pipeline import server as p
from pipeline.store import FileStore


@pytest.fixture
def spy_manifest_reads(monkeypatch):
    calls = []

    def spy(plan_name, *, fallback_on_error, plans_root=None):
        calls.append({"plans_root": plans_root, "fallback_on_error": fallback_on_error})

    monkeypatch.setattr(manifest_source, "read_plan_manifest", spy)
    return calls


@pytest.fixture
def spy_plan_names(monkeypatch):
    calls = []

    def spy(*, fallback_on_error, plans_root=None):
        calls.append({"plans_root": plans_root, "fallback_on_error": fallback_on_error})
        return ["first-plan"]

    monkeypatch.setattr(manifest_source, "read_plan_names", spy)
    return calls


def test_checkpoint_manifest_read_leaves_the_root_to_the_host_state(
    spy_manifest_reads, tmp_path, monkeypatch
):
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)

    with pytest.raises(FileNotFoundError):
        checkpoint._plan_manifest("missing")

    assert spy_manifest_reads == [{"plans_root": None, "fallback_on_error": False}]


def test_checkpoint_missing_manifest_error_names_the_host_state_root(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path)
    monkeypatch.setattr(p, "_store", FileStore())

    with pytest.raises(FileNotFoundError) as excinfo:
        checkpoint._plan_manifest("missing")

    assert str(excinfo.value) == str(tmp_path / "missing.manifest.json")


def test_wake_handler_manifest_read_leaves_the_root_to_the_host_state(
    spy_manifest_reads,
):
    result = event_wiring.wake_handler(
        {"plan": "plan-a", "story_key": "s-1", "type": "story_done"}
    )

    assert result == {"ok": False, "skipped": "no_manifest"}
    assert spy_manifest_reads == [{"plans_root": None, "fallback_on_error": True}]


def test_first_plan_attribution_leaves_the_root_to_the_host_state(spy_plan_names):
    assert tick_checks._first_plan_name() == "first-plan"
    assert spy_plan_names == [{"plans_root": None, "fallback_on_error": True}]


def test_worktree_patch_refuses_a_target_inside_the_host_state_root(
    monkeypatch, tmp_path
):
    """The forbidden-root check reads the host state's root, not a server binding."""
    from pipeline import host_state, worktree_patch

    host_plans = (tmp_path / "host-plans").resolve()
    host_plans.mkdir()

    class _Host:
        def root(self):
            return host_plans

    monkeypatch.setattr(host_state, "build_host_state", lambda: _Host())
    monkeypatch.setattr(p, "PLAN_DIR", tmp_path / "server-plans")

    with pytest.raises(worktree_patch.PatchSecurityError):
        worktree_patch.resolve_write_target(str(host_plans), "demo.manifest.json")
