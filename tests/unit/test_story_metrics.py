"""Story metrics from a plan's notifications JSONL sidecar (pure computation).

Story under test: ``pipeline/story_metrics.py`` computes cost-per-merged-story
metrics from the structured notification records the event-stamping stories
already write through ``pipeline.persistence._notification_record`` (keys: ts,
plan, message, story_key, severity, event, dedup_key, plus OPTIONAL
correlation_id/attempt/role/provider/model that are ABSENT, not null, on
older records).

Surfaces pinned here - the public surface is exactly three functions:

  * ``load_notification_records(path)`` returns ``(records, malformed_count)``:
    blank lines are skipped, lines that fail ``json.loads`` are counted in
    ``malformed_count`` and never raise, and a missing file yields ``([], 0)``
    (a plan with no sidecar yet has zero metrics, not an error).
  * ``compute_story_metrics(records)`` groups each record by correlation_id
    when that record has one, else by story_key, else the literal
    ``"<uncorrelated>"`` key, and returns one payload per group with
    story_key / correlation_id / dispatch_failures / rework_cycles /
    escalations / merged / merged_ts / cost, ordered by story_key.
  * ``compute_plan_rollup(stories)`` reduces group payloads to plan totals,
    including ``cost_per_merged_story`` (total_cost / stories_merged rounded
    to 1 decimal; ``None`` when nothing merged).

Fixture discipline (hard rule, see .claude/rules/testing-config-gates.md):
every test writes its own JSONL fixtures into ``tmp_path``.  No test reads,
globs, or references the live ``~/.claude/plans`` directory, ``PLAN_DIR``, or
any real plan's notifications file, and no assertion is keyed to today's live
data.
"""

import ast
import inspect
import json
import re
import sys
from pathlib import Path

import pytest

from pipeline import story_metrics

# --------------------------------------------------------------------------- #
# Fixture helpers - the ONLY data these tests use is built right here.
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[2]
IMPL_PATH = REPO_ROOT / "pipeline" / "story_metrics.py"

PUBLIC_SURFACE = (
    "compute_plan_rollup",
    "compute_story_metrics",
    "load_notification_records",
)

REWORK_EVENTS = ("tests_failed", "merge_ci_rework", "merge_gate_retry", "merge_retry")

TS_A = "2026-01-01T00:00:00+00:00"
TS_B = "2026-01-02T00:00:00+00:00"
TS_C = "2026-01-03T00:00:00+00:00"

GROUP_KEYS = {
    "story_key",
    "correlation_id",
    "dispatch_failures",
    "rework_cycles",
    "escalations",
    "merged",
    "merged_ts",
    "cost",
    "disqualifying_events",
    "first_pass_clean",
}

ROLLUP_KEYS = {
    "stories_total",
    "stories_merged",
    "total_rework_cycles",
    "total_escalations",
    "total_dispatch_failures",
    "total_cost",
    "cost_per_merged_story",
    "first_pass_clean_rate",
}

_OMIT = object()  # sentinel: leave the key absent entirely (older-record shape)
_DEFAULT_TS = TS_A

_STDLIB_MODULES = getattr(sys, "stdlib_module_names", None)


def make_record(
    story_key="S1",
    event=None,
    ts=_DEFAULT_TS,
    correlation_id=_OMIT,
    dedup_key=None,
    with_context=False,
):
    """Build one on-disk notification record (persistence record shape).

    ``correlation_id=_OMIT`` (the default) leaves the key absent entirely -
    the older-record shape.  ``correlation_id=None`` writes an explicit null.
    ``ts=_OMIT`` and ``story_key=_OMIT`` drop those keys.  Metrics must treat
    absent and null optional keys alike.
    """
    record = {
        "ts": ts,
        "plan": "fixture-plan",
        "message": "fixture message",
        "story_key": story_key,
        "severity": "info",
        "event": event,
        "dedup_key": dedup_key,
    }
    if ts is _OMIT:
        del record["ts"]
    if story_key is _OMIT:
        del record["story_key"]
    if correlation_id is not _OMIT:
        record["correlation_id"] = correlation_id
    if with_context:
        record["attempt"] = 2
        record["role"] = "implementer"
        record["provider"] = "anthropic"
        record["model"] = "claude-opus-4"
    return record


def write_jsonl(path, entries):
    """Write fixture entries as JSONL; str entries are written verbatim."""
    lines = [entry if isinstance(entry, str) else json.dumps(entry) for entry in entries]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _impl_tree():
    if not IMPL_PATH.exists():
        pytest.fail("pipeline/story_metrics.py does not exist yet")
    return ast.parse(IMPL_PATH.read_text(encoding="utf-8"), filename=str(IMPL_PATH))


# --------------------------------------------------------------------------- #
# load_notification_records
# --------------------------------------------------------------------------- #


def test_load_missing_file_returns_empty_records_and_zero_malformed(tmp_path):
    records, malformed = story_metrics.load_notification_records(
        tmp_path / "absent.notifications.jsonl"
    )
    assert records == []
    assert malformed == 0


def test_load_empty_file_returns_empty_records_and_zero_malformed(tmp_path):
    path = write_jsonl(tmp_path / "p.notifications.jsonl", [])
    records, malformed = story_metrics.load_notification_records(path)
    assert records == []
    assert malformed == 0


def test_load_returns_a_two_tuple_of_list_and_int(tmp_path):
    path = write_jsonl(tmp_path / "p.notifications.jsonl", [make_record()])
    result = story_metrics.load_notification_records(path)
    assert isinstance(result, tuple)
    assert len(result) == 2
    records, malformed = result
    assert isinstance(records, list)
    assert isinstance(malformed, int)


def test_load_returns_records_verbatim_in_file_order(tmp_path):
    first = make_record(story_key="S1", event="dispatch_failed", dedup_key="d1")
    second = make_record(
        story_key="S2",
        event="story_merged",
        ts=TS_B,
        correlation_id="c-2",
        dedup_key="d2",
    )
    path = write_jsonl(tmp_path / "p.notifications.jsonl", [first, second])
    records, malformed = story_metrics.load_notification_records(path)
    assert malformed == 0
    assert records == [first, second]
    assert all(isinstance(record, dict) for record in records)


def test_load_skips_blank_and_whitespace_only_lines(tmp_path):
    first = make_record(story_key="S1", event="escalated")
    second = make_record(story_key="S1", event="story_merged")
    path = write_jsonl(
        tmp_path / "p.notifications.jsonl",
        ["", first, "   ", second, "\t"],
    )
    records, malformed = story_metrics.load_notification_records(path)
    assert records == [first, second]
    assert malformed == 0


def test_load_counts_malformed_lines_without_raising(tmp_path):
    good = make_record(story_key="S1", event="tests_failed")
    path = write_jsonl(
        tmp_path / "p.notifications.jsonl",
        [good, "{not json", "]", good],
    )
    records, malformed = story_metrics.load_notification_records(path)
    assert records == [good, good]
    assert malformed == 2


def test_load_file_of_only_malformed_lines_returns_empty_with_count(tmp_path):
    path = write_jsonl(
        tmp_path / "p.notifications.jsonl",
        ["{oops", "[1, 2", "nope"],
    )
    records, malformed = story_metrics.load_notification_records(path)
    assert records == []
    assert malformed == 3


def test_load_notification_records_counts_valid_json_non_dict_lines_as_malformed(tmp_path):
    """Lines that parse to a non-dict are malformed, never records.

    Every other malformed fixture in this module is invalid JSON and only
    exercises the ``except ValueError`` path.  These four lines are all VALID
    JSON (the docstring's own examples), so ``json.loads`` succeeds on each and
    only the "parsed value is not a dict" type guard may count them: a
    regression that appends them as records would then be silently dropped by
    ``compute_story_metrics``, reporting zero malformed and no groups.
    """
    path = write_jsonl(
        tmp_path / "p.notifications.jsonl",
        ["[1, 2]", '"str"', "42", "null"],
    )
    records, malformed = story_metrics.load_notification_records(path)
    assert records == []
    assert malformed == 4


# --------------------------------------------------------------------------- #
# compute_story_metrics - grouping
# --------------------------------------------------------------------------- #


def test_metrics_empty_records_give_empty_mapping():
    assert story_metrics.compute_story_metrics([]) == {}


def test_metrics_groups_by_story_key_in_sorted_order():
    records = [
        make_record(story_key="S3", event="dispatch_failed"),
        make_record(story_key="S1", event="story_merged"),
        make_record(story_key="S2", event="escalated"),
    ]
    result = story_metrics.compute_story_metrics(records)
    assert list(result) == ["S1", "S2", "S3"]
    assert [group["story_key"] for group in result.values()] == ["S1", "S2", "S3"]


def test_metrics_group_key_is_correlation_id_when_present():
    records = [
        make_record(story_key="S1", event="dispatch_failed", correlation_id="c-1"),
        make_record(story_key="S1", event="story_merged", correlation_id="c-1"),
    ]
    result = story_metrics.compute_story_metrics(records)
    assert list(result) == ["c-1"]
    group = result["c-1"]
    assert group["correlation_id"] == "c-1"
    assert group["story_key"] == "S1"
    assert group["merged"] is True
    assert group["dispatch_failures"] == 1


def test_metrics_records_without_story_key_or_correlation_use_uncorrelated():
    records = [
        make_record(story_key=_OMIT, event="dispatch_failed"),
        make_record(story_key=_OMIT, event=None),
    ]
    result = story_metrics.compute_story_metrics(records)
    assert list(result) == ["<uncorrelated>"]
    group = result["<uncorrelated>"]
    assert group["story_key"] is None
    assert group["correlation_id"] is None
    assert group["dispatch_failures"] == 1
    assert group["merged"] is False
    assert group["cost"] == 2


def test_metrics_explicit_null_correlation_id_grouped_like_absent():
    records = [
        make_record(story_key="S1", event="tests_failed", correlation_id=None),
        make_record(story_key="S1", event="story_merged"),
    ]
    result = story_metrics.compute_story_metrics(records)
    assert list(result) == ["S1"]
    group = result["S1"]
    assert group["correlation_id"] is None
    assert group["rework_cycles"] == 1
    assert group["merged"] is True


def test_metrics_correlated_and_uncorrelated_records_stay_separate_groups():
    # Literal contract: grouping is per record - correlation_id when that
    # record has one, else that record's story_key.  Two records for the same
    # story, only one of which carries the correlation id, therefore land in
    # two different groups.
    records = [
        make_record(story_key="S1", event="dispatch_failed", correlation_id="c-1"),
        make_record(story_key="S1", event="story_merged"),
    ]
    result = story_metrics.compute_story_metrics(records)
    assert set(result) == {"c-1", "S1"}
    assert result["c-1"]["dispatch_failures"] == 1
    assert result["c-1"]["merged"] is False
    assert result["S1"]["merged"] is True


def test_metrics_mixed_groups_sorted_by_story_key():
    records = [
        make_record(story_key="S2", event="escalated"),
        make_record(story_key="S3", event="dispatch_failed", correlation_id="c-3"),
        make_record(story_key="S1", event="story_merged"),
    ]
    result = story_metrics.compute_story_metrics(records)
    assert [group["story_key"] for group in result.values()] == ["S1", "S2", "S3"]


def test_metrics_optional_context_keys_do_not_affect_grouping_or_counters():
    records = [
        make_record(story_key="S1", event="dispatch_failed", with_context=True),
        make_record(story_key="S1", event="dispatch_failed"),
    ]
    result = story_metrics.compute_story_metrics(records)
    assert list(result) == ["S1"]
    assert result["S1"]["dispatch_failures"] == 2


# --------------------------------------------------------------------------- #
# compute_story_metrics - counters, merged flag, cost
# --------------------------------------------------------------------------- #


def test_metrics_dispatch_failures_count_only_dispatch_failed_events():
    records = [
        make_record(story_key="S1", event="dispatch_failed", dedup_key="d1"),
        make_record(story_key="S1", event="dispatch_failed", dedup_key="d2"),
        make_record(story_key="S1", event="agent_gave_up"),
    ]
    group = story_metrics.compute_story_metrics(records)["S1"]
    assert group["dispatch_failures"] == 2


def test_metrics_rework_cycles_count_all_four_rework_events():
    records = [
        make_record(story_key="S1", event=event, dedup_key=f"d-{event}")
        for event in REWORK_EVENTS
    ]
    group = story_metrics.compute_story_metrics(records)["S1"]
    assert group["rework_cycles"] == 4


def test_metrics_escalations_count_escalated_and_model_fallback():
    records = [
        make_record(story_key="S1", event="escalated"),
        make_record(story_key="S1", event="model_fallback"),
        make_record(story_key="S1", event="agent_gave_up"),
    ]
    group = story_metrics.compute_story_metrics(records)["S1"]
    assert group["escalations"] == 2


def test_metrics_merged_flag_and_timestamp_from_story_merged_record():
    records = [
        make_record(story_key="S1", event="tests_failed"),
        make_record(story_key="S1", event="story_merged", ts=TS_B),
    ]
    group = story_metrics.compute_story_metrics(records)["S1"]
    assert group["merged"] is True
    assert group["merged_ts"] == TS_B


def test_metrics_not_merged_story_has_false_flag_and_none_timestamp():
    records = [make_record(story_key="S1", event="merge_retry")]
    group = story_metrics.compute_story_metrics(records)["S1"]
    assert group["merged"] is False
    assert group["merged_ts"] is None


def test_metrics_merged_story_without_ts_keeps_none_timestamp():
    records = [make_record(story_key="S1", event="story_merged", ts=_OMIT)]
    group = story_metrics.compute_story_metrics(records)["S1"]
    assert group["merged"] is True
    assert group["merged_ts"] is None


def test_metrics_cost_is_one_plus_all_counters():
    records = [
        make_record(story_key="S1", event="dispatch_failed"),
        make_record(story_key="S1", event="tests_failed"),
        make_record(story_key="S1", event="merge_ci_rework"),
        make_record(story_key="S1", event="escalated"),
    ]
    group = story_metrics.compute_story_metrics(records)["S1"]
    assert group["dispatch_failures"] == 1
    assert group["rework_cycles"] == 2
    assert group["escalations"] == 1
    assert group["cost"] == 5  # 1 + 1 + 2 + 1


def test_metrics_merged_story_with_zero_rework_costs_one():
    records = [make_record(story_key="S1", event="story_merged")]
    group = story_metrics.compute_story_metrics(records)["S1"]
    assert group["dispatch_failures"] == 0
    assert group["rework_cycles"] == 0
    assert group["escalations"] == 0
    assert group["cost"] == 1


def test_metrics_known_non_counted_events_change_no_counter():
    for event in (
        "agent_gave_up",
        "merge_gate_failed",
        "merge_failed",
        "rebase_auto_resolved",
    ):
        records = [make_record(story_key="S1", event=event)]
        group = story_metrics.compute_story_metrics(records)["S1"]
        assert group["dispatch_failures"] == 0, event
        assert group["rework_cycles"] == 0, event
        assert group["escalations"] == 0, event
        assert group["merged"] is False, event
        assert group["cost"] == 1, event


def test_metrics_unrecognized_or_missing_event_still_grouped_but_inert():
    for event in (None, "totally_unknown_event", _OMIT):
        records = [
            make_record(story_key="S1", event=event),
            make_record(story_key="S1", event="story_merged"),
        ]
        group = story_metrics.compute_story_metrics(records)["S1"]
        assert group["merged"] is True, event
        assert group["dispatch_failures"] == 0, event
        assert group["rework_cycles"] == 0, event
        assert group["escalations"] == 0, event
        assert group["cost"] == 1, event


def test_metrics_duplicate_events_with_same_dedup_key_counted_twice():
    records = [
        make_record(
            story_key="S1", event="tests_failed", dedup_key="tests_failed:S1"
        ),
        make_record(
            story_key="S1", event="tests_failed", dedup_key="tests_failed:S1"
        ),
    ]
    group = story_metrics.compute_story_metrics(records)["S1"]
    assert group["rework_cycles"] == 2  # raw counts; dedup is the sink's job


def test_metrics_group_payload_has_exactly_the_contracted_keys():
    records = [
        make_record(
            story_key="S1", event="story_merged", correlation_id="c-1", ts=TS_B
        )
    ]
    (group,) = story_metrics.compute_story_metrics(records).values()
    assert set(group) == GROUP_KEYS


def test_metrics_docstring_documents_inert_unrecognized_events():
    doc = story_metrics.compute_story_metrics.__doc__
    assert doc, "compute_story_metrics must have a docstring"
    lowered = doc.lower()
    phrasing = (
        r"no\s+recognizable|unrecognized|unknown\s+event|not\s+recognized|"
        r"no\s+known\s+event|not\s+one\s+of\s+the\s+known"
    )
    assert re.search(phrasing, lowered), (
        "compute_story_metrics docstring must document that records with no "
        "recognizable event are still attributed to their group but change no "
        "counter"
    )
    assert "count" in lowered


# --------------------------------------------------------------------------- #
# compute_plan_rollup
# --------------------------------------------------------------------------- #


def test_rollup_empty_story_list_is_all_zeros_with_none_ratio():
    rollup = story_metrics.compute_plan_rollup([])
    assert set(rollup) == ROLLUP_KEYS
    assert rollup["stories_total"] == 0
    assert rollup["stories_merged"] == 0
    assert rollup["total_rework_cycles"] == 0
    assert rollup["total_escalations"] == 0
    assert rollup["total_dispatch_failures"] == 0
    assert rollup["total_cost"] == 0
    assert rollup["cost_per_merged_story"] is None


def test_rollup_nothing_merged_guards_division_by_zero():
    stories = [
        {
            "story_key": "S1",
            "merged": False,
            "cost": 3,
            "rework_cycles": 1,
            "escalations": 1,
            "dispatch_failures": 1,
        },
        {
            "story_key": "S2",
            "merged": False,
            "cost": 1,
            "rework_cycles": 0,
            "escalations": 0,
            "dispatch_failures": 0,
        },
    ]
    rollup = story_metrics.compute_plan_rollup(stories)
    assert rollup["stories_total"] == 2
    assert rollup["stories_merged"] == 0
    assert rollup["total_cost"] == 4
    assert rollup["cost_per_merged_story"] is None


def test_rollup_happy_path_totals_and_ratio():
    stories = [
        {
            "story_key": "S1",
            "merged": True,
            "cost": 2,
            "rework_cycles": 1,
            "escalations": 0,
            "dispatch_failures": 0,
        },
        {
            "story_key": "S2",
            "merged": False,
            "cost": 4,
            "rework_cycles": 0,
            "escalations": 1,
            "dispatch_failures": 2,
        },
        {
            "story_key": "S3",
            "merged": True,
            "cost": 1,
            "rework_cycles": 0,
            "escalations": 0,
            "dispatch_failures": 0,
        },
    ]
    rollup = story_metrics.compute_plan_rollup(stories)
    assert set(rollup) == ROLLUP_KEYS
    assert rollup["stories_total"] == 3
    assert rollup["stories_merged"] == 2
    assert rollup["total_rework_cycles"] == 1
    assert rollup["total_escalations"] == 1
    assert rollup["total_dispatch_failures"] == 2
    assert rollup["total_cost"] == 7
    assert rollup["cost_per_merged_story"] == 3.5


def test_rollup_cost_per_merged_story_rounded_to_one_decimal():
    stories = [
        {
            "story_key": "S1",
            "merged": True,
            "cost": 4,
            "rework_cycles": 3,
            "escalations": 0,
            "dispatch_failures": 0,
        },
        {
            "story_key": "S2",
            "merged": True,
            "cost": 3,
            "rework_cycles": 2,
            "escalations": 0,
            "dispatch_failures": 0,
        },
        {
            "story_key": "S3",
            "merged": True,
            "cost": 3,
            "rework_cycles": 2,
            "escalations": 0,
            "dispatch_failures": 0,
        },
    ]
    rollup = story_metrics.compute_plan_rollup(stories)
    assert rollup["total_cost"] == 10
    assert rollup["stories_merged"] == 3
    assert rollup["cost_per_merged_story"] == 3.3


def test_rollup_single_merged_story_ratio_equals_total_cost():
    stories = [
        {
            "story_key": "S1",
            "merged": True,
            "cost": 7,
            "rework_cycles": 0,
            "escalations": 0,
            "dispatch_failures": 0,
        }
    ]
    rollup = story_metrics.compute_plan_rollup(stories)
    assert rollup["cost_per_merged_story"] == 7.0


def test_rollup_from_compute_story_metrics_output_end_to_end():
    records = [
        make_record(story_key="S1", event="tests_failed"),
        make_record(story_key="S1", event="story_merged", ts=TS_B),
        make_record(story_key="S2", event="dispatch_failed"),
        make_record(story_key="S2", event="dispatch_failed"),
        make_record(story_key="S2", event="escalated"),
        make_record(story_key="S3", event="story_merged", ts=TS_C),
    ]
    metrics = story_metrics.compute_story_metrics(records)
    rollup = story_metrics.compute_plan_rollup(list(metrics.values()))
    assert rollup["stories_total"] == 3
    assert rollup["stories_merged"] == 2
    assert rollup["total_rework_cycles"] == 1
    assert rollup["total_escalations"] == 1
    assert rollup["total_dispatch_failures"] == 2
    assert rollup["total_cost"] == 7
    assert rollup["cost_per_merged_story"] == 3.5


def test_metrics_compute_from_loaded_sidecar_fixture(tmp_path):
    fixture_records = [
        make_record(story_key="S1", event="tests_failed", dedup_key="a"),
        make_record(
            story_key="S1", event="story_merged", ts=TS_B, dedup_key="b"
        ),
        make_record(story_key="S2", event="dispatch_failed", dedup_key="c"),
        make_record(story_key="S2", event="dispatch_failed", dedup_key="d"),
        make_record(story_key="S2", event="escalated", dedup_key="e"),
    ]
    path = write_jsonl(
        tmp_path / "fixture-plan.notifications.jsonl", fixture_records
    )
    loaded, malformed = story_metrics.load_notification_records(path)
    assert malformed == 0
    result = story_metrics.compute_story_metrics(loaded)
    assert [group["story_key"] for group in result.values()] == ["S1", "S2"]
    assert result["S1"]["cost"] == 2
    assert result["S2"]["cost"] == 4


# --------------------------------------------------------------------------- #
# Module-shape gates: public surface, stdlib-only, I/O confinement
# --------------------------------------------------------------------------- #


def test_public_function_signatures_match_the_contract():
    expected = {
        "load_notification_records": ("path",),
        "compute_story_metrics": ("records",),
        "compute_plan_rollup": ("stories",),
    }
    for name, params in expected.items():
        func = getattr(story_metrics, name)
        assert tuple(inspect.signature(func).parameters) == params, name


def test_module_public_surface_is_exactly_the_three_functions():
    tree = _impl_tree()
    public_functions = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]
    assert sorted(public_functions) == sorted(PUBLIC_SURFACE)
    public_classes = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
    ]
    assert public_classes == []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
        ):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            assert sorted(ast.literal_eval(node.value)) == sorted(PUBLIC_SURFACE)


def test_module_imports_are_stdlib_only():
    if _STDLIB_MODULES is None:
        pytest.skip("sys.stdlib_module_names needs Python 3.10+")
    roots = set()
    for node in ast.walk(_impl_tree()):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add("<relative-import>")
            elif node.module:
                roots.add(node.module.split(".")[0])
    non_stdlib = sorted(roots - set(_STDLIB_MODULES))
    assert non_stdlib == [], f"non-stdlib imports found: {non_stdlib}"


_IO_METHODS = frozenset(
    {
        "open",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "unlink",
        "mkdir",
        "rmdir",
        "rename",
        "replace",
        "touch",
    }
)


def _io_calls(node):
    calls = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if (isinstance(func, ast.Name) and func.id == "open") or (
            isinstance(func, ast.Attribute) and func.attr in _IO_METHODS
        ):
            calls.append(sub)
    return calls


def test_file_io_is_confined_to_load_notification_records():
    tree = _impl_tree()
    offenders = []
    loader_calls = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            count = len(_io_calls(node))
            if node.name == "load_notification_records":
                loader_calls = count
            elif count:
                offenders.append(f"{node.name}(): {count} file-I/O call(s)")
        elif isinstance(node, ast.ClassDef):
            if _io_calls(node):
                offenders.append(f"class {node.name} does file I/O")
        elif _io_calls(node):
            offenders.append("module-level statement does file I/O")
    assert offenders == [], (
        "file I/O must live only in load_notification_records; found: "
        f"{offenders}"
    )
    assert loader_calls >= 1, "load_notification_records must read the sidecar"


def test_module_never_references_the_live_plans_directory():
    source = IMPL_PATH.read_text(encoding="utf-8")
    for forbidden in ("PLAN_DIR", ".claude", "~"):
        assert forbidden not in source, (
            f"story_metrics.py must not reference {forbidden!r}; it is a pure "
            "module over the path/records its caller passes in"
        )