"""PLANNOTIFY-02: pipeline.plan_completion.notify_if_plan_completed.

Covers the plan-completion detector:

* completion test - every story in ``manifest["stories"]`` has
  ``status == "done"``; an empty/missing ``stories`` dict is NOT complete;
* once-only guard - a ``<plan>.plan_completed`` marker file under PLAN_DIR,
  written only AFTER the notification is emitted, so a failed notify can be
  retried by a later tick;
* body - records come from ``story_metrics.load_notification_records`` and the
  message is ``plan_summary.format_plan_summary(plan_name, manifest, records)``;
* emission - exactly one ``_notify_user(plan_name, summary, event=
  "plan_completed", severity="info", dedup_key="plan_completed:<plan>")``;
* never raises - any failure logs at ERROR with exc_info and returns False.

``_notify_user`` and ``format_plan_summary`` are stubbed on the module under
test; PLAN_DIR is monkeypatched to ``tmp_path`` so no test touches the real
plan directory.
"""

import logging

import pytest

from pipeline import plan_completion, story_metrics

PLAN = "plannotify02"


def _manifest(statuses=None, *, include_stories=True):
    """Build a manifest dict; ``statuses`` maps story_key -> status."""
    manifest = {"title": "Plannotify 02 plan"}
    if include_stories:
        manifest["stories"] = {
            key: {"status": status, "title": f"story {key}"}
            for key, status in (statuses or {}).items()
        }
    return manifest


class _Harness:
    """Stubs + scratch dir for one test."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.notify_calls = []
        self.summary_calls = []
        self.loader_paths = []
        self.records_to_serve = ([{"event": "story_done", "story_key": "s1"}], 0)

        monkeypatch.setattr(plan_completion, "PLAN_DIR", tmp_path)

        def fake_notify(plan_name, message, **kwargs):
            self.notify_calls.append((plan_name, message, kwargs))

        def fake_summary(plan_name, manifest, records):
            self.summary_calls.append((plan_name, manifest, records))
            return f"SUMMARY[{plan_name}]"

        monkeypatch.setattr(plan_completion, "_notify_user", fake_notify)
        monkeypatch.setattr(plan_completion, "format_plan_summary", fake_summary)

        def fake_loader(path):
            self.loader_paths.append(path)
            return self.records_to_serve

        monkeypatch.setattr(story_metrics, "load_notification_records", fake_loader)
        # The implementation may import the loader by name (`from .story_metrics
        # import load_notification_records`), in which case it holds its own
        # binding on the module under test - patch that too when present.
        if hasattr(plan_completion, "load_notification_records"):
            monkeypatch.setattr(
                plan_completion, "load_notification_records", fake_loader
            )

    def marker_path(self, plan_name=PLAN):
        return self.tmp_path / f"{plan_name}.plan_completed"

    def call(self, manifest, plan_name=PLAN):
        return plan_completion.notify_if_plan_completed(plan_name, manifest)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return _Harness(tmp_path, monkeypatch)


# ---------- positive ----------


def test_all_done_returns_true_and_notifies_once_with_plan_completed_event(harness):
    manifest = _manifest({"s1": "done", "s2": "done", "s3": "done"})

    result = harness.call(manifest)

    assert result is True
    assert len(harness.notify_calls) == 1
    plan_name, message, kwargs = harness.notify_calls[0]
    assert plan_name == PLAN
    # The message is the formatter's output, nothing else.
    assert message == f"SUMMARY[{PLAN}]"
    assert kwargs["event"] == "plan_completed"
    assert kwargs["severity"] == "info"
    assert kwargs["dedup_key"] == f"plan_completed:{PLAN}"


def test_formatter_receives_plan_manifest_and_loader_records(harness):
    manifest = _manifest({"s1": "done"})

    harness.call(manifest)

    assert len(harness.summary_calls) == 1
    summary_plan, summary_manifest, summary_records = harness.summary_calls[0]
    assert summary_plan == PLAN
    assert summary_manifest is manifest
    # Records are exactly what load_notification_records returned (its first
    # tuple element), not the malformed count or a re-wrapped dict.
    assert summary_records == harness.records_to_serve[0]
    # The loader reads this plan's JSONL sidecar under the (patched) PLAN_DIR.
    assert harness.loader_paths == [harness.tmp_path / f"{PLAN}.notifications.jsonl"]


def test_single_story_done_boundary(harness):
    result = harness.call(_manifest({"only": "done"}))

    assert result is True
    assert len(harness.notify_calls) == 1


def test_marker_file_written_after_successful_notify(harness):
    manifest = _manifest({"s1": "done", "s2": "done"})

    harness.call(manifest)

    marker = harness.marker_path()
    assert marker.exists(), f"marker file {marker} not created after notify"


# ---------- negative / boundary ----------


def test_one_story_in_progress_returns_false_and_does_not_notify(harness):
    manifest = _manifest({"s1": "done", "s2": "in_progress"})

    result = harness.call(manifest)

    assert result is False
    assert harness.notify_calls == []
    assert not harness.marker_path().exists()


def test_one_story_parked_returns_false_parked_is_not_done(harness):
    manifest = _manifest({"s1": "done", "s2": "parked"})

    result = harness.call(manifest)

    assert result is False
    assert harness.notify_calls == []
    assert not harness.marker_path().exists()


def test_empty_stories_dict_is_not_a_completed_plan(harness):
    result = harness.call(_manifest(include_stories=True))

    assert result is False
    assert harness.notify_calls == []
    assert not harness.marker_path().exists()


def test_manifest_missing_stories_key_returns_false_without_raising(harness):
    result = harness.call(_manifest(include_stories=False))

    assert result is False
    assert harness.notify_calls == []
    assert not harness.marker_path().exists()


def test_marker_already_present_returns_false_and_does_not_notify(harness):
    harness.marker_path().write_text("sent")
    manifest = _manifest({"s1": "done", "s2": "done"})

    result = harness.call(manifest)

    assert result is False
    assert harness.notify_calls == []


def test_second_call_does_not_notify_again(harness):
    manifest = _manifest({"s1": "done", "s2": "done"})

    first = harness.call(manifest)
    second = harness.call(manifest)

    assert first is True
    assert second is False
    assert len(harness.notify_calls) == 1


def test_formatter_raising_returns_false_no_marker_and_logs_error(
    harness, monkeypatch, caplog
):
    manifest = _manifest({"s1": "done"})

    def boom(plan_name, manifest, records):
        raise RuntimeError("summary exploded")

    monkeypatch.setattr(plan_completion, "format_plan_summary", boom)

    with caplog.at_level(logging.ERROR):
        result = harness.call(manifest)

    assert result is False
    assert harness.notify_calls == []
    assert not harness.marker_path().exists()
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "no ERROR logged when format_plan_summary raised"
    assert any(r.exc_info is not None for r in error_records), (
        "ERROR log must carry exc_info=True"
    )
    assert "summary exploded" in caplog.text


def test_notify_user_raising_returns_false_no_marker_and_retry_succeeds(
    harness, monkeypatch
):
    manifest = _manifest({"s1": "done", "s2": "done"})

    def boom(plan_name, message, **kwargs):
        raise OSError("sink down")

    monkeypatch.setattr(plan_completion, "_notify_user", boom)
    first = harness.call(manifest)

    assert first is False
    assert not harness.marker_path().exists(), (
        "marker must not be written when _notify_user fails"
    )

    # A later tick with a working sink can retry and complete the plan.
    monkeypatch.setattr(
        plan_completion,
        "_notify_user",
        lambda plan_name, message, **kwargs: harness.notify_calls.append(
            (plan_name, message, kwargs)
        ),
    )
    second = harness.call(manifest)

    assert second is True
    assert len(harness.notify_calls) == 1
    assert harness.marker_path().exists()