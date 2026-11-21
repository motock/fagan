"""fcr-01: the plan summary counts the story whose merge completes the plan.

With a durable remote consumer the ``story_merged`` history row is written
asynchronously, so ``notify_if_plan_completed`` can read a history that lacks
the merge it was just called for.  ``just_merged`` names that story so the
summary counts it without ever writing the synthesized record to the store.
"""
# ruff: noqa: I001
import re

import pytest

from pipeline import server as p
import pipeline.plan_completion as ppc
from tests.unit._pipeline_mcp_server_test_helpers import (  # noqa: F401
    _hermetic_ollama_seams,
    _isolate_usage_state,
    _NullSetStateProvider,
    _story,
    _write_manifest,
    plan_dir,
)
from tests.unit.test_plan_completion_wiring import (
    _pr_open_story,
    _stub_advance_merge_boundaries,
    _stub_approve_merge_boundaries,
)

PLAN = "just-merged-plan"


def _manifest(*keys):
    return {"stories": {k: {"status": "done", "title": k} for k in keys}}


def _merged(key):
    return {
        "event": "story_merged",
        "story_key": key,
        "ts": "2026-11-21T14:15:30+00:00",
        "plan": PLAN,
        "message": f"{key} merged",
    }


class _Records:
    def __init__(self, history):
        self.history = history
        self.writes = []

    def get_notification_history(self, namespace, plan_name):
        return list(self.history), None

    def is_plan_completed(self, namespace, plan_name):
        return False

    def mark_plan_completed(self, namespace, plan_name):
        return True

    def put_plan_report(self, namespace, plan_name, text):
        self.writes.append(("report", text))

    def get_journal(self, namespace, plan_name, story_key):
        return []

    def append_notification(self, *args, **kwargs):
        self.writes.append(("notification", args, kwargs))


@pytest.fixture
def harness(monkeypatch):
    class _H:
        def __init__(self):
            self.records = None
            self.summaries = []

        def use(self, history):
            self.records = _Records(history)
            records = self.records

            class _Store:
                def records(self):
                    return records

            monkeypatch.setattr(p, "_store", _Store())
            monkeypatch.setattr(ppc, "build_store", lambda *a, **k: _Store())

    h = _H()
    monkeypatch.setattr(ppc, "_notify_user", lambda plan, msg, **kw: h.summaries.append(msg))
    return h


def _merged_count(summary):
    match = re.search(r"stories_merged\D*(\d+)", summary)
    assert match, summary
    return int(match.group(1))


def test_should_count_the_just_merged_story_when_history_has_not_caught_up(harness):
    harness.use([_merged("S1")])  # S2's merge row has not landed yet

    assert ppc.notify_if_plan_completed(PLAN, _manifest("S1", "S2"), just_merged="S2")

    assert _merged_count(harness.summaries[0]) == 2


def test_should_not_double_count_when_history_already_has_the_merge(harness):
    harness.use([_merged("S1"), _merged("S2")])

    ppc.notify_if_plan_completed(PLAN, _manifest("S1", "S2"), just_merged="S2")

    assert _merged_count(harness.summaries[0]) == 2


def test_should_not_add_a_merge_when_just_merged_is_not_given(harness):
    harness.use([_merged("S1")])

    ppc.notify_if_plan_completed(PLAN, _manifest("S1", "S2"))

    assert _merged_count(harness.summaries[0]) == 1


def test_should_not_write_the_synthesized_record_to_the_store(harness):
    harness.use([])

    ppc.notify_if_plan_completed(PLAN, _manifest("S1"), just_merged="S1")

    assert harness.records.history == []
    assert [w for w in harness.records.writes if w[0] == "notification"] == []


def test_should_synthesize_a_record_shaped_like_a_notification(harness, monkeypatch):
    harness.use([])
    seen = []
    real = ppc.format_plan_summary
    monkeypatch.setattr(
        ppc, "format_plan_summary", lambda n, m, records: (seen.extend(records), real(n, m, records))[1]
    )
    manifest = _manifest("S1")
    manifest["stories"]["S1"]["correlation_id"] = "corr-1"

    ppc.notify_if_plan_completed(PLAN, manifest, just_merged="S1")

    (record,) = seen
    assert (record["event"], record["story_key"], record["plan"]) == ("story_merged", "S1", PLAN)
    assert record["correlation_id"] == "corr-1"
    assert record["ts"] and record["message"]


def test_should_pass_just_merged_from_approve_merge(plan_dir, monkeypatch):  # noqa: F811
    _stub_approve_merge_boundaries(monkeypatch)
    calls = []
    monkeypatch.setattr(
        ppc, "notify_if_plan_completed",
        lambda plan_name, manifest, **kw: calls.append(kw),
    )
    _write_manifest(plan_dir, "jm-approve", {
        "S1": _story(status="parked", review_verdict="APPROVE", review_verdict_sha="stub-head", worktree="/nonexistent-fcr01-worktree"),
    })

    assert p.approve_merge("jm-approve", "S1").get("ok") is True

    assert calls == [{"just_merged": "S1"}]


def test_should_pass_just_merged_from_advance_merge(plan_dir, monkeypatch):  # noqa: F811
    _stub_advance_merge_boundaries(monkeypatch)
    calls = []
    monkeypatch.setattr(
        ppc, "notify_if_plan_completed",
        lambda plan_name, manifest, **kw: calls.append(kw),
    )
    _write_manifest(plan_dir, "jm-advance", {"P1": _pr_open_story()})

    assert p.advance_pipeline("jm-advance")["merged"] == ["P1"]

    assert calls == [{"just_merged": "P1"}]


@pytest.mark.parametrize(
    ("story_fields", "expected"),
    [
        ({"status": "pr_open", "pr_url": "https://example.test/pr/1"}, [{"just_merged": "S1"}]),
        ({"status": "todo"}, [{}]),
    ],
    ids=["merged_outside_pipeline", "not_merged_outside_pipeline"],
)
def test_should_pass_just_merged_from_mark_done_only_when_merged_outside_the_pipeline(
    plan_dir, monkeypatch, story_fields, expected  # noqa: F811
):
    monkeypatch.setattr(p, "get_ticket_provider", lambda: _NullSetStateProvider())
    calls = []
    monkeypatch.setattr(
        ppc, "notify_if_plan_completed",
        lambda plan_name, manifest, **kw: calls.append(kw),
    )
    _write_manifest(plan_dir, "jm-done", {"S1": story_fields})

    assert p.mark_story_done("jm-done", "S1").get("ok") is True

    assert calls == expected
