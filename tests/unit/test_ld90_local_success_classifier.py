"""LD90-W0-06: pure first-pass-clean classifier for local (non-Claude) stories.

`pipeline/local_success.py` holds the pure logic behind the "90% first-pass
clean over the last 30 non-Claude stories" goal: which stories belong to the
population, which of those were clean, which tier they ran on, and the rolling
rate over a window. It is stdlib-only and does no I/O; the CLI that reads
manifests and sidecars lives outside this pure module.

All fixtures here are synthetic dicts; no real manifest or sidecar is read.
"""
import ast
import pathlib
import sys

import pytest


def _ls():
    """Import the module under test at call time, not at collection time.

    A bare `pytest --collect-only` must still collect this file (see
    tests/unit/test_pytest_collection_allowlist.py, which asserts that run
    exits 0), so the import is deferred; each test then fails with the
    ModuleNotFoundError naming the missing module.
    """
    import pipeline.local_success as mod

    return mod


def classify_story(*args, **kwargs):
    """Delegate to pipeline.local_success.classify_story."""
    return _ls().classify_story(*args, **kwargs)


def rolling_rate(*args, **kwargs):
    """Delegate to pipeline.local_success.rolling_rate."""
    return _ls().rolling_rate(*args, **kwargs)

RESULT_KEYS = {
    "story_key",
    "in_population",
    "tier",
    "dispatched_at",
    "clean",
    "reasons",
}


def _story(**overrides):
    """A done, on-device story that is in population and clean by default."""
    story = {
        "story_key": "S1",
        "status": "done",
        "backend": "local",
        "model": "qwen3-30b",
        "dispatched_model": "qwen3-30b",
        "dispatched_at": "2026-09-18T10:00:00Z",
    }
    story.update(overrides)
    return story


def _rec(**overrides):
    return dict(overrides)


# --------------------------------------------------------------------------
# 1. happy path
# --------------------------------------------------------------------------
def test_done_local_story_with_only_a_merge_record_is_clean():
    out = classify_story(
        "S1", _story(), [_rec(event="story_merged", story_key="S1")]
    )
    assert out["story_key"] == "S1"
    assert out["in_population"] is True
    assert out["clean"] is True
    assert out["reasons"] == []
    assert out["tier"] == "on-device"
    assert out["dispatched_at"] == "2026-09-18T10:00:00Z"


def test_result_shape_is_exactly_the_documented_keys():
    out = classify_story("S1", _story(), [])
    assert set(out) == RESULT_KEYS


def test_reasons_are_a_sorted_list():
    out = classify_story(
        "S1",
        _story(status="parked", agent_instructions="REWORK SCOPE: x"),
        [_rec(event="escalated", story_key="S1")],
    )
    assert isinstance(out["reasons"], list)
    assert out["reasons"] == sorted(out["reasons"])
    assert {"not_done", "escalated", "brief_rewrite_marker"} <= set(out["reasons"])


@pytest.mark.parametrize(
    "story,records",
    [
        (_story(), [_rec(event="story_merged", story_key="S1")]),
        (_story(status="parked"), []),
        (_story(backend="claude"), []),
        (_story(agent_instructions="REWORK SCOPE:"), []),
        (_story(), [_rec(event="escalated", story_key="S1")]),
    ],
)
def test_reasons_are_empty_if_and_only_if_clean(story, records):
    out = classify_story("S1", story, records)
    assert (out["reasons"] == []) is out["clean"]


# --------------------------------------------------------------------------
# 2. escalation
# --------------------------------------------------------------------------
def test_escalated_story_is_in_population_and_not_clean():
    story = _story(backend="claude", escalated=True, status="done")
    out = classify_story("S1", story, [_rec(event="escalated", story_key="S1")])
    assert out["in_population"] is True
    assert out["clean"] is False
    assert "escalated" in out["reasons"]


def test_escalated_flag_alone_puts_a_claude_story_in_population():
    out = classify_story("S1", _story(backend="claude", escalated=True), [])
    assert out["in_population"] is True


def test_model_fallback_record_puts_a_claude_story_in_population():
    story = _story(backend="claude", status="done")
    out = classify_story("S1", story, [_rec(event="model_fallback", story_key="S1")])
    assert out["in_population"] is True
    assert out["clean"] is False
    assert "model_fallback" in out["reasons"]


# --------------------------------------------------------------------------
# 3. out of population
# --------------------------------------------------------------------------
def test_claude_story_without_escalation_or_records_is_out_of_population():
    out = classify_story("S1", _story(backend="claude"), [])
    assert out["in_population"] is False


def test_empty_backend_is_not_a_non_claude_backend():
    """An empty backend is not a non-Claude backend.

    On its own - with no model tag to attribute the story to a tier - it stays
    out of the population. The model tags are dropped here deliberately so this
    fixture pins the empty-backend signal alone; see
    test_a_story_with_no_backend_but_a_local_tag_is_in_population for the tag-only
    population clause added alongside it.
    """
    out = classify_story(
        "S1", _story(backend="", model=None, dispatched_model=None), []
    )
    assert out["in_population"] is False
    assert out["tier"] == "unknown"


# --------------------------------------------------------------------------
# 4. legacy records matched by message prefix only
# --------------------------------------------------------------------------
def test_legacy_escalation_message_matches_by_prefix():
    story = _story()
    record = _rec(event=None, message="S1 escalating to Claude (step cap)")
    out = classify_story("S1", story, [record])
    assert out["in_population"] is True
    assert out["clean"] is False
    assert "legacy_message" in out["reasons"]


def test_legacy_record_without_an_event_key_also_matches():
    record = _rec(message="S1 escalating to Claude (step cap)")
    out = classify_story("S1", _story(), [record])
    assert "legacy_message" in out["reasons"]


@pytest.mark.parametrize(
    "message",
    [
        "S1 escalating to Claude (step cap)",
        "S1 parked: rebase conflict",
        "S1 switching to fallback model",
        "S1 triage needed",
        "S1 wedged on a lock",
        "S1 ESCALATING TO CLAUDE",
    ],
)
def test_every_legacy_marker_word_flags_an_event_less_record(message):
    out = classify_story("S1", _story(), [_rec(event=None, message=message)])
    assert out["clean"] is False
    assert "legacy_message" in out["reasons"]


def test_benign_event_less_message_is_not_a_legacy_hit():
    out = classify_story("S1", _story(), [_rec(event=None, message="S1 merged")])
    assert out["clean"] is True
    assert out["reasons"] == []


# --------------------------------------------------------------------------
# 5. the legacy regex applies only to event-less records
# --------------------------------------------------------------------------
def test_event_bearing_record_mentioning_triage_is_still_clean():
    out = classify_story(
        "S1", _story(), [_rec(event="tests_failed", message="triage: 3 failed")]
    )
    assert out["clean"] is True
    assert out["reasons"] == []


@pytest.mark.parametrize("event", ["story_parked", "brief_patched"])
def test_park_and_patch_events_are_reasons(event):
    out = classify_story("S1", _story(), [_rec(event=event, story_key="S1")])
    assert out["clean"] is False
    assert event in out["reasons"]


# --------------------------------------------------------------------------
# 6. correlation_id matching
# --------------------------------------------------------------------------
def test_record_matched_by_correlation_id_only_is_counted():
    story = _story(correlation_id="corr-1")
    record = _rec(correlation_id="corr-1", event="escalated", message="unrelated")
    out = classify_story("S1", story, [record])
    assert out["in_population"] is True
    assert out["clean"] is False
    assert "escalated" in out["reasons"]


def test_a_different_correlation_id_does_not_match():
    story = _story(correlation_id="corr-1")
    record = _rec(correlation_id="corr-2", event="escalated")
    out = classify_story("S1", story, [record])
    assert out["clean"] is True
    assert out["reasons"] == []


def test_a_story_without_a_correlation_id_never_matches_on_one():
    record = _rec(correlation_id="corr-1", event="escalated")
    out = classify_story("S1", _story(), [record])
    assert out["clean"] is True


# --------------------------------------------------------------------------
# 7. prefix matching requires the trailing space
# --------------------------------------------------------------------------
def test_record_for_s10_does_not_match_story_s1():
    out = classify_story("S1", _story(backend="claude"), [_rec(story_key="S10", event="escalated")])
    assert out["in_population"] is False
    assert out["clean"] is True


def test_message_for_s10_does_not_match_story_s1():
    out = classify_story("S1", _story(), [_rec(event=None, message="S10 parked: rebase conflict")])
    assert out["clean"] is True
    assert out["reasons"] == []


def test_story_prefixed_park_message_matches_s1():
    record = _rec(event=None, message="story S1 parked: rebase conflict in pipeline/x.py")
    out = classify_story("S1", _story(), [record])
    assert out["in_population"] is True
    assert out["clean"] is False
    assert "legacy_message" in out["reasons"]


def test_story_key_is_taken_from_the_argument_not_the_story_dict():
    record = _rec(event="escalated", story_key="S1")
    out = classify_story("S1", {"status": "done", "backend": "local"}, [record])
    assert out["clean"] is False
    assert "escalated" in out["reasons"]


# --------------------------------------------------------------------------
# 8. brief-rewrite markers
# --------------------------------------------------------------------------
def test_rework_marker_in_agent_instructions():
    out = classify_story("S1", _story(agent_instructions="REWORK SCOPE: shrink it"), [])
    assert out["clean"] is False
    assert "brief_rewrite_marker" in out["reasons"]


def test_amendment_marker_in_agent_instructions():
    out = classify_story("S1", _story(agent_instructions="AMENDMENT: add a test"), [])
    assert "brief_rewrite_marker" in out["reasons"]


@pytest.mark.parametrize(
    "instructions",
    [
        "PIPELINE_REWORK_MAX_ATTEMPTS=3",
        "PIPELINE_AMENDMENT_LIMIT=2",
        "REWORKED the plan",
        "rework scope: lowercase",
        "amendment: lowercase",
        "",
        None,
    ],
)
def test_non_marker_instructions_do_not_flag(instructions):
    out = classify_story("S1", _story(agent_instructions=instructions), [])
    assert out["clean"] is True
    assert out["reasons"] == []


# --------------------------------------------------------------------------
# 9. status
# --------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["parked", "in_progress", "blocked", None, ""])
def test_any_status_other_than_done_is_not_done(status):
    out = classify_story("S1", _story(status=status), [])
    assert out["clean"] is False
    assert "not_done" in out["reasons"]


# --------------------------------------------------------------------------
# 10. tier
# --------------------------------------------------------------------------
def test_cloud_suffix_on_dispatched_model_is_cloud_oss():
    story = _story(dispatched_model="deepseek-v4.1-flash:cloud")
    assert classify_story("S1", story, [])["tier"] == "cloud-oss"


def test_cloud_suffix_on_model_is_cloud_oss_when_dispatched_model_is_absent():
    story = _story(dispatched_model=None, model="qwen3-30b:cloud")
    assert classify_story("S1", story, [])["tier"] == "cloud-oss"


def test_cloud_suffix_wins_over_a_non_claude_backend():
    story = _story(backend="local", dispatched_model="x:cloud")
    assert classify_story("S1", story, [])["tier"] == "cloud-oss"


def test_local_backend_without_a_cloud_tag_is_on_device():
    story = _story(dispatched_model="qwen3-30b", model="qwen3-30b")
    assert classify_story("S1", story, [])["tier"] == "on-device"


def test_escalated_to_claude_story_has_unknown_tier():
    story = _story(backend="claude", escalated=True, model="claude-sonnet", dispatched_model=None)
    assert classify_story("S1", story, [])["tier"] == "unknown"


def test_no_backend_and_no_tag_is_unknown_tier():
    story = _story(backend="", dispatched_model=None, model=None)
    assert classify_story("S1", story, [])["tier"] == "unknown"


# --------------------------------------------------------------------------
# malformed input
# --------------------------------------------------------------------------
def test_non_dict_records_are_ignored():
    records = [None, "S1 escalating to Claude", 42, ["escalated"]]
    out = classify_story("S1", _story(), records)
    assert out["clean"] is True
    assert out["in_population"] is True
    assert out["reasons"] == []


def test_non_dict_records_do_not_hide_a_real_match():
    records = [None, "junk", _rec(event="escalated", story_key="S1")]
    out = classify_story("S1", _story(), records)
    assert out["clean"] is False
    assert "escalated" in out["reasons"]


def test_missing_dispatched_at_is_none():
    story = _story()
    story.pop("dispatched_at")
    assert classify_story("S1", story, [])["dispatched_at"] is None


def test_empty_records_list_is_clean_for_a_done_local_story():
    out = classify_story("S1", _story(), [])
    assert out["in_population"] is True
    assert out["clean"] is True


def test_story_dict_missing_every_key_does_not_crash():
    out = classify_story("S1", {}, [])
    assert out["story_key"] == "S1"
    assert out["in_population"] is False
    assert out["tier"] == "unknown"
    assert out["dispatched_at"] is None
    assert out["clean"] is False
    assert "not_done" in out["reasons"]


def test_record_without_a_message_key_is_not_a_legacy_hit():
    out = classify_story("S1", _story(), [_rec(story_key="S1", event=None)])
    assert out["clean"] is True
    assert out["reasons"] == []


def test_escalated_false_does_not_put_a_claude_story_in_population():
    out = classify_story("S1", _story(backend="claude", escalated=False), [])
    assert out["in_population"] is False


@pytest.mark.parametrize("message", [None, 42, ["S1 parked"], {"a": 1}])
def test_non_string_messages_are_stringified_and_do_not_crash(message):
    out = classify_story("S1", _story(), [_rec(event=None, message=message)])
    assert out["clean"] is True
    assert out["reasons"] == []


# --------------------------------------------------------------------------
# 11. rolling_rate
# --------------------------------------------------------------------------
def _entry(dispatched_at, clean, tier="on-device", reasons=(), in_population=True):
    return {
        "story_key": dispatched_at,
        "in_population": in_population,
        "tier": tier,
        "dispatched_at": dispatched_at,
        "clean": clean,
        "reasons": list(reasons),
    }


def _five_entries():
    return [
        _entry("2026-09-01T00:00:00Z", True, reasons=["not_done"]),
        _entry("2026-09-02T00:00:00Z", True),
        _entry("2026-09-03T00:00:00Z", False, reasons=["not_done"]),
        _entry("2026-09-04T00:00:00Z", False, reasons=["escalated", "not_done"]),
        _entry("2026-09-05T00:00:00Z", True),
    ]


def test_window_keeps_only_the_latest_entries():
    out = rolling_rate(_five_entries(), window=3)
    assert out["count"] == 3
    assert out["clean"] == 1
    assert out["rate"] == round(1 / 3, 3)


def test_window_zero_means_all_entries():
    out = rolling_rate(_five_entries(), window=0)
    assert out["count"] == 5
    assert out["clean"] == 3
    assert out["rate"] == 0.6


def test_negative_window_means_all_entries():
    out = rolling_rate(_five_entries(), window=-5)
    assert out["count"] == 5


def test_default_window_is_thirty():
    out = rolling_rate(_five_entries())
    assert out["count"] == 5


def test_window_larger_than_the_input_keeps_everything():
    out = rolling_rate(_five_entries(), window=30)
    assert out["count"] == 5


def test_window_of_one_keeps_only_the_latest():
    out = rolling_rate(_five_entries(), window=1)
    assert out["count"] == 1
    assert out["clean"] == 1
    assert out["rate"] == 1.0


def test_entries_are_sorted_by_dispatched_at_not_input_order():
    shuffled = list(reversed(_five_entries()))
    out = rolling_rate(shuffled, window=2)
    assert out["count"] == 2
    assert out["clean"] == 1


def test_reasons_are_aggregated_over_the_counted_window():
    out = rolling_rate(_five_entries(), window=3)
    assert out["reasons"] == {"not_done": 2, "escalated": 1}


def test_reasons_are_aggregated_over_all_entries_when_window_is_zero():
    out = rolling_rate(_five_entries(), window=0)
    assert out["reasons"] == {"not_done": 3, "escalated": 1}


def test_tier_filter_restricts_the_window():
    entries = [
        _entry("2026-09-01T00:00:00Z", True, tier="on-device"),
        _entry("2026-09-02T00:00:00Z", False, tier="cloud-oss"),
        _entry("2026-09-03T00:00:00Z", True, tier="on-device"),
        _entry("2026-09-04T00:00:00Z", True, tier="cloud-oss"),
        _entry("2026-09-05T00:00:00Z", False, tier="on-device"),
    ]
    cloud = rolling_rate(entries, window=0, tier="cloud-oss")
    assert cloud["count"] == 2
    assert cloud["clean"] == 1
    assert cloud["rate"] == 0.5
    local = rolling_rate(entries, window=0, tier="on-device")
    assert local["count"] == 3
    assert local["clean"] == 2


def test_tier_filter_with_no_matches_is_an_empty_window():
    out = rolling_rate(_five_entries(), window=0, tier="unknown")
    assert out["count"] == 0
    assert out["clean"] == 0
    assert out["rate"] is None
    assert out["reasons"] == {}


def test_empty_input_has_no_rate():
    out = rolling_rate([], window=30)
    assert out["count"] == 0
    assert out["clean"] == 0
    assert out["rate"] is None
    assert out["reasons"] == {}


def test_entries_without_a_dispatched_at_are_excluded():
    entries = _five_entries() + [
        _entry(None, True),
        _entry("", True),
    ]
    out = rolling_rate(entries, window=0)
    assert out["count"] == 5


def test_entries_out_of_population_are_excluded():
    entries = _five_entries() + [
        _entry("2026-09-06T00:00:00Z", True, in_population=False),
    ]
    out = rolling_rate(entries, window=0)
    assert out["count"] == 5
    assert out["clean"] == 3


def test_rate_is_rounded_to_three_places():
    entries = [
        _entry("2026-09-01T00:00:00Z", True),
        _entry("2026-09-02T00:00:00Z", True),
        _entry("2026-09-03T00:00:00Z", False),
    ]
    out = rolling_rate(entries, window=0)
    assert out["rate"] == round(2 / 3, 3) == 0.667


def test_rolling_rate_result_keys():
    out = rolling_rate(_five_entries(), window=3)
    assert set(out) == {"count", "clean", "rate", "reasons"}


def test_window_equal_to_the_entry_count_keeps_everything():
    out = rolling_rate(_five_entries(), window=5)
    assert out["count"] == 5
    assert out["clean"] == 3


def test_tier_filter_is_applied_before_the_window():
    """The window is the last N *matching* entries, not the last N overall."""
    entries = [
        _entry("2026-09-01T00:00:00Z", True, tier="on-device"),
        _entry("2026-09-02T00:00:00Z", False, tier="cloud-oss"),
        _entry("2026-09-03T00:00:00Z", False, tier="on-device"),
        _entry("2026-09-04T00:00:00Z", False, tier="cloud-oss"),
        _entry("2026-09-05T00:00:00Z", True, tier="on-device"),
    ]
    out = rolling_rate(entries, window=2, tier="on-device")
    assert out["count"] == 2
    assert out["clean"] == 1
    assert out["rate"] == 0.5


# --------------------------------------------------------------------------
# module-level contract
# --------------------------------------------------------------------------
def _module_source():
    """The source text of pipeline/local_success.py."""
    return pathlib.Path(_ls().__file__).read_text()


def test_all_exports_the_public_api():
    assert "classify_story" in _ls().__all__
    assert "rolling_rate" in _ls().__all__


def test_public_api_is_the_two_documented_functions():
    """The pure logic exposes exactly classify_story and rolling_rate.

    A CLI entry point (`main`/`cli`) is tolerated because the file-reading
    CLI lives outside this pure module; any other public function would be a
    third public API surface this module is not meant to grow.
    """
    tree = ast.parse(_module_source())
    public = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    assert {"classify_story", "rolling_rate"} <= public
    assert public - {"classify_story", "rolling_rate"} <= {"main", "cli"}


def test_at_most_one_private_helper():
    tree = ast.parse(_module_source())
    private = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_")
    ]
    assert len(private) <= 1


def test_module_has_a_docstring_stating_the_definitions():
    doc = _ls().__doc__ or ""
    assert doc.strip()
    lowered = doc.lower()
    assert "population" in lowered
    assert "clean" in lowered
    assert "tier" in lowered


def test_module_imports_only_the_standard_library():
    tree = ast.parse(_module_source())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
            else:
                roots.add("<relative>")
    assert roots <= set(sys.stdlib_module_names), sorted(roots)
    assert "pipeline" not in roots
    assert "app" not in roots
