"""The deployment dispatch hold in the scheduler tick (advance_pipeline).

While ``dispatch_hold`` is on, the tick dispatches no ready story and
redispatches no interrupted or changes_requested story on any plan. Stories
already past dispatch keep moving: review still runs. Nothing is interrupted
by the hold itself. The hold is read through pipeline.dispatch_hold, so a read
that raises counts as held and warns.
"""

import json
import logging

import pipeline.advance as advance_module
from pipeline.store_records import DEFAULT_NAMESPACE

_PLAN = "hold-plan"
_HOLD_KEY = "dispatch_hold"


class _Records:
    def __init__(self, store, settings):
        self._store = store
        self._settings = settings

    def list_plans(self, namespace):
        return [_PLAN] if self._store.path.exists() else []

    def get_manifest(self, namespace, plan_name):
        return self._store.get_manifest()

    def put_manifest(self, namespace, plan_name, manifest):
        self._store.put_manifest(namespace, plan_name, manifest)

    def get_manifests(self, namespace):
        return {}

    def get_settings(self, namespace, scope, scope_id, repo):
        if isinstance(self._settings, Exception):
            raise self._settings
        if scope != "deployment" or namespace != DEFAULT_NAMESPACE:
            return None
        return self._settings


class _FakeStore:
    """The tick's _store seam, backed by a real manifest file."""

    def __init__(self, path, settings):
        self.path = path
        self._records = _Records(self, settings)

    def get_manifest(self, *args):
        return json.loads(self.path.read_text())

    def put_manifest(self, namespace, plan_name, manifest):
        self.path.write_text(json.dumps(manifest))

    def records(self):
        return self._records


class _FakeBackend:
    def get_backend(self, *_args, **_kwargs):
        return self

    def resource_status(self, **_kwargs):
        return {"ok": True}


class _FakeAutonomy:
    def _value(self):
        return "gated"


def _hold_settings(value):
    return {"values": {_HOLD_KEY: value}, "allow_loosen": [], "version": 1}


def _seed(monkeypatch, tmp_path, stories, settings):
    manifest = {"name": _PLAN, "paused": False, "approved": True, "stories": stories}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    store = _FakeStore(path, settings)

    monkeypatch.setattr(advance_module, "_store", store)
    monkeypatch.setattr(advance_module, "backend", _FakeBackend())
    monkeypatch.setattr(advance_module, "_role_resource_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(advance_module, "_count_on_device_in_progress_agents", lambda: 0)
    monkeypatch.setattr(advance_module, "PIPELINE_AUTONOMY", _FakeAutonomy(), raising=True)
    monkeypatch.setattr(advance_module, "_adjudicate_merges", lambda *a, **k: None)
    monkeypatch.setattr(advance_module, "_notify_user", lambda *a, **k: None)
    monkeypatch.setattr(advance_module, "interrupt_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module, "check_story_status", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(advance_module.plan_approval, "is_activatable", lambda m: True)

    dispatched = []
    reviewed = []

    def _fake_dispatch(plan_name, key):
        dispatched.append(key)
        return {"ok": True}

    monkeypatch.setattr(advance_module, "dispatch_story", _fake_dispatch)
    monkeypatch.setattr(advance_module, "review_story", lambda plan, key, *a, **k: reviewed.append(key) or {"ok": True, "status": "pr_open"})
    return dispatched, reviewed


def _tick():
    return advance_module._advance_pipeline_locked_impl(_PLAN)


def _story(key, status, **extra):
    return {"key": key, "title": f"story {key}", "status": status, "risk": "low", "dispatch_attempts": 0, **extra}


def test_hold_on_dispatches_no_ready_story(monkeypatch, tmp_path):
    dispatched, _ = _seed(monkeypatch, tmp_path, {"S1": _story("S1", "todo")}, _hold_settings(True))
    _tick()
    assert dispatched == []


def test_hold_on_does_not_redispatch_an_interrupted_story(monkeypatch, tmp_path):
    dispatched, _ = _seed(
        monkeypatch,
        tmp_path,
        {"S1": _story("S1", "interrupted"), "S2": _story("S2", "changes_requested")},
        _hold_settings(True),
    )
    _tick()
    assert dispatched == []


def test_hold_on_still_reviews_a_story_already_past_dispatch(monkeypatch, tmp_path):
    _, reviewed = _seed(monkeypatch, tmp_path, {"S1": _story("S1", "tests_passed")}, _hold_settings(True))
    _tick()
    assert reviewed == ["S1"]


def test_hold_on_reports_dispatch_paused_with_the_hold_reason(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, {"S1": _story("S1", "todo")}, _hold_settings(True))
    result = _tick()
    assert result["dispatch_paused"] is True
    assert result["dispatch_pause_reason"] == "dispatch_hold"


def test_hold_off_dispatches_a_ready_story(monkeypatch, tmp_path):
    dispatched, _ = _seed(monkeypatch, tmp_path, {"S1": _story("S1", "todo")}, _hold_settings(False))
    _tick()
    assert dispatched == ["S1"]


def test_hold_off_reports_no_hold_reason(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, {"S1": _story("S1", "todo")}, _hold_settings(False))
    result = _tick()
    assert result["dispatch_paused"] is False
    assert "dispatch_pause_reason" not in result


def test_an_unreadable_hold_is_treated_as_held_and_warns(monkeypatch, tmp_path, caplog):
    dispatched, _ = _seed(
        monkeypatch, tmp_path, {"S1": _story("S1", "todo")}, OSError("store read failed")
    )
    with caplog.at_level(logging.WARNING, logger="pipeline.dispatch_hold"):
        result = _tick()
    assert dispatched == []
    assert result["dispatch_pause_reason"] == "dispatch_hold"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) >= 1
