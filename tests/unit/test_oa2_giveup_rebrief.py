"""OA2-08: route the ``give_up`` failure through the rebrief diagnosis path.

Background
----------
``pipeline/advance.py``'s in-progress poll loop classifies a finished dispatch
via ``check_story_status``.  When the agent explicitly surrendered
(``failure_kind == "give_up"``) the tick used to be *notify-only*: it told a
human the story looked under-specified and moved on, leaving the next dispatch
to carry the exact same brief that just failed.

The step-cap path already solves this: ``pipeline/story_status.py`` calls
``_rebrief_step_cap_struggle`` (pipeline/dispatch.py), which runs the
``pipeline/rebrief.py`` machinery -- ``detect_unsatisfiable_signal`` +
``diagnose_failure`` + ``compose_rebriefed_instructions`` -- and folds the
diagnosis into ``story["agent_instructions"]`` (the ``DIAGNOSIS_HEADER``
block).  That rewritten brief is the marker the step-cap path leaves behind.

What is graded here
-------------------
These tests drive the REAL tick (``_advance_pipeline_locked_impl``) with
``check_story_status`` stubbed to report a give_up, so they prove the *wiring*
in ``pipeline/advance.py`` rather than the existence of a helper.  The main
test asserts the SAME marker the step-cap path sets (``DIAGNOSIS_HEADER`` in
``story["agent_instructions"]``, read back from the manifest on disk), so it
fails while ``pipeline.rebrief`` is not imported/called from ``advance.py``.

Coverage:

* positive -- give_up on an in_progress story rebriefs it (marker persisted);
* fallback -- a diagnosis that raises leaves the notify-only behaviour intact
  (no crash, notification still emitted, no marker);
* boundary -- give_up on an already-terminal story is a no-op (no diagnosis);
* negative -- a non-give_up failure does not rebrief.

No real backend, git repo or network is ever contacted.
"""

import json

import pipeline.advance as adv
from pipeline.rebrief import DIAGNOSIS_HEADER

PLAN = "oa2-giveup-plan"
KEY = "S1"


# ---------------------------------------------------------------------------
# Tick seams
# ---------------------------------------------------------------------------


class _FakeStore:
    """Backs the tick's ``_store`` seam with a real JSON file on disk."""

    def __init__(self, path):
        self.path = path

    def manifest_path(self, plan_name):
        return self.path

    def get_manifest(self, plan_name):
        return json.loads(self.path.read_text())


class _FakeBackend:
    """Per-story resource gate always reports healthy."""

    def get_backend(self, *_args, **_kwargs):
        return self

    def resource_status(self, **_kwargs):
        return {"ok": True}


class _FakeAutonomy:
    """Stands in for the PIPELINE_AUTONOMY _ServerRef proxy."""

    def __init__(self, value="gated"):
        self._value_ = value

    def _value(self):
        return self._value_


def _story(status="in_progress", **over):
    story = {
        "key": KEY,
        "title": "story",
        "summary": "story summary",
        "status": status,
        "risk": "low",
        "backend": "claude",
        "pid": 4242,
        "worktree": "/nonexistent-oa2-giveup-worktree",
        "agent_instructions": "ORIGINAL BRIEF",
        "dependencies": [],
    }
    story.update(over)
    return story


def _wire(monkeypatch, tmp_path, story, check_result, *, notify_calls, status_spy):
    """Write a one-story manifest and stub every seam the tick touches.

    ``check_story_status`` is stubbed to (a) record that it ran and (b) write
    the manifest exactly like the real one does -- status ``failed`` plus the
    ``failure_kind`` -- so the tick's own persistence (if any) is exercised
    against a manifest that already moved on from ``in_progress``.
    """
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"name": PLAN, "paused": False, "stories": {KEY: story}}))

    def _fake_check(plan_name, key):
        status_spy.append(key)
        manifest = json.loads(path.read_text())
        manifest["stories"][key]["status"] = "failed"
        manifest["stories"][key]["failure_kind"] = check_result.get("failure_kind")
        path.write_text(json.dumps(manifest))
        return dict(check_result)

    monkeypatch.setattr(adv, "_store", _FakeStore(path))
    monkeypatch.setattr(adv, "backend", _FakeBackend())
    monkeypatch.setattr(adv, "_role_resource_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(adv, "_count_on_device_in_progress_agents", lambda: 0)
    monkeypatch.setattr(adv, "PIPELINE_AUTONOMY", _FakeAutonomy("gated"))
    monkeypatch.setattr(adv, "_adjudicate_merges", lambda *a, **k: None)
    monkeypatch.setattr(adv, "interrupt_story", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(adv, "check_story_status", _fake_check)
    monkeypatch.setattr(
        adv,
        "_notify_user",
        lambda plan_name, message, **kwargs: notify_calls.append(
            {"plan_name": plan_name, "message": message, "kwargs": kwargs}
        ),
    )
    return path


def _read_story(path):
    return json.loads(path.read_text())["stories"][KEY]


def _gave_up_notifications(notify_calls):
    return [c for c in notify_calls if c["kwargs"].get("event") == "agent_gave_up"]


# ---------------------------------------------------------------------------
# Positive: the give_up path rebriefs the story
# ---------------------------------------------------------------------------


def test_give_up_on_in_progress_story_rebriefs_it(monkeypatch, tmp_path):
    """A give_up must run the diagnosis and leave the rewritten brief on the
    story -- the same DIAGNOSIS_HEADER marker the step-cap path produces."""
    notify_calls = []
    status_spy = []
    path = _wire(
        monkeypatch,
        tmp_path,
        _story(),
        {"status": "failed", "failure_kind": "give_up"},
        notify_calls=notify_calls,
        status_spy=status_spy,
    )
    monkeypatch.setattr(
        adv,
        "diagnose_failure",
        lambda evidence, story, plan_role_config=None: "missing API X; add it",
        raising=False,
    )

    adv._advance_pipeline_locked_impl(PLAN)

    story = _read_story(path)
    assert DIAGNOSIS_HEADER in story["agent_instructions"], (
        "give_up must fold a rebrief diagnosis into agent_instructions"
    )
    assert "missing API X; add it" in story["agent_instructions"]
    # The tick must persist onto the CURRENT manifest, not clobber the status
    # check_story_status already wrote.
    assert story["status"] == "failed"
    assert len(_gave_up_notifications(notify_calls)) == 1


# ---------------------------------------------------------------------------
# Fallback: a failing diagnosis degrades to notify-only
# ---------------------------------------------------------------------------


def test_diagnosis_failure_falls_back_to_notify_only(monkeypatch, tmp_path):
    """A diagnosis that raises must not crash the tick and must not stamp a
    partial marker -- today's notify-only behaviour is the fallback."""
    notify_calls = []
    status_spy = []
    path = _wire(
        monkeypatch,
        tmp_path,
        _story(),
        {"status": "failed", "failure_kind": "give_up"},
        notify_calls=notify_calls,
        status_spy=status_spy,
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("no signal")

    monkeypatch.setattr(adv, "diagnose_failure", _boom, raising=False)

    adv._advance_pipeline_locked_impl(PLAN)  # must not raise

    story = _read_story(path)
    assert DIAGNOSIS_HEADER not in story["agent_instructions"]
    assert story["agent_instructions"] == "ORIGINAL BRIEF"
    assert story["status"] == "failed"
    assert len(_gave_up_notifications(notify_calls)) == 1


# ---------------------------------------------------------------------------
# Boundary: give_up on a terminal story is a no-op
# ---------------------------------------------------------------------------


def test_give_up_on_terminal_story_is_a_noop(monkeypatch, tmp_path):
    """A terminal story is never polled, so no diagnosis runs and no marker is
    stamped."""
    notify_calls = []
    status_spy = []
    path = _wire(
        monkeypatch,
        tmp_path,
        _story(status="done"),
        {"status": "failed", "failure_kind": "give_up"},
        notify_calls=notify_calls,
        status_spy=status_spy,
    )
    diagnosis_spy = []
    monkeypatch.setattr(
        adv,
        "diagnose_failure",
        lambda *a, **k: diagnosis_spy.append(a) or "should not run",
        raising=False,
    )

    adv._advance_pipeline_locked_impl(PLAN)

    story = _read_story(path)
    assert status_spy == []
    assert diagnosis_spy == []
    assert DIAGNOSIS_HEADER not in story["agent_instructions"]
    assert story["status"] == "done"
    assert _gave_up_notifications(notify_calls) == []


# ---------------------------------------------------------------------------
# Negative: a non-give_up failure does not rebrief
# ---------------------------------------------------------------------------


def test_non_give_up_failure_does_not_rebrief(monkeypatch, tmp_path):
    """An ordinary red-test failure must not touch the brief."""
    notify_calls = []
    status_spy = []
    path = _wire(
        monkeypatch,
        tmp_path,
        _story(),
        {"status": "failed", "failure_kind": "tests"},
        notify_calls=notify_calls,
        status_spy=status_spy,
    )
    diagnosis_spy = []
    monkeypatch.setattr(
        adv,
        "diagnose_failure",
        lambda *a, **k: diagnosis_spy.append(a) or "should not run",
        raising=False,
    )

    adv._advance_pipeline_locked_impl(PLAN)

    story = _read_story(path)
    assert status_spy == [KEY]
    assert diagnosis_spy == []
    assert DIAGNOSIS_HEADER not in story["agent_instructions"]
    assert story["agent_instructions"] == "ORIGINAL BRIEF"
    assert _gave_up_notifications(notify_calls) == []
