"""PLANREPORT-01: durable ``<plan>.report.md`` written on plan completion.

``notify_if_plan_completed`` emits the ``plan_completed`` notification and, in
addition, writes the very same summary to ``PLAN_DIR / "<plan>.report.md"`` as
a durable per-plan record for retros.  The file is a *record*, not the
delivery: writing it is best effort, a failure is logged at WARNING with
``exc_info`` and must never block the notification or the once-only marker.

Covered here:

* happy path - a completed plan writes ``<plan>.report.md`` under PLAN_DIR
  whose content is exactly the notified message plus one trailing newline;
* the report is written *before* the notification is emitted (the edit places
  the write directly after the summary is rendered);
* the file name is derived from ``plan_name`` (not hard-coded);
* the content is written as UTF-8 (non-ASCII summaries round-trip);
* negative - an incomplete plan, a plan whose marker already exists, and a
  formatter failure all write no report;
* once-only - a second call (marker present) does not rewrite the report;
* failure - when the report write raises ``OSError`` (the path is a
  directory) the notification is still emitted, the marker is still created,
  the function still returns ``True``, and a WARNING carrying ``exc_info`` is
  logged;
* docs - the module docstring and the REFERENCE.md notification section both
  document the report file.

``_notify_user``, ``format_plan_summary`` and the notification-record loader
are stubbed on the module under test; ``PLAN_DIR`` is monkeypatched to
``tmp_path`` so no test touches the real plan directory.
"""

import logging
from pathlib import Path

import pytest

from pipeline import plan_completion, story_metrics

PLAN = "planreport01"
REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "REFERENCE.md"

# The sentence the REFERENCE.md edit anchors on; the new sentence must land in
# the same paragraph, immediately after it.
REFERENCE_ANCHOR = "but no second notification is ever emitted."
REFERENCE_NEW_SENTENCE = (
    "The same summary is also written to `<plan>.report.md` in `PLAN_DIR`"
)


def _manifest(statuses=None, *, include_stories=True):
    """Build a manifest dict; ``statuses`` maps story_key -> status."""
    manifest = {"title": "Planreport 01 plan"}
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
        self.summary_text = f"SUMMARY[{PLAN}]"
        # Snapshot of whether the report file existed when _notify_user ran.
        self.report_existed_at_notify = []

        monkeypatch.setattr(plan_completion, "PLAN_DIR", tmp_path)

        def fake_notify(plan_name, message, **kwargs):
            self.notify_calls.append((plan_name, message, kwargs))
            self.report_existed_at_notify.append(
                (tmp_path / f"{plan_name}.report.md").exists()
            )

        def fake_summary(plan_name, manifest, records):
            self.summary_calls.append((plan_name, manifest, records))
            return self.summary_text

        monkeypatch.setattr(plan_completion, "_notify_user", fake_notify)
        monkeypatch.setattr(plan_completion, "format_plan_summary", fake_summary)

        def fake_loader(path):
            self.loader_paths.append(path)
            return self.records_to_serve

        monkeypatch.setattr(story_metrics, "load_notification_records", fake_loader)
        # The implementation may import the loader by name, in which case it
        # holds its own binding on the module under test - patch that too.
        if hasattr(plan_completion, "load_notification_records"):
            monkeypatch.setattr(
                plan_completion, "load_notification_records", fake_loader
            )

    def marker_path(self, plan_name=PLAN):
        return self.tmp_path / f"{plan_name}.plan_completed"

    def report_path(self, plan_name=PLAN):
        return self.tmp_path / f"{plan_name}.report.md"

    def call(self, manifest, plan_name=PLAN):
        return plan_completion.notify_if_plan_completed(plan_name, manifest)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return _Harness(tmp_path, monkeypatch)


# ---------- happy path ----------


def test_completed_plan_writes_report_file_under_plan_dir(harness):
    manifest = _manifest({"s1": "done", "s2": "done"})

    result = harness.call(manifest)

    assert result is True
    report = harness.report_path()
    assert report.exists(), f"report file {report} not written for a completed plan"
    assert report.is_file()


def test_report_content_is_notified_message_plus_trailing_newline(harness):
    manifest = _manifest({"s1": "done", "s2": "done"})

    harness.call(manifest)

    assert len(harness.notify_calls) == 1
    _, message, _ = harness.notify_calls[0]
    content = harness.report_path().read_text(encoding="utf-8")
    assert content == message + "\n"
    assert content == harness.summary_text + "\n"


def test_report_is_written_before_the_notification_is_emitted(harness):
    manifest = _manifest({"s1": "done"})

    harness.call(manifest)

    assert harness.report_existed_at_notify == [True], (
        "the report must be written before _notify_user is called"
    )


def test_report_file_name_uses_the_plan_name(harness):
    other = "planreport01other"
    manifest = _manifest({"s1": "done"})

    result = harness.call(manifest, plan_name=other)

    assert result is True
    assert harness.report_path(other).exists()
    assert not harness.report_path(PLAN).exists()


def test_report_content_is_written_as_utf8(harness):
    harness.summary_text = "SUMMARY[pl\u00e2n] \u2014 caf\u00e9 \u2713"
    manifest = _manifest({"s1": "done"})

    harness.call(manifest)

    raw = harness.report_path().read_bytes()
    assert raw.decode("utf-8") == harness.summary_text + "\n"


def test_single_story_done_boundary_writes_report(harness):
    result = harness.call(_manifest({"only": "done"}))

    assert result is True
    assert harness.report_path().read_text(encoding="utf-8") == (
        harness.summary_text + "\n"
    )


# ---------- negative / boundary ----------


def test_incomplete_plan_writes_no_report(harness):
    manifest = _manifest({"s1": "done", "s2": "in_progress"})

    result = harness.call(manifest)

    assert result is False
    assert harness.notify_calls == []
    assert not harness.report_path().exists()


def test_empty_stories_dict_writes_no_report(harness):
    result = harness.call(_manifest(include_stories=True))

    assert result is False
    assert not harness.report_path().exists()


def test_manifest_missing_stories_key_writes_no_report(harness):
    result = harness.call(_manifest(include_stories=False))

    assert result is False
    assert not harness.report_path().exists()


def test_marker_already_present_writes_no_report(harness):
    harness.marker_path().write_text("sent")
    manifest = _manifest({"s1": "done", "s2": "done"})

    result = harness.call(manifest)

    assert result is False
    assert harness.notify_calls == []
    assert not harness.report_path().exists()


def test_formatter_failure_writes_no_report(harness, monkeypatch):
    manifest = _manifest({"s1": "done"})

    def boom(plan_name, manifest, records):
        raise RuntimeError("summary exploded")

    monkeypatch.setattr(plan_completion, "format_plan_summary", boom)

    result = harness.call(manifest)

    assert result is False
    assert not harness.report_path().exists()


# ---------- once-only ----------


def test_second_call_does_not_rewrite_the_report(harness):
    manifest = _manifest({"s1": "done", "s2": "done"})

    first = harness.call(manifest)
    assert first is True
    report = harness.report_path()
    assert report.exists()

    # Change the content between calls; a rewrite would clobber this sentinel.
    sentinel = "SENTINEL - must survive the second call\n"
    report.write_text(sentinel, encoding="utf-8")

    second = harness.call(manifest)

    assert second is False
    assert len(harness.notify_calls) == 1
    assert report.read_text(encoding="utf-8") == sentinel


# ---------- write failure is best effort ----------


def test_report_write_oserror_still_notifies_marks_and_returns_true(
    harness, caplog
):
    manifest = _manifest({"s1": "done", "s2": "done"})
    # A directory at the report path makes write_text raise IsADirectoryError
    # (an OSError) without touching the notification path.
    harness.report_path().mkdir()

    with caplog.at_level(logging.WARNING):
        result = harness.call(manifest)

    assert result is True
    assert len(harness.notify_calls) == 1
    plan_name, message, kwargs = harness.notify_calls[0]
    assert plan_name == PLAN
    assert message == harness.summary_text
    assert kwargs["event"] == "plan_completed"
    assert harness.marker_path().exists(), (
        "a failed report write must not block the once-only marker"
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "no WARNING logged when the report write failed"
    assert any(PLAN in r.getMessage() for r in warnings), (
        "the WARNING must name the plan"
    )
    assert any(r.exc_info is not None for r in warnings), (
        "the WARNING must carry exc_info=True"
    )


def test_report_write_oserror_does_not_raise(harness):
    manifest = _manifest({"s1": "done"})
    harness.report_path().mkdir()

    # Must not propagate: the function never raises inside a scheduler tick.
    assert harness.call(manifest) is True


# ---------- docs ----------


def test_module_docstring_documents_the_report_file():
    doc = plan_completion.__doc__ or ""
    low = doc.lower()
    assert ".report.md" in doc, "module docstring must mention <plan>.report.md"
    assert "PLAN_DIR" in doc, "module docstring must mention PLAN_DIR"
    assert "best effort" in low or "best-effort" in low, (
        "module docstring must say the write is best effort"
    )
    assert "does not block" in low or "never blocks" in low, (
        "module docstring must say a write failure does not block the notification"
    )


def test_reference_md_documents_the_report_file():
    text = REFERENCE.read_text(encoding="utf-8")
    assert "## Notifications: plan_completed and the outbound e-mail channel" in text
    assert REFERENCE_ANCHOR in text, "REFERENCE.md anchor sentence is missing"

    anchor_end = text.index(REFERENCE_ANCHOR) + len(REFERENCE_ANCHOR)
    assert REFERENCE_NEW_SENTENCE in text, (
        "REFERENCE.md must document the <plan>.report.md report file"
    )
    new_start = text.index(REFERENCE_NEW_SENTENCE)
    assert new_start > anchor_end, (
        "the report sentence must follow the 'no second notification' sentence"
    )
    between = text[anchor_end:new_start]
    assert "\n\n" not in between, (
        "the report sentence must be appended to the same paragraph"
    )
    appended = text[new_start : new_start + 400].lower()
    assert "best effort" in appended or "best-effort" in appended, (
        "REFERENCE.md must say the report write is best effort"
    )
    assert "does not block" in appended or "never blocks" in appended, (
        "REFERENCE.md must say a write failure never blocks the notification"
    )
