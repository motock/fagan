"""Plan completion through the record store (ADR 0012 decision 3).

``notify_if_plan_completed`` no longer touches ``PLAN_DIR`` itself: the
once-only marker and the durable report are *records* of the namespaced store,
so the completion flow is graded through the public record API.

Covered here:

* a completed plan emits exactly one notice, sets the marker record and
  stores the rendered summary as the report record;
* a second call finds the marker, so it returns ``False`` and emits nothing;
* a failing notice leaves the marker unset, so a later tick retries;
* a failing report write is best effort: the notice is still emitted, the
  marker is still set, and the failure is logged at WARNING;
* an empty (or missing) ``stories`` dict is not a completed plan.

The notification sink is stubbed on the module under test; the record store is
the real one, pointed at a temporary plans directory, so the marker and report
records are read back through the same public API a caller would use.
"""

from __future__ import annotations

import logging

import pytest

from pipeline import persistence, plan_completion, server
from pipeline.store import FileStore
from pipeline.store_records import DEFAULT_NAMESPACE

PLAN = 'plan-completion-flow'


def _manifest(statuses=None, *, include_stories=True):
    """Build a manifest dict; ``statuses`` maps story_key -> status."""
    manifest = {'title': 'Plan completion flow plan'}
    if include_stories:
        manifest['stories'] = {
            key: {'status': status, 'title': f'story {key}'}
            for key, status in (statuses or {}).items()
        }
    return manifest


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Stub the notice sink and point the record store at a scratch dir.

    Returns a small object holding the recorded notify calls and the record
    view the implementation must write through.
    """
    plan_dir = tmp_path / 'plans'
    plan_dir.mkdir()
    monkeypatch.setattr(server, 'PLAN_DIR', plan_dir)
    monkeypatch.setattr(persistence, 'PLAN_DIR', plan_dir)

    calls = []
    monkeypatch.setattr(
        plan_completion,
        '_notify_user',
        lambda plan_name, message, **kwargs: calls.append(
            (plan_name, message, kwargs)
        ),
    )

    records = FileStore().records()
    monkeypatch.setattr(server, '_store', records)
    monkeypatch.setattr(
        plan_completion,
        'build_store',
        lambda *a, **k: records,
    )

    class _Harness:
        plan_dir = plan_dir

        def __init__(self):
            self.notify_calls = calls

        def call(self, manifest, plan_name=PLAN):
            return plan_completion.notify_if_plan_completed(plan_name, manifest)

        def is_completed(self, plan_name=PLAN):
            return records.is_plan_completed(DEFAULT_NAMESPACE, plan_name)

        def report(self, plan_name=PLAN):
            return records.get_plan_report(DEFAULT_NAMESPACE, plan_name)

    return _Harness()


# ---------- happy path ----------


def test_a_completed_plan_notifies_once_and_sets_the_marker(harness):
    manifest = _manifest({'s1': 'done', 's2': 'done'})

    result = harness.call(manifest)

    assert result is True
    assert len(harness.notify_calls) == 1
    plan_name, message, kwargs = harness.notify_calls[0]
    assert plan_name == PLAN
    assert kwargs['event'] == 'plan_completed'
    assert kwargs['severity'] == 'info'
    assert kwargs['dedup_key'] == f'plan_completed:{PLAN}'
    assert harness.is_completed() is True


def test_the_report_record_holds_the_notified_summary(harness):
    manifest = _manifest({'s1': 'done'})

    harness.call(manifest)

    report = harness.report()
    assert report is not None, 'the plan report record must be written'
    _, message, _ = harness.notify_calls[0]
    assert message.strip() in report


def test_a_second_call_finds_the_marker_and_emits_nothing(harness):
    manifest = _manifest({'s1': 'done'})

    first = harness.call(manifest)
    second = harness.call(manifest)

    assert first is True
    assert second is False
    assert len(harness.notify_calls) == 1


# ---------- a failing notice leaves the marker unset ----------


def test_a_failing_notice_leaves_the_marker_unset_so_a_later_tick_retries(
    harness, monkeypatch
):
    manifest = _manifest({'s1': 'done'})

    def boom(plan_name, message, **kwargs):
        raise OSError('sink down')

    monkeypatch.setattr(plan_completion, '_notify_user', boom)
    first = harness.call(manifest)

    assert first is False
    assert harness.is_completed() is False, (
        'the marker must not be set when the notice failed'
    )

    monkeypatch.setattr(
        plan_completion,
        '_notify_user',
        lambda plan_name, message, **kwargs: harness.notify_calls.append(
            (plan_name, message, kwargs)
        ),
    )
    second = harness.call(manifest)

    assert second is True
    assert len(harness.notify_calls) == 1
    assert harness.is_completed() is True


# ---------- the report write is best effort ----------


def test_a_failing_report_write_still_notifies_and_sets_the_marker(
    harness, monkeypatch, caplog
):
    manifest = _manifest({'s1': 'done'})

    def boom(namespace, plan_name, text):
        raise OSError('report write failed')

    monkeypatch.setattr(
        type(harness.report.__self__ if False else object),  # placeholder
        'x',
        None,
        raising=False,
    )

    # Patch the record view the implementation writes through: a report write
    # that raises must not block the notice.
    store = server._store
    original = store.put_plan_report

    def failing_put(namespace, plan_name, text):
        raise OSError('report write failed')

    monkeypatch.setattr(store, 'put_plan_report', failing_put)

    with caplog.at_level(logging.WARNING):
        result = harness.call(manifest)

    assert result is True
    assert len(harness.notify_calls) == 1
    assert harness.is_completed() is True
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, 'a failed report write must be logged at WARNING'


# ---------- negative / boundary ----------


def test_an_empty_stories_dict_is_not_a_completed_plan(harness):
    result = harness.call(_manifest())

    assert result is False
    assert harness.notify_calls == []
    assert harness.is_completed() is False


def test_a_manifest_without_a_stories_key_is_not_a_completed_plan(harness):
    result = harness.call(_manifest(include_stories=False))

    assert result is False
    assert harness.notify_calls == []
    assert harness.is_completed() is False


def test_an_incomplete_plan_is_not_a_completed_plan(harness):
    result = harness.call(_manifest({'s1': 'done', 's2': 'in_progress'}))

    assert result is False
    assert harness.notify_calls == []
    assert harness.is_completed() is False
    assert harness.report() is None
