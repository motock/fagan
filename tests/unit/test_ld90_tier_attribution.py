"""MFR-02: tier attribution, the tag-only population gap and per-story reasons.

``pipeline/local_success.py::classify_story`` decides two things from the
story's ``backend`` / ``dispatched_model`` / ``model`` fields: whether the story
is in the measured population, and which tier it is attributed to.  Escalation
overwrites exactly those fields with its target, so the classifier must read the
pre-escalation stamp written by ``pipeline/escalation.py`` instead.

These tests pin three things:

* tier attribution from the pre-escalation stamp - the local tier that actually
  failed keeps the story, not the escalation tier that was charged with it;
* the tag-only population gap - a manifest written before ``backend`` was
  stamped still belongs to the population;
* reasons as a set of per-story reasons, not one entry per matching record.

All fixtures are synthetic dicts; no real manifest or sidecar is read.  The
integration test drives the real ``pipeline.escalation`` stamping function so
the classifier is graded against what the producer actually writes.
"""

from __future__ import annotations

import pathlib

import pytest

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

MODULE_SOURCE = pathlib.Path(mod.__file__).read_text(encoding="utf-8")


def _story(**overrides):
    """A done, on-device story that is in population and clean by default."""
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


def _rec(**overrides):
    return dict(overrides)


def _classify(story, records=None):
    return mod.classify_story(story.get("story_key", "S1"), story, records or [])


# --------------------------------------------------------------------------
# Tier attribution: the pre-escalation stamp wins
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
        dispatched_model="claude-sonnet",
        model="claude-sonnet",
        pre_escalation_model="gpt-oss-20b-high",
    )
    out = _classify(story)
    assert out["tier"] == "on-device"


def test_the_pre_escalation_model_beats_a_cloud_current_tag():
    """The discriminating case: the current tag alone would say cloud-oss."""
    story = _story(
        backend="claude",
        model="claude-sonnet:cloud",
        dispatched_model="claude-sonnet:cloud",
        escalated=True,
        pre_escalation_backend="local",
        pre_escalation_model="gpt-oss-20b-high",
    )
    out = _classify(story)
    assert out["tier"] == "on-device"
    assert out["in_population"] is True


def test_without_a_stamp_the_old_attribution_is_unchanged():
    story = _story(backend="claude", model="claude-sonnet", dispatched_model=None)
    out = _classify(story)
    assert out["tier"] == "unknown"
    assert out["in_population"] is False


# --------------------------------------------------------------------------
# Integration: the real escalation function writes what the classifier reads
# --------------------------------------------------------------------------
def test_the_escalation_stamp_is_what_the_classifier_attributes(monkeypatch):
    monkeypatch.setattr(esc, "_notify_user", lambda *a, **k: None)
    monkeypatch.setenv("PIPELINE_ESCALATION_BACKEND", "claude")
    monkeypatch.delenv("PIPELINE_ESCALATION_MODEL", raising=False)

    story = _story()
    esc._escalate_review_to_claude(story, "S1", "plan", "budget exhausted")

    # Sanity: escalation really did retarget the live fields.
    assert story["backend"] == "claude"

    out = mod.classify_story("S1", story, [])
    assert out["tier"] == "on-device"
    assert out["in_population"] is True
    assert "escalated" in out["reasons"]


# --------------------------------------------------------------------------
# Population gap: a tag-only manifest still belongs to the population
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
    story = _story(backend="", model=None, dispatched_model=None)
    out = _classify(story)
    assert out["in_population"] is False
    assert out["tier"] == "unknown"


# --------------------------------------------------------------------------
# Reasons: one per story, not one per record
# --------------------------------------------------------------------------
def test_repeated_records_of_one_event_are_one_reason():
    story = _story(status="parked")
    records = [_rec(event="story_parked", story_key="S1") for _ in range(3)]
    out = _classify(story, records)
    assert out["reasons"] == ["not_done", "story_parked"]
    assert out["clean"] is False


def test_two_different_events_are_two_reasons():
    story = _story()
    records = [
        _rec(event="story_parked", story_key="S1"),
        _rec(event="brief_patched", story_key="S1"),
        _rec(event="brief_patched", story_key="S1"),
    ]
    out = _classify(story, records)
    assert out["reasons"] == ["brief_patched", "story_parked"]


def test_an_escalated_manifest_flag_is_a_reason_without_a_sidecar_event():
    story = _story(escalated=True)
    out = _classify(story, [])
    assert "escalated" in out["reasons"]
    assert out["clean"] is False


def test_an_escalation_reported_twice_is_counted_once():
    story = _story(escalated=True)
    records = [_rec(event="escalated", story_key="S1")]
    out = _classify(story, records)
    assert out["reasons"] == ["escalated"]


def test_reasons_stay_a_sorted_list_of_unique_strings():
    story = _story(
        status="parked",
        escalated=True,
        agent_instructions="REWORK SCOPE: x",
    )
    records = [
        _rec(event="story_parked", story_key="S1"),
        _rec(event="story_parked", story_key="S1"),
        _rec(event="brief_patched", story_key="S1"),
        _rec(event=None, message="S1 parked by triage", story_key="S1"),
    ]
    out = _classify(story, records)
    reasons = out["reasons"]
    assert isinstance(reasons, list)
    assert reasons == sorted(reasons)
    assert len(reasons) == len(set(reasons))
    assert {"not_done", "escalated", "story_parked", "brief_patched", "brief_rewrite_marker"} <= set(
        reasons
    )


@pytest.mark.parametrize(
    "story,records",
    [
        (_story(), []),
        (_story(status="parked"), []),
        (_story(escalated=True), []),
        (
            _story(),
            [_rec(event=None, message="S1 parked by triage", story_key="S1")],
        ),
    ],
)
def test_reasons_are_empty_if_and_only_if_clean(story, records):
    out = mod.classify_story("S1", story, records)
    assert (out["reasons"] == []) is out["clean"]


def test_the_aggregated_reason_count_counts_stories_not_records():
    story = _story(status="parked")
    records = [_rec(event="story_parked", story_key="S1") for _ in range(3)]
    classified = [mod.classify_story("S1", story, records)]
    rate = mod.rolling_rate(classified, window=0)
    assert rate["reasons"]["story_parked"] == 1
    assert rate["reasons"]["not_done"] == 1


def test_the_result_shape_is_unchanged():
    out = _classify(_story())
    assert set(out) == RESULT_KEYS


# --------------------------------------------------------------------------
# Boundary / malformed input
# --------------------------------------------------------------------------
def test_a_story_with_no_fields_at_all_is_not_done_and_out_of_population():
    out = mod.classify_story("S1", {}, [])
    assert out["in_population"] is False
    assert out["tier"] == "unknown"
    assert out["dispatched_at"] is None
    assert out["reasons"] == ["not_done"]
    assert out["clean"] is False


def test_non_dict_records_are_ignored():
    story = _story()
    out = mod.classify_story("S1", story, [None, "nope", 7, _rec(event="story_parked", story_key="S1")])
    assert out["reasons"] == ["story_parked"]


def test_an_empty_backend_with_a_cloud_tag_is_cloud_oss():
    story = _story(backend="", dispatched_model="glm-5.3-flash:cloud")
    out = _classify(story)
    assert out["in_population"] is True
    assert out["tier"] == "cloud-oss"


# --------------------------------------------------------------------------
# Source-level requirements of this change
# --------------------------------------------------------------------------
def test_the_docstring_documents_one_reason_per_story():
    assert "each reason appearing at most once" in MODULE_SOURCE
    assert "sorted list of strings; it is empty iff" not in MODULE_SOURCE


def test_the_population_reads_the_pre_escalation_stamp():
    assert 'story.get("pre_escalation_backend")' in MODULE_SOURCE
    assert 'story.get("pre_escalation_model")' in MODULE_SOURCE
    assert 'backend = story.get("backend")' not in MODULE_SOURCE


def test_the_population_has_the_tag_only_clause():
    assert "not backend and bool(tag)" in MODULE_SOURCE
    assert (
        'in_population = bool(backend and backend != "claude") or escalated_flag '
        "or any_escalated_event" not in MODULE_SOURCE
    )


def test_the_tier_block_no_longer_recomputes_the_tag():
    assert 'tag = story.get("dispatched_model") or story.get("model") or ""' not in MODULE_SOURCE
    assert "elif tag:" in MODULE_SOURCE


def test_reasons_are_accumulated_in_a_set():
    assert "reasons: set[str] = set()" in MODULE_SOURCE
    assert "reasons: list[str] = []" not in MODULE_SOURCE
    assert "reasons.append(" not in MODULE_SOURCE
    assert "reasons.sort()" not in MODULE_SOURCE
    assert '"reasons": sorted(reasons)' in MODULE_SOURCE
