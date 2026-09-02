"""Structured ``event=`` names on story-lifecycle notifications.

Wiring story: every story-lifecycle ``_notify_user(...)`` call in
``pipeline/advance.py`` must carry a structured ``event="<name>"`` keyword
drawn from a fixed vocabulary, so the notifications JSONL sidecar can drive
cost-per-merged-story metrics.  One NEW notification (``story_merged``) is
added on the successful merge path, immediately after the block that calls
``_mark_plane_done``.

Two grades live in this module:

(a) source-level: an AST walk over ``pipeline/advance.py`` asserting that
    every ``_notify_user`` call whose static message matches the vocabulary
    is stamped with exactly the right ``event=`` name (and that no call is
    wrongly stamped), plus a grep-style ``inspect.getsource`` count per
    vocabulary name.  Membership/per-entry counts only -- never a total
    count of ``_notify_user`` calls, this file is shared with later work.
(b) behavioral: real ``advance_pipeline`` call paths with stubbed
    manifest/store driving the "tests failed" notification, the merged
    path, and the merge-gate failure path.
"""

import ast
import contextlib
import inspect
import json
from functools import lru_cache

import pytest

from pipeline import advance

# --- the vocabulary (exact strings; the metrics story depends on them) -----

# (message substring, event name) pairs, most specific first.
SUBSTRING_VOCABULARY = (
    ("merge-gate CI failed", "merge_ci_rework"),
    ("merge gate failed", "merge_gate_failed"),
    ("merge gate attempt", "merge_gate_retry"),
    ("merge failed", "merge_failed"),
    ("merge attempt", "merge_retry"),
    ("dispatch failed", "dispatch_failed"),
    ("escalating to", "escalated"),
    ("agent gave up", "agent_gave_up"),
)

# "retrying on" AND "fallback model" together -> model_fallback
MODEL_FALLBACK_MARKERS = ("retrying on", "fallback model")
# message is exactly "<key> tests failed" (endswith) -> tests_failed
TESTS_FAILED_SUFFIX = "tests failed"
STORY_MERGED_EVENT = "story_merged"

ALL_EVENTS = frozenset(
    [event for _, event in SUBSTRING_VOCABULARY] + ["model_fallback", "tests_failed"]
)


# --- source helpers ---------------------------------------------------------


@lru_cache(maxsize=1)
def _source() -> str:
    return inspect.getsource(advance)


@lru_cache(maxsize=1)
def _tree() -> ast.Module:
    return ast.parse(_source())


@lru_cache(maxsize=1)
def _notify_calls() -> tuple:
    return tuple(
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and _call_name(node.func) == "_notify_user"
    )


def _call_name(func) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _static_message(call):
    """Best-effort static text of the message arg (2nd positional).

    f-string holes become ``{}`` so the skeleton keeps the exact character
    stream of the runtime message.  Returns None when the message is not
    statically readable (e.g. built by a helper call) -- such sites are
    skipped, matching the brief's "leave unmatched call sites alone".
    """
    if len(call.args) < 2:
        return None
    node = call.args[1]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                parts.append("{}")
        return "".join(parts)
    return None


def _kwarg_value(call, name):
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _expected_event(message):
    """Map a static message skeleton to its vocabulary event name (or None)."""
    if message is None:
        return None
    if all(marker in message for marker in MODEL_FALLBACK_MARKERS):
        return "model_fallback"
    if message.endswith(TESTS_FAILED_SUFFIX):
        return "tests_failed"
    for substring, event in SUBSTRING_VOCABULARY:
        if substring in message:
            return event
    return None


# --- (a) source-level assertions --------------------------------------------


class TestSourceLevelEventStamps:
    def test_every_vocabulary_call_site_is_stamped(self):
        problems = []
        for call in _notify_calls():
            message = _static_message(call)
            expected = _expected_event(message)
            if expected is None:
                continue
            actual = _kwarg_value(call, "event")
            if actual != expected:
                problems.append(
                    f"line {call.lineno}: message {message!r} must carry "
                    f'event="{expected}" (found {actual!r})'
                )
        assert not problems, (
            "pipeline/advance.py notification event= stamps missing/wrong:\n"
            + "\n".join(problems)
        )

    def test_no_vocabulary_event_on_unmatched_message(self):
        problems = []
        for call in _notify_calls():
            message = _static_message(call)
            actual = _kwarg_value(call, "event")
            if actual in ALL_EVENTS and _expected_event(message) != actual:
                problems.append(
                    f"line {call.lineno}: event={actual!r} stamped on message "
                    f"{message!r}, which does not match that vocabulary entry"
                )
        assert not problems, "\n".join(problems)

    def test_each_vocabulary_event_grep_count(self):
        source = _source()
        problems = []
        for event in sorted(ALL_EVENTS):
            needle = f'event="{event}"'
            expected_count = sum(
                1
                for call in _notify_calls()
                if _expected_event(_static_message(call)) == event
            )
            if expected_count == 0:
                problems.append(
                    f"{needle}: no static _notify_user message in "
                    "pipeline/advance.py matches this vocabulary entry"
                )
            elif source.count(needle) != expected_count:
                problems.append(
                    f"{needle}: expected {expected_count} occurrence(s) in "
                    f"pipeline/advance.py, found {source.count(needle)}"
                )
        assert not problems, "\n".join(problems)


# --- the NEW story_merged notification --------------------------------------


class TestStoryMergedNotification:
    def _story_merged_calls(self) -> tuple:
        return tuple(
            call
            for call in _notify_calls()
            if _kwarg_value(call, "event") == STORY_MERGED_EVENT
        )

    def test_story_merged_exists_on_merged_path(self):
        calls = self._story_merged_calls()
        assert calls, (
            'expected a _notify_user(..., event="story_merged") call on the '
            "successful merge path in pipeline/advance.py"
        )
        for call in calls:
            assert _static_message(call) == "{} merged", (
                f"line {call.lineno}: story_merged message must be "
                f'f"{{key}} merged" (found {_static_message(call)!r})'
            )

    def test_story_merged_uses_conditional_correlation_id_pattern(self):
        calls = self._story_merged_calls()
        assert calls, 'no event="story_merged" call site found'
        for call in calls:
            names = [kw.arg for kw in call.keywords]
            assert "story_key" in names, (
                f"line {call.lineno}: story_merged must pass story_key=key"
            )
            assert "correlation_id" not in names, (
                f"line {call.lineno}: story_merged must use the conditional "
                "** correlation_id dict pattern, not a bare correlation_id="
            )
            spreads = [kw for kw in call.keywords if kw.arg is None]
            assert spreads, (
                f"line {call.lineno}: story_merged must pass the conditional "
                "correlation_id dict via **"
            )
            spread_src = ast.get_source_segment(_source(), spreads[0].value) or ""
            assert "correlation_id" in spread_src and "else" in spread_src, (
                f"line {call.lineno}: correlation_id must be conditional "
                f"(found {spread_src!r})"
            )

    def test_story_merged_follows_mark_plane_done(self):
        calls = self._story_merged_calls()
        assert calls, 'no event="story_merged" call site found'
        mark_calls = [
            node
            for node in ast.walk(_tree())
            if isinstance(node, ast.Call)
            and _call_name(node.func) == "_mark_plane_done"
        ]
        assert mark_calls, "expected a _mark_plane_done call in pipeline/advance.py"
        first_merged_line = min(call.lineno for call in calls)
        assert any(node.lineno < first_merged_line for node in mark_calls), (
            "story_merged must be emitted after the merge path sets the story "
            "done (the _mark_plane_done block)"
        )


# --- (b) behavioral: real advance_pipeline call paths ------------------------


class _FakeStore:
    """Minimal stand-in for the server store: manifest_path + get_manifest."""

    def __init__(self, plan_dir, plan_name):
        self.plan_dir = plan_dir
        self.plan_name = plan_name

    def manifest_path(self, plan_name):
        return self.plan_dir / f"{plan_name}.manifest.json"

    def get_manifest(self, plan_name):
        return json.loads(self.manifest_path(plan_name).read_text())


class _AutoAutonomy:
    """Stands in for the PIPELINE_AUTONOMY proxy (only _value() is used)."""

    def _value(self):
        return "auto"


def _write_manifest(plan_dir, plan_name, stories):
    (plan_dir / f"{plan_name}.manifest.json").write_text(
        json.dumps({"epics": {}, "stories": stories})
    )


@pytest.fixture
def notify_spy(monkeypatch):
    """Replace advance's _notify_user global with a capturing stub."""
    records = []

    def _capture(plan_name, message, **kwargs):
        records.append({"plan_name": plan_name, "message": message, **kwargs})

    monkeypatch.setattr(advance, "_notify_user", _capture)
    return records


def _stub_tick(monkeypatch, plan_dir, plan_name):
    """Stub everything around the poll/merge loops so each test drives one
    story through exactly one lifecycle branch of _advance_pipeline_locked."""
    monkeypatch.setattr(advance, "_store", _FakeStore(plan_dir, plan_name))
    monkeypatch.setattr(advance, "_role_resource_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(advance, "PIPELINE_AUTONOMY", _AutoAutonomy())
    monkeypatch.setattr(
        advance, "_resolve_dispatch_backend", lambda story, env_backend: "local"
    )
    monkeypatch.setattr(
        advance, "check_story_status", lambda plan_name, key: {"status": "ok"}
    )
    monkeypatch.setattr(advance, "run_triage_sweep", lambda plan_name: None, raising=False)
    monkeypatch.setattr(
        advance, "_auto_escalation_enabled", lambda: False, raising=False
    )
    monkeypatch.setattr(advance, "_maybe_record_retro", lambda *a, **k: None)
    monkeypatch.setattr(
        advance, "_mcp_self_source_touched", lambda *a, **k: False, raising=False
    )
    monkeypatch.setattr(
        advance, "_mcp_restart_notice", lambda *a, **k: "", raising=False
    )
    monkeypatch.setattr(
        advance, "review_story", lambda *a, **k: {"status": "skipped"}
    )
    monkeypatch.setattr(advance, "dispatch_story", lambda *a, **k: {})
    monkeypatch.setattr(advance, "interrupt_story", lambda *a, **k: None)
    monkeypatch.setattr(
        advance,
        "_scoped_repo_root",
        lambda plan_name: contextlib.nullcontext(),
        raising=False,
    )
    monkeypatch.setattr(
        advance,
        "_reverify_acceptance",
        lambda story, worktree, key: {"state": "pass"},
        raising=False,
    )
    monkeypatch.setattr(
        advance,
        "_reverify_build",
        lambda worktree: {"state": "pass"},
        raising=False,
    )


class TestTestsFailedNotificationPath:
    """The "<key> tests failed" notification, driven through the real tick."""

    @staticmethod
    def _drive(monkeypatch, tmp_path, notify_spy, story):
        plan = "evtests"
        _write_manifest(tmp_path, plan, {"S1": story})
        _stub_tick(monkeypatch, tmp_path, plan)
        monkeypatch.setattr(
            advance,
            "check_story_status",
            lambda plan_name, key: {"status": "failed", "failure_kind": "tests"},
        )
        summary = advance._advance_pipeline_locked(plan)
        matches = [r for r in notify_spy if r["message"] == "S1 tests failed"]
        assert len(matches) == 1, notify_spy
        return summary, matches[0]

    def test_event_and_unchanged_correlation_id(
        self, tmp_path, monkeypatch, notify_spy
    ):
        summary, record = self._drive(
            monkeypatch,
            tmp_path,
            notify_spy,
            {"status": "in_progress", "pid": 4242, "correlation_id": "cid-abc"},
        )
        assert record["event"] == "tests_failed"
        assert record["correlation_id"] == "cid-abc"
        # The conditional correlation_id dict is byte-for-byte unchanged: the
        # record gains ONLY the event name - no attempt kwarg, nothing else.
        assert set(record) == {"plan_name", "message", "event", "correlation_id"}
        assert summary["failed"] == ["S1"]

    def test_without_correlation_id_the_dict_stays_empty(
        self, tmp_path, monkeypatch, notify_spy
    ):
        _, record = self._drive(
            monkeypatch, tmp_path, notify_spy, {"status": "in_progress", "pid": 4242}
        )
        assert record["event"] == "tests_failed"
        assert set(record) == {"plan_name", "message", "event"}


class TestStoryMergedBehavioralPath:
    @staticmethod
    def _merge_stubs(monkeypatch, ci_state="success"):
        monkeypatch.setattr(
            advance,
            "_merge_decision",
            lambda story: {"action": "merge", "reason": ""},
        )
        monkeypatch.setattr(
            advance,
            "_rebase_and_push_for_merge",
            lambda plan_name, key, branch, worktree: ("", "sha123"),
        )
        monkeypatch.setattr(
            advance,
            "_merge_gate_ci_status",
            lambda branch, sha=None: {
                "state": ci_state,
                "error": "boom" if ci_state == "fail" else "",
            },
        )
        monkeypatch.setattr(advance, "_merge_pr", lambda *a, **k: True)
        monkeypatch.setattr(advance, "_mark_plane_done", lambda *a, **k: None)

    def test_story_merged_emitted_after_successful_merge(
        self, tmp_path, monkeypatch, notify_spy
    ):
        plan = "evmerge"
        _write_manifest(
            tmp_path, plan, {"S1": {"status": "pr_open", "correlation_id": "cid-m"}}
        )
        _stub_tick(monkeypatch, tmp_path, plan)
        self._merge_stubs(monkeypatch)
        summary = advance._advance_pipeline_locked(plan)

        merged = [r for r in notify_spy if r.get("event") == "story_merged"]
        assert len(merged) == 1, notify_spy
        record = merged[0]
        assert record["message"] == "S1 merged"
        assert record["story_key"] == "S1"
        assert record["correlation_id"] == "cid-m"
        assert summary["merged"] == ["S1"]

    def test_story_merged_without_correlation_id_omits_it(
        self, tmp_path, monkeypatch, notify_spy
    ):
        plan = "evmerge"
        _write_manifest(tmp_path, plan, {"S1": {"status": "pr_open"}})
        _stub_tick(monkeypatch, tmp_path, plan)
        self._merge_stubs(monkeypatch)
        advance._advance_pipeline_locked(plan)

        merged = [r for r in notify_spy if r.get("event") == "story_merged"]
        assert len(merged) == 1, notify_spy
        record = merged[0]
        assert record["message"] == "S1 merged"
        assert record["story_key"] == "S1"
        assert "correlation_id" not in record

    @pytest.mark.parametrize(
        "merge_attempts", [0, advance.MERGE_MAX_ATTEMPTS - 1], ids=["retry", "terminal"]
    )
    def test_no_story_merged_on_merge_gate_failure(
        self, tmp_path, monkeypatch, notify_spy, merge_attempts
    ):
        plan = "evfail"
        story = {"status": "pr_open", "correlation_id": "cid-f"}
        if merge_attempts:
            story["merge_attempts"] = merge_attempts
        _write_manifest(tmp_path, plan, {"S1": story})
        _stub_tick(monkeypatch, tmp_path, plan)
        monkeypatch.delenv("PIPELINE_REWORK_ON_CI_FAIL", raising=False)
        self._merge_stubs(monkeypatch, ci_state="fail")
        advance._advance_pipeline_locked(plan)

        assert not [r for r in notify_spy if r.get("event") == "story_merged"], notify_spy
        gate = [
            r
            for r in notify_spy
            if any(
                marker in r["message"]
                for marker in (
                    "merge-gate CI failed",
                    "merge gate failed",
                    "merge gate attempt",
                    "merge failed",
                    "merge attempt",
                )
            )
        ]
        assert gate, f"merge-gate failure path was not exercised: {notify_spy}"