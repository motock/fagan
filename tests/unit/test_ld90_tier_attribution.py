"""MFR-02: tier attribution, the tag-only population gap and per-story reasons.

``pipeline/local_success.py::classify_story`` decides two things from the
story's ``backend`` / ``dispatched_model`` / ``model`` fields: whether the story
is in the measured population, and which tier it is attributed to.  Escalation
overwrites exactly those fields with its target, so the classifier must read the
pre‑escalation stamp written by ``pipeline/escalation.py`` instead.

These tests pin three things:

* tier attribution from the pre‑escalation stamp – the local tier that actually
  failed keeps the story, not the escalation tier that was charged with it;
* the tag‑only population gap – a manifest written before ``backend`` was
  stamped still belongs to the population;
* reasons as a set of per‑story reasons, not one entry per matching record.

All fixtures are synthetic dicts; no real manifest or sidecar is read.  The
integration test drives the real ``pipeline.escalation`` stamping function so
the classifier is graded against what the producer actually writes.
"""

from __future__ import annotations

import os

import pipeline.local_success as mod
from pipeline import escalation as esc

RESULT_KEYS = {
    "story_key",
    "in_population",
    "tier",
    "dispatched_at",
    "clean",
    "reasons",
}

# Factory for stories with defaults

def _story(**overrides):
    story = {
        "story_key": "S1",
        "status": "done",
        "backend": "local",
        "model": "gpt-oss-20b-high",
        "dispatched_model": "gpt-oss-20b-high",
        "dispatched_at": "2026-09-18T10:00:00Z",
    }
    story.update(overrides)
    return story

# Helper to create records list

def _rec(**overrides):
    return dict(overrides)

# Helper to classify

def _classify(story, records=None):
    return mod.classify_story(story.get("story_key", "S1"), story, records or [])

# --------------------------------------------------------------------------
# Tier attribution: the pre‑escalation stamp wins
# --------------------------------------------------------------------------

def test_an_escalated_local_story_is_attributed_to_the_local_tier():
    story = _story(
        backend="claude",
        model="claude-sonnet",
        dispatched_model=None,
        escalated=True,
        pre_escalation_backend="local",
        pre_escalation_model="gpt-oss-20b-high",
    )
    out = _classify(story)
    assert out["tier"] == "on-device"
    assert out["in_population"] is True


def test_an_escalated_story_keeps_its_cloud_oss_tier():
    story = _story(
        backend="claude",
        model="claude-sonnet",
        escalated=True,
        pre_escalation_backend="ollama",
        pre_escalation_model="deepseek-v4-flash:cloud",
    )
    out = _classify(story)
    assert out["tier"] == "cloud-oss"
    assert out["in_population"] is True


def test_the_pre_escalation_model_beats_the_current_dispatched_model():
    story = _story(
        backend="claude",
        model="claude-sonnet",
        dispatched_model="claude-sonnet",
        pre_escalation_model="gpt-oss-20b-high",
    )
    out = _classify(story)
    assert out["tier"] == "on-device"


def test_without_a_stamp_the_old_attribution_is_unchanged():
    story = _story(backend="claude", model="claude-sonnet", dispatched_model=None)
    out = _classify(story)
    assert out["tier"] == "unknown"
    assert out["in_population"] is False

# --------------------------------------------------------------------------
# Integration: the real escalation function writes what the classifier reads
# --------------------------------------------------------------------------

def test_the_escalation_stamp_is_what_the_classifier_attributes(monkeypatch):
    monkeypatch.setattr(esc, "_notify_user", lambda *_, **__: None)
    os.environ["PIPELINE_ESCALATION_BACKEND"] = "claude"
    os.environ.pop("PIPELINE_ESCALATION_MODEL", None)
    story = _story(backend="local", model="qwen3-30b", dispatched_model="qwen3-30b")
    esc._escalate_review_to_claude(story, "S1", "plan", "budget exhausted")
    assert story["backend"] == "claude"
    out = _classify(story)
    assert out["tier"] == "on-device"
    assert out["in_population"] is True
    assert "escalated" in out["reasons"]

# --------------------------------------------------------------------------
# Population gap tests
# --------------------------------------------------------------------------

def test_a_story_with_no_backend_but_a_local_tag_is_in_population():
    story = _story(backend=None, dispatched_model=None, model="qwen3-30b")
    out = _classify(story)
    assert out["in_population"] is True
    assert out["tier"] == "on-device"


def test_a_story_with_no_backend_but_a_cloud_tag_is_cloud_oss():
    story = _story(backend=None, dispatched_model="glm-5.3-flash:cloud")
    out = _classify(story)
    assert out["in_population"] is True
    assert out["tier"] == "cloud-oss"


def test_a_claude_story_without_a_tag_or_escalation_stays_out():
    story = _story(backend="claude", dispatched_model=None, model=None)
    out = _classify(story)
    assert out["in_population"] is False
    assert out["tier"] == "unknown"


def test_a_story_with_no_backend_and_no_tag_stays_out():
    story = _story(backend="", dispatched_model=None, model=None)
    out = _classify(story)
    assert out["in_population"] is False
    assert out["tier"] == "unknown"

# --------------------------------------------------------------------------
# Reasons tests
# --------------------------------------------------------------------------

def test_repeated_records_of_one_event_are_one_reason():
    records = [_rec(event="story_parked", story_key="S1") for _ in range(3)]
    out = _classify(_story(status="parked"), records)
    assert out["reasons"] == ["not_done", "story_parked"]
    assert out["clean"] is False


def test_two_different_events_are_two_reasons():
    records = [_rec(event="story_parked", story_key="S1"),
               _rec(event="brief_patched", story_key="S1"),
               _rec(event="brief_patched", story_key="S1")]
    out = _classify(_story(), records)
    assert sorted(out["reasons"]) == ["brief_patched", "story_parked"]


def test_an_escalated_manifest_flag_is_a_reason_without_a_sidecar_event():
    story = _story(escalated=True, status="done")
    out = _classify(story)
    assert "escalated" in out["reasons"]
    assert out["clean"] is False


def test_an_escalation_reported_twice_is_counted_once():
    records = [_rec(event="escalated", story_key="S1")]
    story = _story(escalated=True, status="done")
    out = _classify(story, records)
    assert out["reasons"] == ["escalated"]


def test_reasons_stay_a_sorted_list_of_unique_strings():
    out = _classify(_story(), [])
    assert isinstance(out["reasons"], list)
    assert out["reasons"] == sorted(out["reasons"])
    assert len(out["reasons"]) == len(set(out["reasons"]))


def test_reasons_are_empty_if_and_only_if_clean():
    # Clean story
    out = _classify(_story(), [])
    assert out["reasons"] == []
    assert out["clean"] is True
    # Not clean story
    out = _classify(_story(status="parked"), [])
    assert out["reasons"] != []
    assert out["clean"] is False


def test_the_aggregated_reason_count_counts_stories_not_records():
    # One parked local story with three park records
    records = [_rec(event="story_parked", story_key="S1") for _ in range(3)]
    out = _classify(_story(), records)
    classified = [out]
    agg = mod.rolling_rate(classified, window=0)
    assert agg["reasons"]["story_parked"] == 1


def test_the_result_shape_is_unchanged():
    out = _classify(_story(), [])
    assert set(out) == RESULT_KEYS
