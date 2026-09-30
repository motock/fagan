"""RH-02: one event name means one cause.

``brief_patched`` used to fire from three sites with no cause discriminator:
the operator ``patch_story`` MCP tool, the automated step-cap rebrief in
``pipeline/dispatch_attempt.py`` and the automated give-up rebrief in
``pipeline/advance.py``.  The first-pass-clean reason histogram therefore
counted events, not story causes: one story could produce two "independent"
disqualifiers from a single brief rewrite (retro 2026-09-29 §2.3).

The two automated paths now emit cause-specific names -- ``step_cap_rebrief``
and ``give_up_rebrief`` -- and ``brief_patched`` is left to mean exactly the
operator patch.  These tests grade the deliverable: the classifier and the
metrics rollup must report each cause under its own name, and a story must not
be able to manufacture two disqualifiers out of one cause.
"""

from pathlib import Path

from pipeline.local_success import classify_story
from pipeline.story_metrics import compute_story_metrics

STORY_KEY = "S1"
CORRELATION = "corr-1"

STEP_CAP = "step_cap_rebrief"
GIVE_UP = "give_up_rebrief"
OPERATOR = "brief_patched"


def _story(**overrides):
    """A dispatched, finished local story with a clean brief."""
    story = {
        "status": "done",
        "backend": "local",
        "dispatched_at": "2026-09-18T10:00:00Z",
        "correlation_id": CORRELATION,
        "agent_instructions": "GOAL: x",
    }
    story.update(overrides)
    return story


def _record(event, **overrides):
    rec = {"event": event, "story_key": STORY_KEY, "correlation_id": CORRELATION}
    rec.update(overrides)
    return rec


def _merged():
    return _record("story_merged")


# ---------------------------------------------------------------------------
# 1. One cause, one reason -- the classifier counts story causes, not events
# ---------------------------------------------------------------------------


def test_one_step_cap_rebrief_record_yields_exactly_one_reason():
    out = classify_story(STORY_KEY, _story(), [_record(STEP_CAP)])

    assert out["reasons"] == [STEP_CAP], (
        f"a single {STEP_CAP} record must yield exactly one reason, got {out['reasons']}"
    )
    assert len(out["reasons"]) == 1
    assert out["clean"] is False


def test_one_give_up_rebrief_record_yields_exactly_one_reason():
    out = classify_story(STORY_KEY, _story(), [_record(GIVE_UP)])

    assert out["reasons"] == [GIVE_UP], (
        f"a single {GIVE_UP} record must yield exactly one reason, got {out['reasons']}"
    )
    assert len(out["reasons"]) == 1
    assert out["clean"] is False


def test_two_records_of_one_cause_still_yield_one_reason():
    """The regression the story exists to prevent: one cause, two events.

    A story that hit the step cap twice (or whose rebrief was notified twice)
    must not be counted as two independent disqualifiers.
    """
    out = classify_story(STORY_KEY, _story(), [_record(STEP_CAP), _record(STEP_CAP)])

    assert out["reasons"] == [STEP_CAP], (
        f"two {STEP_CAP} records are one cause and must yield one reason, "
        f"got {out['reasons']}"
    )


# ---------------------------------------------------------------------------
# 2. The operator path keeps its own name and still disqualifies
# ---------------------------------------------------------------------------


def test_operator_brief_patched_still_disqualifies_under_its_own_name():
    out = classify_story(STORY_KEY, _story(), [_record(OPERATOR)])

    assert out["reasons"] == [OPERATOR], (
        f"an operator {OPERATOR} record must still disqualify under its own "
        f"name, got {out['reasons']}"
    )
    assert out["clean"] is False
    assert STEP_CAP not in out["reasons"]
    assert GIVE_UP not in out["reasons"]


# ---------------------------------------------------------------------------
# 3. All three names are distinguishable in one rollup
# ---------------------------------------------------------------------------


def test_all_three_names_distinguishable_in_one_rollup():
    records = [_merged(), _record(STEP_CAP), _record(GIVE_UP), _record(OPERATOR)]

    classified = classify_story(STORY_KEY, _story(), records)
    assert classified["reasons"] == sorted([STEP_CAP, GIVE_UP, OPERATOR]), (
        f"all three causes must be reported separately, got {classified['reasons']}"
    )
    assert classified["clean"] is False

    rollup = compute_story_metrics(records)
    payload = rollup[STORY_KEY]
    assert payload["disqualifying_events"] == 3, (
        f"each of the three causes must disqualify once, got {payload}"
    )
    assert payload["first_pass_clean"] is False


def test_each_automated_name_disqualifies_a_first_pass_clean_rollup():
    """Both renamed paths must still disqualify a first pass on their own."""
    for event in (STEP_CAP, GIVE_UP):
        rollup = compute_story_metrics([_merged(), _record(event)])
        payload = rollup[STORY_KEY]
        assert payload["disqualifying_events"] == 1, (
            f"a {event} record must disqualify a first pass, got {payload}"
        )
        assert payload["first_pass_clean"] is False, (
            f"a {event} record must make the story not first-pass-clean"
        )


# ---------------------------------------------------------------------------
# 4. REFERENCE.md attributes each name to its emitter, in one paragraph
# ---------------------------------------------------------------------------


def _notification_records_section() -> str:
    lines = Path("REFERENCE.md").read_text().splitlines()
    start = lines.index("## Notification records")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    return "\n".join(lines[start:end])


def test_reference_attributes_step_cap_rebrief_to_dispatch_attempt():
    section = _notification_records_section()
    paragraphs = [
        p for p in section.split("\n\n") if "pipeline/dispatch_attempt.py" in p
    ]
    assert paragraphs, (
        "REFERENCE.md's Notification records section must name "
        "pipeline/dispatch_attempt.py as a rebrief emitter"
    )
    assert any(STEP_CAP in p for p in paragraphs), (
        f"the paragraph naming pipeline/dispatch_attempt.py must also name "
        f"{STEP_CAP}"
    )


def test_reference_attributes_give_up_rebrief_to_advance():
    section = _notification_records_section()
    paragraphs = [p for p in section.split("\n\n") if "pipeline/advance.py" in p]
    assert paragraphs, (
        "REFERENCE.md's Notification records section must name "
        "pipeline/advance.py as a rebrief emitter"
    )
    assert any(GIVE_UP in p for p in paragraphs), (
        f"the paragraph naming pipeline/advance.py must also name {GIVE_UP}"
    )
