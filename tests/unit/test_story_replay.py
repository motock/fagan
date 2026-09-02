"""Tests for app/story_replay.py — pure replay-event assembly.

These tests are RED until app/story_replay.py exists. The module must
export ``build_replay_events(journal_entries, log_sources)`` and contain
NO I/O of any kind (no open(), no Path reads, no datetime.now — callers
supply every input). The brief permits reusing
``app.dashboard_helpers._parse_iso`` (or replicating it); nothing heavier
than stdlib + that helper may be imported.

Contract under test (from the brief):
  - one event per journal entry: source "journal", kind = step,
    message = summary with "; next: <hint>" folded in when next_hint is
    a non-empty string;
  - a log line that STARTS with an ISO-8601 timestamp (trailing 'Z'
    tolerated) starts a new event {ts, source label, remainder-as-message};
  - a log line WITHOUT a leading timestamp is a continuation: appended
    (newline-separated) to the previous event of the SAME source only;
  - leading garbage before any timestamp in a source becomes a ts=None
    event for that source;
  - ts=None events sort before all timed events; timed events sort by ts
    ascending; the sort is STABLE — equal keys keep input order, where
    input order is journal first, then sources in dict insertion order;
  - never raises: malformed journal entries, empty sources, weird strings
    all degrade to best-effort events or are skipped.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

from app.story_replay import build_replay_events

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

J_TS_A = "2025-01-01T10:00:00Z"
J_TS_B = "2025-01-01T11:00:00Z"
J_TS_C = "2025-01-01T12:00:00Z"


def _parse_iso(ts):
    """Mirror of app.dashboard_helpers._parse_iso (tolerates trailing Z)."""
    from datetime import datetime

    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _keys(event):
    """The exact event schema from the brief."""
    return set(event.keys())


def _seq(events):
    """Compact (source, ts) view for ordering assertions."""
    return [(e["source"], e["ts"]) for e in events]


def _messages(events, source):
    return [e["message"] for e in events if e["source"] == source]


# ---------------------------------------------------------------------------
# module-level purity / import-weight constraints
# ---------------------------------------------------------------------------


class TestModuleConstraints:
    """The brief pins non-functional properties of the module itself."""

    def _source(self) -> str:
        return Path("app/story_replay.py").read_text(encoding="utf-8")

    def test_module_has_no_io(self):
        src = self._source()
        for forbidden in (
            "open(",
            "datetime.now",
            "datetime.utcnow",
            "read_text",
            "write_text",
            "listdir",
            "glob(",
            "subprocess",
            "from pathlib",
            "import pathlib",
        ):
            assert forbidden not in src, (
                f"app/story_replay.py must not do I/O; found {forbidden!r}"
            )

    def test_module_imports_nothing_heavier_than_stdlib_plus_dashboard_helpers(self):
        import ast

        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = [node.module]
            for root in roots:
                top = root.split(".")[0]
                if top == "app":
                    assert root == "app.dashboard_helpers", (
                        f"only app.dashboard_helpers may be imported from app/, "
                        f"got {root!r}"
                    )
                else:
                    assert top in sys.stdlib_module_names, (
                        f"import {root!r} is not stdlib; the module may import "
                        f"nothing heavier than stdlib + app.dashboard_helpers"
                    )

    def test_repeated_calls_are_pure_and_deterministic(self):
        journal = [{"step": "s1", "summary": "sum", "ts": J_TS_A, "next_hint": "h"}]
        sources = {"agent.log": ["2025-01-01T09:00:00Z boot", "continuation"]}
        journal_snapshot = copy.deepcopy(journal)
        sources_snapshot = copy.deepcopy(sources)

        first = build_replay_events(journal, sources)
        second = build_replay_events(journal, sources)

        assert first == second
        # pure function: inputs are not mutated
        assert journal == journal_snapshot
        assert sources == sources_snapshot


# ---------------------------------------------------------------------------
# event schema
# ---------------------------------------------------------------------------


class TestEventSchema:
    def test_every_event_has_exactly_the_four_contract_keys(self):
        events = build_replay_events(
            [{"step": "s1", "summary": "sum", "ts": J_TS_A, "next_hint": None}],
            {"agent.log": [f"{J_TS_B} log line"]},
        )
        assert events, "expected at least one event"
        for event in events:
            assert _keys(event) == {"ts", "source", "kind", "message"}

    def test_log_line_events_have_kind_none(self):
        events = build_replay_events([], {"agent.log": [f"{J_TS_A} hello world"]})
        assert len(events) == 1
        assert events[0]["kind"] is None

    def test_log_event_ts_round_trips_to_the_line_timestamp(self):
        events = build_replay_events([], {"agent.log": [f"{J_TS_A} hello world"]})
        assert _parse_iso(events[0]["ts"]) == _parse_iso(J_TS_A)

    def test_log_event_source_is_the_dict_key_label(self):
        events = build_replay_events(
            [], {"review.log": [f"{J_TS_A} reviewed"], "agent.log": [f"{J_TS_B} ran"]}
        )
        assert _seq(events) == [
            ("review.log", _parse_iso(J_TS_A)),
            ("agent.log", _parse_iso(J_TS_B)),
        ]
        assert events[0]["source"] == "review.log"
        assert events[1]["source"] == "agent.log"


# ---------------------------------------------------------------------------
# journal-only input
# ---------------------------------------------------------------------------


class TestJournalOnly:
    def test_journal_only_yields_one_event_per_entry_in_ts_order(self):
        entries = [
            {"step": "step-two", "summary": "second", "ts": J_TS_B, "next_hint": None},
            {"step": "step-one", "summary": "first", "ts": J_TS_A, "next_hint": None},
        ]
        events = build_replay_events(entries, {})
        assert _seq(events) == [("journal", J_TS_B), ("journal", J_TS_A)]
        assert [e["message"] for e in events] == ["second", "first"]

    def test_journal_kind_is_the_entry_step(self):
        events = build_replay_events(
            [{"step": "implement", "summary": "did things", "ts": J_TS_A}], {}
        )
        assert events[0]["kind"] == "implement"

    def test_journal_ts_passes_through_unchanged(self):
        ts = "2025-01-01T10:00:00+05:00"
        events = build_replay_events(
            [{"step": "s", "summary": "sum", "ts": ts, "next_hint": None}], {}
        )
        assert events[0]["ts"] == ts

    def test_next_hint_is_folded_into_message(self):
        events = build_replay_events(
            [{"step": "s", "summary": "did things", "ts": J_TS_A, "next_hint": "run tests"}],
            {},
        )
        assert events[0]["message"] == "did things; next: run tests"

    def test_empty_string_next_hint_is_not_appended(self):
        events = build_replay_events(
            [{"step": "s", "summary": "did things", "ts": J_TS_A, "next_hint": ""}], {}
        )
        assert events[0]["message"] == "did things"

    def test_none_next_hint_is_not_appended(self):
        events = build_replay_events(
            [{"step": "s", "summary": "did things", "ts": J_TS_A, "next_hint": None}], {}
        )
        assert events[0]["message"] == "did things"

    def test_non_string_next_hint_is_not_appended(self):
        events = build_replay_events(
            [{"step": "s", "summary": "did things", "ts": J_TS_A, "next_hint": 7}], {}
        )
        assert events[0]["message"] == "did things"

    def test_missing_next_hint_key_is_not_appended(self):
        events = build_replay_events([{"step": "s", "summary": "did things", "ts": J_TS_A}], {})
        assert events[0]["message"] == "did things"

    def test_journal_entry_missing_ts_sorts_as_none_first(self):
        entries = [
            {"step": "timed", "summary": "has ts", "ts": J_TS_A, "next_hint": None},
            {"step": "untimed", "summary": "no ts", "next_hint": None},
        ]
        events = build_replay_events(entries, {})
        assert _seq(events) == [("journal", None), ("journal", J_TS_A)]

    def test_journal_entry_with_malformed_ts_is_ts_none_not_a_crash(self):
        entries = [
            {"step": "s", "summary": "bad ts", "ts": "not-a-date", "next_hint": None},
            {"step": "s", "summary": "good ts", "ts": J_TS_A, "next_hint": None},
        ]
        events = build_replay_events(entries, {})
        assert events[0]["ts"] is None
        assert events[0]["message"] == "bad ts"
        assert events[1]["ts"] == J_TS_A

    def test_journal_entry_missing_summary_still_yields_a_string_message(self):
        events = build_replay_events([{"step": "s1", "ts": J_TS_A}], {})
        assert len(events) == 1
        assert events[0]["kind"] == "s1"
        assert isinstance(events[0]["message"], str)

    def test_journal_entry_missing_step_has_kind_none(self):
        events = build_replay_events([{"summary": "sum", "ts": J_TS_A}], {})
        assert events[0]["kind"] is None

    def test_non_dict_journal_entries_are_skipped_without_raising(self):
        entries = [None, "garbage", 42, {"step": "s", "summary": "real", "ts": J_TS_A}]
        events = build_replay_events(entries, {})
        assert len(events) == 1
        assert events[0]["message"] == "real"

    def test_equal_ts_journal_entries_keep_input_order(self):
        entries = [
            {"step": "a", "summary": "first", "ts": J_TS_A, "next_hint": None},
            {"step": "b", "summary": "second", "ts": J_TS_A, "next_hint": None},
        ]
        events = build_replay_events(entries, {})
        assert [e["message"] for e in events] == ["first", "second"]


# ---------------------------------------------------------------------------
# log lines with ISO timestamps
# ---------------------------------------------------------------------------


class TestTimestampedLogLines:
    def test_each_timestamped_line_becomes_one_event(self):
        sources = {
            "agent.log": [f"{J_TS_A} boot ok", f"{J_TS_C} done"],
            "review.log": [f"{J_TS_B} looks fine"],
        }
        events = build_replay_events([], sources)
        assert len(events) == 3
        assert _seq(events) == [
            ("agent.log", _parse_iso(J_TS_A)),
            ("review.log", _parse_iso(J_TS_B)),
            ("agent.log", _parse_iso(J_TS_C)),
        ]

    def test_message_is_the_remainder_after_the_timestamp(self):
        events = build_replay_events([], {"agent.log": [f"{J_TS_A} hello world"]})
        assert events[0]["message"] == "hello world"

    def test_bare_timestamp_line_yields_empty_message(self):
        events = build_replay_events([], {"agent.log": [J_TS_A]})
        assert len(events) == 1
        assert events[0]["message"] == ""

    def test_trailing_z_timestamp_is_accepted(self):
        events = build_replay_events([], {"agent.log": [f"{J_TS_A} with zulu"]})
        assert events[0]["ts"] is not None

    def test_naive_timestamp_is_accepted(self):
        events = build_replay_events(
            [], {"agent.log": ["2025-01-01T10:00:00 naive ok"]}
        )
        assert events[0]["ts"] is not None

    def test_offset_timestamp_is_accepted(self):
        events = build_replay_events(
            [], {"agent.log": ["2025-01-01T10:00:00+00:00 offset ok"]}
        )
        assert events[0]["ts"] is not None

    def test_line_with_timestamp_not_at_start_is_a_continuation(self):
        sources = {"agent.log": [f"{J_TS_A} first", "step one 2025-01-01T10:00:00Z"]}
        events = build_replay_events([], sources)
        assert len(events) == 1
        assert events[0]["message"] == "first\nstep one 2025-01-01T10:00:00Z"

    def test_equal_ts_lines_within_one_source_keep_line_order(self):
        sources = {"agent.log": [f"{J_TS_A} one", f"{J_TS_A} two"]}
        events = build_replay_events([], sources)
        assert _messages(events, "agent.log") == ["one", "two"]


# ---------------------------------------------------------------------------
# continuation lines
# ---------------------------------------------------------------------------


class TestContinuationLines:
    def test_continuation_appends_to_previous_event_of_same_source(self):
        sources = {
            "agent.log": [f"{J_TS_A} first line", "indented detail", "more detail"],
        }
        events = build_replay_events([], sources)
        assert len(events) == 1
        assert events[0]["message"] == "first line\nindented detail\nmore detail"

    def test_continuation_does_not_leak_into_another_source(self):
        sources = {
            "agent.log": [f"{J_TS_A} agent event"],
            "review.log": [f"{J_TS_B} review line", "review detail"],
        }
        events = build_replay_events([], sources)
        assert _messages(events, "agent.log") == ["agent line"]
        assert _messages(events, "review.log") == ["review line\nreview detail"]

    def test_continuation_attaches_to_same_source_even_when_out_of_ts_order(self):
        # review.log's event sorts BEFORE agent.log's, but the continuation
        # must still attach to review.log's own previous event.
        sources = {
            "agent.log": [f"{J_TS_C} agent late"],
            "review.log": [f"{J_TS_A} review early", "review detail"],
        }
        events = build_replay_events([], sources)
        assert _messages(events, "review.log") == ["review early\nreview detail"]
        assert _messages(events, "agent.log") == ["agent late"]

    def test_leading_garbage_before_first_timestamp_becomes_ts_none_event(self):
        sources = {"agent.log": ["not-a-date hello", f"{J_TS_A} real line"]}
        events = build_replay_events([], sources)
        assert events[0]["ts"] is None
        assert events[0]["source"] == "agent.log"
        assert events[0]["message"] == "not-a-date hello"
        assert _parse_iso(events[1]["ts"]) == _parse_iso(J_TS_A)

    def test_ts_none_events_sort_before_all_timed_events(self):
        sources = {
            "agent.log": ["garbage a", f"{J_TS_C} agent timed"],
            "review.log": ["garbage r", f"{J_TS_A} review timed"],
        }
        events = build_replay_events([], sources)
        untimed = [e for e in events if e["ts"] is None]
        assert untimed, "leading garbage must surface as ts=None events"
        assert events[: len(untimed)] == untimed
        assert [e["source"] for e in untimed] == ["agent.log", "review.log"]

    def test_ts_none_events_keep_source_insertion_order(self):
        sources = {
            "zeta.log": ["z garbage"],
            "alpha.log": ["a garbage"],
        }
        events = build_replay_events([], sources)
        untimed = [e for e in events if e["ts"] is None]
        assert [e["source"] for e in untimed] == ["zeta.log", "alpha.log"]

    def test_journal_bad_ts_sorts_before_source_garbage_in_ts_none_block(self):
        entries = [{"step": "s", "summary": "bad", "ts": "not-a-date", "next_hint": None}]
        sources = {"agent.log": ["garbage", f"{J_TS_A} timed"]}
        events = build_replay_events(entries, sources)
        untimed = [e for e in events if e["ts"] is None]
        assert [e["source"] for e in untimed] == ["journal", "agent.log"]


# ---------------------------------------------------------------------------
# interleaving / global sort
# ---------------------------------------------------------------------------


class TestInterleavedMerge:
    def test_journal_ts_between_two_log_timestamps_lands_in_the_middle(self):
        entries = [{"step": "s", "summary": "mid", "ts": J_TS_B, "next_hint": None}]
        sources = {"agent.log": [f"{J_TS_A} early", f"{J_TS_C} late"]}
        events = build_replay_events(entries, sources)
        assert _seq(events) == [
            ("agent.log", _parse_iso(J_TS_A)),
            ("journal", J_TS_B),
            ("agent.log", _parse_iso(J_TS_C)),
        ]

    def test_full_interleave_across_three_sources(self):
        entries = [
            {"step": "j1", "summary": "j one", "ts": J_TS_A, "next_hint": None},
            {"step": "j2", "summary": "j two", "ts": J_TS_C, "next_hint": None},
        ]
        sources = {
            "agent.log": [f"{J_TS_B} agent mid"],
            "review.log": [f"{J_TS_A} review at a", f"{J_TS_C} review at c"],
        }
        events = build_replay_events(entries, sources)
        assert _seq(events) == [
            ("journal", J_TS_A),
            ("review.log", _parse_iso(J_TS_A)),
            ("agent.log", _parse_iso(J_TS_B)),
            ("journal", J_TS_C),
            ("review.log", _parse_iso(J_TS_C)),
        ]

    def test_duplicate_identical_timestamps_across_sources_are_stable(self):
        entries = [{"step": "s", "summary": "j", "ts": J_TS_A, "next_hint": None}]
        sources = {
            "agent.log": [f"{J_TS_A} agent"],
            "review.log": [f"{J_TS_A} review"],
        }
        events = build_replay_events(entries, sources)
        assert [e["source"] for e in events] == ["journal", "agent.log", "review.log"]

    def test_mixed_aware_and_naive_timestamps_do_not_raise(self):
        sources = {
            "agent.log": [
                "2025-01-01T09:00:00 naive early",
                "2025-01-01T10:00:00Z aware later",
            ]
        }
        events = build_replay_events([], sources)
        assert len(events) == 2
        assert events[0]["message"].endswith("naive early")

    def test_mixed_aware_and_naive_sort_chronologically(self):
        sources = {
            "agent.log": [
                "2025-01-01T10:00:00Z aware",
                "2025-01-01T09:00:00 naive",
            ]
        }
        events = build_replay_events([], sources)
        assert events[0]["message"].endswith("naive")
        assert events[1]["message"].endswith("aware")


# ---------------------------------------------------------------------------
# empty / None-ish / hostile inputs
# ---------------------------------------------------------------------------


class TestEmptyAndHostileInputs:
    def test_empty_journal_and_empty_sources_return_empty_list(self):
        assert build_replay_events([], {}) == []

    def test_dict_with_empty_lists_returns_empty_list(self):
        assert build_replay_events([], {"agent.log": [], "review.log": []}) == []

    def test_none_inputs_return_empty_list(self):
        assert build_replay_events(None, None) == []

    def test_none_journal_with_real_sources_still_yields_log_events(self):
        events = build_replay_events(None, {"agent.log": [f"{J_TS_A} boot"]})
        assert len(events) == 1
        assert events[0]["source"] == "agent.log"

    def test_empty_source_among_non_empty_ones_is_ignored(self):
        events = build_replay_events(
            [], {"agent.log": [f"{J_TS_A} ok"], "review.log": []}
        )
        assert [e["source"] for e in events] == ["agent.log"]

    def test_weird_log_lines_never_raise(self):
        sources = {"agent.log": ["", "   ", None, 42, f"{J_TS_A} ok"]}
        events = build_replay_events([], sources)  # must not raise
        assert isinstance(events, list)

    def test_weird_journal_entries_never_raise(self):
        entries = [{"step": None, "summary": None, "ts": None, "next_hint": None}]
        events = build_replay_events(entries, {})
        assert len(events) == 1
        assert events[0]["ts"] is None
        assert events[0]["kind"] is None
        assert isinstance(events[0]["message"], str)

    def test_keyword_arguments_match_the_documented_signature(self):
        events = build_replay_events(
            journal_entries=[{"step": "s", "summary": "sum", "ts": J_TS_A}],
            log_sources={},
        )
        assert len(events) == 1
        assert events[0]["source"] == "journal"