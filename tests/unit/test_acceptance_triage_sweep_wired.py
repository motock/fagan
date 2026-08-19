"""Acceptance oracle: the triage sweep is wired into the scheduler tick.

A unit test of `run_triage_sweep` passes whether or not anything ever calls it,
so it would grade a helper sitting unconnected. This drives the real production
entrypoint `_advance_pipeline_locked` (what `advance_pipeline` /
`advance_all_plans` reach) and asserts (a) the sweep runs, (b) it runs before
the tick body, and (c) C2's fail-open holds at the wiring layer: a raising
sweep must leave the tick's result exactly as it was.
"""

import json

import pipeline.server as p


def _write_manifest(plan_dir, **extra):
    manifest = {"epics": {}, "stories": {}}
    manifest.update(extra)
    (plan_dir / "tri.manifest.json").write_text(json.dumps(manifest))


def _stub_tick_boundaries(monkeypatch):
    monkeypatch.setattr(p, "PIPELINE_AUTONOMY", "gated")
    monkeypatch.setattr(p, "_role_resource_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)


def test_tick_invokes_the_triage_sweep_with_the_plan_name(plan_dir, monkeypatch):
    seen = []
    monkeypatch.setattr(p, "run_triage_sweep", lambda plan_name: seen.append(plan_name))
    _stub_tick_boundaries(monkeypatch)
    _write_manifest(plan_dir)

    p._advance_pipeline_locked("tri")

    assert seen == ["tri"]


def test_sweep_runs_before_the_tick_body(plan_dir, monkeypatch):
    order = []

    def fake_sweep(plan_name):
        order.append("sweep")

    def fake_tick(plan_name):
        order.append("tick")
        return {"ok": True}

    monkeypatch.setattr(p, "run_triage_sweep", fake_sweep)
    monkeypatch.setattr(p, "_advance_pipeline_locked_impl", fake_tick)
    _write_manifest(plan_dir)

    p._advance_pipeline_locked("tri")

    assert order == ["sweep", "tick"]


def test_a_raising_sweep_does_not_break_the_tick(plan_dir, monkeypatch):
    def boom(plan_name):
        raise RuntimeError("overlord transport blew up")

    monkeypatch.setattr(p, "run_triage_sweep", boom)
    _stub_tick_boundaries(monkeypatch)
    _write_manifest(plan_dir)

    result = p._advance_pipeline_locked("tri")

    assert result["ok"] is True


def test_a_raising_sweep_still_runs_the_tick_body(plan_dir, monkeypatch):
    ran = []

    def boom(plan_name):
        raise RuntimeError("overlord transport blew up")

    def fake_tick(plan_name):
        ran.append(plan_name)
        return {"ok": True}

    monkeypatch.setattr(p, "run_triage_sweep", boom)
    monkeypatch.setattr(p, "_advance_pipeline_locked_impl", fake_tick)
    _write_manifest(plan_dir)

    p._advance_pipeline_locked("tri")

    assert ran == ["tri"]
