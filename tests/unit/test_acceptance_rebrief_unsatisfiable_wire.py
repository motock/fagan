"""Acceptance oracle: the step-cap rebrief must surface unsatisfiable signals.

``_rebrief_step_cap_struggle`` (pipeline/server.py) is the routine that runs
when an implementer hits the step cap. CLAUDE.md Step 9 requires it to fold a
diagnosis into ``agent_instructions`` so the resume is not a blind retry. On
top of that, when the collected evidence contains an "unsatisfiable as
specified" signal (e.g. ``got an unexpected keyword argument``), the routine
must notify the user via ``_notify_user`` that the story may be unsatisfiable
and may need re-planning rather than another retry.

These tests drive ``_rebrief_step_cap_struggle`` ITSELF - they monkeypatch only
the real external boundary (the evidence/diagnosis collectors and
``_notify_user``), never the detector ``detect_unsatisfiable_signal``. A test
that only called the detector directly would pass even with the wiring
missing, so it does not satisfy this story.

CRITICAL - FAIL OPEN: the notification addition must never block, raise, or
alter the existing rebrief behaviour. The diagnosis/facts folding must still
happen exactly as before whether or not the signal fires, and even when the
detector itself raises.
"""
import inspect

from pipeline import server as p

# ---------------------------------------------------------------------------
# Wiring contract: the import and the call must exist.
# ---------------------------------------------------------------------------

def test_detect_unsatisfiable_signal_is_imported_into_server():
    """The name must be imported from pipeline.rebrief into pipeline.server."""
    src = inspect.getsource(p)
    # Must be imported from the rebrief module (not defined inline).
    assert "detect_unsatisfiable_signal" in src, (
        "pipeline.server must reference detect_unsatisfiable_signal"
    )
    # It must come through the rebrief import, re-exported as a module attr.
    assert hasattr(p, "detect_unsatisfiable_signal"), (
        "pipeline.server must expose detect_unsatisfiable_signal as an attribute "
        "(import it from pipeline.rebrief)"
    )


def test_rebrief_step_cap_struggle_calls_detect_unsatisfiable_signal():
    """The body of _rebrief_step_cap_struggle must invoke the detector."""
    src = inspect.getsource(p._rebrief_step_cap_struggle)
    assert "detect_unsatisfiable_signal" in src, (
        "_rebrief_step_cap_struggle must call detect_unsatisfiable_signal"
    )


def test_rebrief_step_cap_struggle_calls_notify_user():
    """The body must call _notify_user when the signal fires."""
    src = inspect.getsource(p._rebrief_step_cap_struggle)
    assert "_notify_user" in src, (
        "_rebrief_step_cap_struggle must call _notify_user"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_collectors(monkeypatch, evidence):
    """Patch the evidence/diagnosis collectors at the real external boundary."""
    monkeypatch.setattr(p, "collect_attempt_facts", lambda *a, **k: "facts block")
    monkeypatch.setattr(p, "collect_failure_evidence", lambda *a, **k: evidence)
    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: "root cause here")


class _CallRecorder:
    """Records _notify_user calls so tests can assert on them."""

    def __init__(self):
        self.calls = []

    def __call__(self, plan_name, message, **kwargs):
        self.calls.append((plan_name, message, kwargs))


# ---------------------------------------------------------------------------
# 1. POSITIVE: unsatisfiable evidence -> _notify_user called once, mentions key
# ---------------------------------------------------------------------------

def test_positive_unsatisfiable_signal_notifies_user(monkeypatch, tmp_path):
    evidence = "TypeError: got an unexpected keyword argument 'foo'"
    _patch_collectors(monkeypatch, evidence)

    recorder = _CallRecorder()
    monkeypatch.setattr(p, "_notify_user", recorder)

    story = {"agent_instructions": "GOAL: x"}
    p._rebrief_step_cap_struggle(story, str(tmp_path), plan_name="myplan",
                                story_key="story-7")

    unsat_calls = [c for c in recorder.calls if "unsatisfiable" in c[1].lower()]
    assert len(unsat_calls) == 1, (
        f"_notify_user should be called exactly once for unsatisfiable evidence, "
        f"got {len(unsat_calls)} unsatisfiable notices"
    )
    plan_name, message, _kwargs = unsat_calls[0]
    assert plan_name == "myplan"
    # The message must name the story key.
    assert "story-7" in message, (
        f"notification message must mention the story key, got: {message!r}"
    )
    # The message must convey the unsatisfiable reason / re-planning guidance.
    assert "unsatisfiable" in message.lower(), (
        f"notification message must state the story may be unsatisfiable, got: {message!r}"
    )


# ---------------------------------------------------------------------------
# 2. NEGATIVE: ordinary failing-test evidence -> _notify_user NOT called
# ---------------------------------------------------------------------------

def test_negative_ordinary_failure_does_not_notify(monkeypatch, tmp_path):
    evidence = "AssertionError: expected 2 but got 1"
    _patch_collectors(monkeypatch, evidence)

    recorder = _CallRecorder()
    monkeypatch.setattr(p, "_notify_user", recorder)

    story = {"agent_instructions": "GOAL: x"}
    p._rebrief_step_cap_struggle(story, str(tmp_path), plan_name="myplan",
                                story_key="story-7")

    unsat_calls = [c for c in recorder.calls if "unsatisfiable" in c[1].lower()]
    assert unsat_calls == [], (
        f"_notify_user must NOT send an unsatisfiable notice for ordinary "
        f"failing-test evidence, got {unsat_calls}"
    )


# ---------------------------------------------------------------------------
# 3. NO REGRESSION: diagnosis/facts still folded in both cases
# ---------------------------------------------------------------------------

def test_positive_still_folds_diagnosis_and_facts(monkeypatch, tmp_path):
    evidence = "TypeError: got an unexpected keyword argument 'foo'"
    _patch_collectors(monkeypatch, evidence)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    story = {"agent_instructions": "GOAL: x"}
    p._rebrief_step_cap_struggle(story, str(tmp_path), plan_name="myplan",
                                story_key="story-7")

    assert "root cause here" in story["agent_instructions"], (
        "diagnosis must still be folded into agent_instructions when the signal fires"
    )
    assert "facts block" in story["agent_instructions"], (
        "facts must still be folded into agent_instructions when the signal fires"
    )
    assert "GOAL: x" in story["agent_instructions"], (
        "original brief must be preserved when the signal fires"
    )


def test_negative_still_folds_diagnosis_and_facts(monkeypatch, tmp_path):
    evidence = "AssertionError: expected 2 but got 1"
    _patch_collectors(monkeypatch, evidence)
    monkeypatch.setattr(p, "_notify_user", lambda *a, **k: None)

    story = {"agent_instructions": "GOAL: x"}
    p._rebrief_step_cap_struggle(story, str(tmp_path), plan_name="myplan",
                                story_key="story-7")

    assert "root cause here" in story["agent_instructions"], (
        "diagnosis must still be folded into agent_instructions for ordinary failures"
    )
    assert "facts block" in story["agent_instructions"], (
        "facts must still be folded into agent_instructions for ordinary failures"
    )
    assert "GOAL: x" in story["agent_instructions"], (
        "original brief must be preserved for ordinary failures"
    )


# ---------------------------------------------------------------------------
# 4. FAIL-OPEN: detector raises -> still completes and still folds diagnosis
# ---------------------------------------------------------------------------

def test_fail_open_detector_raises_still_completes(monkeypatch, tmp_path):
    evidence = "TypeError: got an unexpected keyword argument 'foo'"
    _patch_collectors(monkeypatch, evidence)

    def _boom(_evidence):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(p, "detect_unsatisfiable_signal", _boom)

    recorder = _CallRecorder()
    monkeypatch.setattr(p, "_notify_user", recorder)

    story = {"agent_instructions": "GOAL: x"}
    # Must not raise.
    p._rebrief_step_cap_struggle(story, str(tmp_path), plan_name="myplan",
                                story_key="story-7")

    # Diagnosis/facts still folded exactly as before.
    assert "root cause here" in story["agent_instructions"], (
        "diagnosis must still be folded when the detector raises"
    )
    assert "facts block" in story["agent_instructions"], (
        "facts must still be folded when the detector raises"
    )
    assert "GOAL: x" in story["agent_instructions"], (
        "original brief must be preserved when the detector raises"
    )
    # A raising detector must not trigger a (spurious) unsatisfiable notice.
    unsat_calls = [c for c in recorder.calls if "unsatisfiable" in c[1].lower()]
    assert unsat_calls == [], (
        "a raising detector must not trigger the unsatisfiable notice"
    )


# ---------------------------------------------------------------------------
# Call-site contract: the call site must thread plan_name and story_key.
# ---------------------------------------------------------------------------

def test_call_site_threads_plan_name_and_story_key():
    """The call site in check_story_status must pass plan_name and story_key."""
    src = inspect.getsource(p.check_story_status)
    assert "_rebrief_step_cap_struggle(" in src
    # The call must pass plan_name and story_key through (by keyword or position).
    assert "plan_name" in src, "call site must thread plan_name"
    assert "story_key" in src, "call site must thread story_key"


# ---------------------------------------------------------------------------
# 5. The rebrief must emit its own brief_patched record when the brief changes.
# ---------------------------------------------------------------------------

def test_brief_patched_record_is_emitted_when_the_brief_changes(monkeypatch, tmp_path):
    """A rewritten brief is a brief_patched event, attributed to its story."""
    evidence = "AssertionError: expected 2 but got 1"
    _patch_collectors(monkeypatch, evidence)

    recorder = _CallRecorder()
    monkeypatch.setattr(p, "_notify_user", recorder)

    story = {"agent_instructions": "GOAL: x", "correlation_id": "corr-1"}
    p._rebrief_step_cap_struggle(story, str(tmp_path), plan_name="myplan",
                                story_key="story-7")

    patched = [c for c in recorder.calls if c[2].get("event") == "brief_patched"]
    assert len(patched) == 1, (
        f"exactly one brief_patched record must be emitted when the brief "
        f"changes, got {patched}"
    )
    plan_name, _message, kwargs = patched[0]
    assert plan_name == "myplan"
    assert kwargs["story_key"] == "story-7"
    assert kwargs["correlation_id"] == "corr-1"


def test_no_brief_patched_record_when_the_rebrief_is_a_noop(monkeypatch, tmp_path):
    """A no-op diagnosis with no facts leaves the brief byte-identical."""
    monkeypatch.setattr(p, "collect_attempt_facts", lambda *a, **k: "")
    monkeypatch.setattr(
        p, "collect_failure_evidence",
        lambda *a, **k: "AssertionError: expected 2 but got 1")
    monkeypatch.setattr(p, "diagnose_failure", lambda *a, **k: None)

    recorder = _CallRecorder()
    monkeypatch.setattr(p, "_notify_user", recorder)

    story = {"agent_instructions": "GOAL: x"}
    p._rebrief_step_cap_struggle(story, str(tmp_path), plan_name="myplan",
                                story_key="story-7")

    assert story["agent_instructions"] == "GOAL: x", (
        "a no-op rebrief must leave the brief unchanged"
    )
    patched = [c for c in recorder.calls if c[2].get("event") == "brief_patched"]
    assert patched == [], (
        f"a no-op rebrief must not emit brief_patched, got {patched}"
    )


def test_brief_patched_record_omits_correlation_id_when_absent(monkeypatch, tmp_path):
    """No correlation_id on the story -> the key is absent, not None."""
    evidence = "AssertionError: expected 2 but got 1"
    _patch_collectors(monkeypatch, evidence)

    recorder = _CallRecorder()
    monkeypatch.setattr(p, "_notify_user", recorder)

    story = {"agent_instructions": "GOAL: x"}
    p._rebrief_step_cap_struggle(story, str(tmp_path), plan_name="myplan",
                                story_key="story-7")

    patched = [c for c in recorder.calls if c[2].get("event") == "brief_patched"]
    assert len(patched) == 1, f"expected one brief_patched record, got {patched}"
    kwargs = patched[0][2]
    assert "correlation_id" not in kwargs, (
        f"correlation_id must be absent (not None) when the story has none, "
        f"got {kwargs!r}"
    )


def test_unsatisfiable_notice_is_attributed_to_the_story(monkeypatch, tmp_path):
    """The unsatisfiable notice must carry story_key so it is attributable."""
    evidence = "TypeError: got an unexpected keyword argument 'foo'"
    _patch_collectors(monkeypatch, evidence)

    recorder = _CallRecorder()
    monkeypatch.setattr(p, "_notify_user", recorder)

    story = {"agent_instructions": "GOAL: x"}
    p._rebrief_step_cap_struggle(story, str(tmp_path), plan_name="myplan",
                                story_key="story-7")

    unsat_calls = [c for c in recorder.calls if "unsatisfiable" in c[1].lower()]
    assert len(unsat_calls) == 1, (
        f"expected exactly one unsatisfiable notice, got {unsat_calls}"
    )
    assert unsat_calls[0][2].get("story_key") == "story-7", (
        f"the unsatisfiable notice must be attributed to its story, "
        f"got kwargs {unsat_calls[0][2]!r}"
    )


def test_brief_patched_record_disqualifies_first_pass_clean():
    """Metrics must agree with the classifier: a rewritten brief is not clean."""
    from pipeline.story_metrics import compute_story_metrics

    merged_only = compute_story_metrics(
        [{"event": "story_merged", "story_key": "story-7"}])
    assert merged_only["story-7"]["first_pass_clean"] is True, (
        "a lone story_merged record is a clean first pass"
    )

    with_patch = compute_story_metrics([
        {"event": "story_merged", "story_key": "story-7"},
        {"event": "brief_patched", "story_key": "story-7"},
    ])
    payload = with_patch["story-7"]
    assert payload["first_pass_clean"] is False, (
        "a brief_patched record for the same story must disqualify first_pass_clean"
    )
    assert payload["disqualifying_events"] == 1