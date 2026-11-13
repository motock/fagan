"""Tests for the pure Prometheus text exposition in ``pipeline.metrics_exposition``.

``render`` turns a snapshot into exposition format 0.0.4: a fixed metric order,
``# HELP`` + ``# TYPE`` per metric, escaped label values, a trailing newline,
and never a ``NaN`` for a sample whose value is not known.

``collect`` gathers the snapshot from the three existing producers
(``health_extras``, ``outbox_summary``, ``consumer_status``) plus the plan
manifests. It must never raise: a producer that fails omits its metric and
increments ``fagan_metrics_errors_total{source}``.

Secure by Design is graded here too: no plan name, story key, file path or
manifest text may reach a label or a value.
"""
from __future__ import annotations

import json

import pytest

from pipeline import metrics_exposition as m
from tests.unit._injected_state import host_at, store_at

#: A fixed snapshot: every metric with a value, deliberately in a scrambled key
#: order so the expected output proves the exposition order is fixed by the
#: module rather than by dict insertion order.
SNAPSHOT = {
    "fagan_plans": [({"status": "approved"}, 2), ({"status": "none"}, 1)],
    "fagan_consumer_pending": [({"durable": "pipeline-story"}, 3)],
    "fagan_scheduler_age_seconds": [({}, 12)],
    "fagan_metrics_errors_total": [({"source": "consumer_status"}, 0)],
    "fagan_scheduler_healthy": [({}, True)],
    "fagan_outbox_records": [({}, 4)],
    "fagan_broker_reachable": [({}, 1)],
    "fagan_consumer_subscribed": [({"durable": "pipeline-story"}, True)],
    "fagan_outbox_oldest_age_seconds": [({}, 90)],
    "fagan_consumer_dead_lettered": [({"durable": "pipeline-story"}, 0)],
}

EXPECTED = (
    "# HELP fagan_scheduler_age_seconds Age in seconds of the scheduler's last"
    " health snapshot.\n"
    "# TYPE fagan_scheduler_age_seconds gauge\n"
    "fagan_scheduler_age_seconds 12\n"
    "# HELP fagan_scheduler_healthy Whether the scheduler health probe reports"
    " healthy (1) or not (0).\n"
    "# TYPE fagan_scheduler_healthy gauge\n"
    "fagan_scheduler_healthy 1\n"
    "# HELP fagan_outbox_records Number of records waiting in the notification"
    " outbox.\n"
    "# TYPE fagan_outbox_records gauge\n"
    "fagan_outbox_records 4\n"
    "# HELP fagan_outbox_oldest_age_seconds Age in seconds of the oldest"
    " notification outbox record.\n"
    "# TYPE fagan_outbox_oldest_age_seconds gauge\n"
    "fagan_outbox_oldest_age_seconds 90\n"
    "# HELP fagan_broker_reachable Whether the NATS broker answered the consumer"
    " status probe (1) or not (0).\n"
    "# TYPE fagan_broker_reachable gauge\n"
    "fagan_broker_reachable 1\n"
    "# HELP fagan_consumer_pending Messages pending for the durable consumer.\n"
    "# TYPE fagan_consumer_pending gauge\n"
    'fagan_consumer_pending{durable="pipeline-story"} 3\n'
    "# HELP fagan_consumer_subscribed Whether the durable consumer is subscribed"
    " (1) or not (0).\n"
    "# TYPE fagan_consumer_subscribed gauge\n"
    'fagan_consumer_subscribed{durable="pipeline-story"} 1\n'
    "# HELP fagan_consumer_dead_lettered Messages on the durable consumer's"
    " dead-letter subject.\n"
    "# TYPE fagan_consumer_dead_lettered gauge\n"
    'fagan_consumer_dead_lettered{durable="pipeline-story"} 0\n'
    "# HELP fagan_plans Number of plan manifests by approval status.\n"
    "# TYPE fagan_plans gauge\n"
    'fagan_plans{status="approved"} 2\n'
    'fagan_plans{status="none"} 1\n'
    "# HELP fagan_metrics_errors_total Producer failures while collecting the"
    " metrics snapshot.\n"
    "# TYPE fagan_metrics_errors_total counter\n"
    'fagan_metrics_errors_total{source="consumer_status"} 0\n'
)


def test_render_matches_the_exact_exposition_text() -> None:
    assert m.render(SNAPSHOT) == EXPECTED


def test_render_ends_with_a_single_trailing_newline() -> None:
    assert m.render(SNAPSHOT).endswith("\n")
    assert not m.render(SNAPSHOT).endswith("\n\n")


def test_render_omits_a_none_sample_and_never_writes_nan() -> None:
    rendered = m.render(
        {
            "fagan_outbox_oldest_age_seconds": [({}, None)],
            "fagan_outbox_records": [({}, 7)],
        }
    )
    assert rendered == (
        "# HELP fagan_outbox_records Number of records waiting in the"
        " notification outbox.\n"
        "# TYPE fagan_outbox_records gauge\n"
        "fagan_outbox_records 7\n"
    )
    assert "NaN" not in rendered


def test_render_omits_a_metric_whose_only_sample_is_none() -> None:
    assert m.render({"fagan_scheduler_age_seconds": [({}, None)]}) == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('a"b', 'fagan_consumer_pending{durable="a\\"b"} 1\n'),
        ("a\\b", 'fagan_consumer_pending{durable="a\\\\b"} 1\n'),
        ("a\nb", 'fagan_consumer_pending{durable="a\\nb"} 1\n'),
    ],
)
def test_render_escapes_label_values(raw: str, expected: str) -> None:
    rendered = m.render({"fagan_consumer_pending": [({"durable": raw}, 1)]})
    assert rendered.endswith(expected)


@pytest.mark.parametrize("spec", m.METRICS)
def test_every_metric_name_starts_with_the_fagan_prefix(spec) -> None:
    assert spec[0].startswith("fagan_")


@pytest.mark.parametrize("spec", m.METRICS)
def test_every_metric_has_a_help_and_a_type_declaration(spec) -> None:
    _name, metric_type, help_text = spec
    assert metric_type in {"gauge", "counter"}
    assert help_text and "\n" not in help_text


# --- collect: the three producers, monkeypatched at this module's attributes ---


def _healthy_scheduler(*_a, **_k):
    return {"scheduler_age_s": 12, "healthy": True, "degraded": []}


def _quiet_outbox(*_a, **_k):
    return {"files": 1, "records": 4, "oldest_age_s": 90}


def _reachable_consumers():
    return {
        "pipeline-story": {
            "pending": 3,
            "redelivered": 0,
            "dead_lettered": 0,
            "subscribed": True,
            "oldest_pending_age_s": 42,
        }
    }


def test_collect_gathers_every_producer(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(m, "health_extras", _healthy_scheduler)
    monkeypatch.setattr(m, "outbox_summary", _quiet_outbox)
    monkeypatch.setattr(m, "consumer_status", _reachable_consumers)
    snapshot = m.collect(host_at(tmp_path), store_at(tmp_path))
    assert snapshot["fagan_scheduler_age_seconds"] == [({}, 12)]
    assert snapshot["fagan_scheduler_healthy"] == [({}, True)]
    assert snapshot["fagan_outbox_records"] == [({}, 4)]
    assert snapshot["fagan_outbox_oldest_age_seconds"] == [({}, 90)]
    assert snapshot["fagan_broker_reachable"] == [({}, 1)]
    assert snapshot["fagan_consumer_pending"] == [({"durable": "pipeline-story"}, 3)]
    assert snapshot["fagan_consumer_subscribed"] == [({"durable": "pipeline-story"}, True)]
    assert snapshot["fagan_consumer_dead_lettered"] == [({"durable": "pipeline-story"}, 0)]
    assert snapshot["fagan_consumer_oldest_pending_age_seconds"] == [
        ({"durable": "pipeline-story"}, 42)
    ]


def test_collect_omits_the_scheduler_age_when_the_scheduler_is_unreported(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        m,
        "health_extras",
        lambda *_a, **_k: {"healthy": False, "degraded": ["scheduler_unreported"]},
    )
    monkeypatch.setattr(m, "outbox_summary", _quiet_outbox)
    monkeypatch.setattr(m, "consumer_status", _reachable_consumers)
    snapshot = m.collect(host_at(tmp_path), store_at(tmp_path))
    assert "fagan_scheduler_age_seconds" not in snapshot
    assert snapshot["fagan_scheduler_healthy"] == [({}, False)]


def test_collect_never_raises_when_a_producer_fails(monkeypatch, tmp_path) -> None:
    def _boom():
        raise RuntimeError("broker refused: password=hunter2")

    monkeypatch.setattr(m, "health_extras", _healthy_scheduler)
    monkeypatch.setattr(m, "outbox_summary", _quiet_outbox)
    monkeypatch.setattr(m, "consumer_status", _boom)
    snapshot = m.collect(host_at(tmp_path), store_at(tmp_path))
    assert "fagan_consumer_pending" not in snapshot
    assert "fagan_consumer_subscribed" not in snapshot
    assert "fagan_consumer_dead_lettered" not in snapshot
    assert "fagan_consumer_oldest_pending_age_seconds" not in snapshot
    assert "fagan_broker_reachable" not in snapshot
    # The other producers still contribute.
    assert snapshot["fagan_outbox_records"] == [({}, 4)]
    assert snapshot["fagan_scheduler_healthy"] == [({}, True)]
    assert ({"source": "consumer_status"}, 1) in snapshot["fagan_metrics_errors_total"]


def test_collect_error_counter_never_echoes_the_exception_text(monkeypatch, tmp_path) -> None:
    def _boom():
        raise RuntimeError("broker refused: password=hunter2")

    monkeypatch.setattr(m, "health_extras", _healthy_scheduler)
    monkeypatch.setattr(m, "outbox_summary", _quiet_outbox)
    monkeypatch.setattr(m, "consumer_status", _boom)
    assert "hunter2" not in m.render(m.collect(host_at(tmp_path), store_at(tmp_path)))


def test_collect_renders_an_unreachable_broker_with_no_consumer_samples(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(m, "health_extras", _healthy_scheduler)
    monkeypatch.setattr(m, "outbox_summary", _quiet_outbox)
    monkeypatch.setattr(m, "consumer_status", lambda: {"reachable": False})
    rendered = m.render(m.collect(host_at(tmp_path), store_at(tmp_path)))
    assert "fagan_broker_reachable 0\n" in rendered
    assert "fagan_consumer_pending" not in rendered
    assert "fagan_consumer_subscribed" not in rendered
    assert "fagan_consumer_dead_lettered" not in rendered
    assert "fagan_consumer_oldest_pending_age_seconds" not in rendered


# --- plans: counted from the manifest listing, never named ---------------------


def _write_manifest(directory, plan_name: str, payload) -> None:
    path = directory / f"{plan_name}.manifest.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def test_collect_counts_plan_manifests_by_approval_status(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(m, "health_extras", _healthy_scheduler)
    monkeypatch.setattr(m, "outbox_summary", _quiet_outbox)
    monkeypatch.setattr(m, "consumer_status", _reachable_consumers)
    _write_manifest(tmp_path, "alpha", {"approval": {"status": "approved"}})
    _write_manifest(tmp_path, "beta", {"approval": {"status": "pending_approval"}})
    _write_manifest(tmp_path, "gamma", {})
    _write_manifest(tmp_path, "delta", {"approval": {"status": "rejected"}})
    snapshot = m.collect(host_at(tmp_path), store_at(tmp_path))
    assert snapshot["fagan_plans"] == [
        ({"status": "approved"}, 1),
        ({"status": "pending_approval"}, 1),
        ({"status": "rejected"}, 1),
        ({"status": "none"}, 1),
    ]


def test_collect_skips_an_unreadable_manifest(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(m, "health_extras", _healthy_scheduler)
    monkeypatch.setattr(m, "outbox_summary", _quiet_outbox)
    monkeypatch.setattr(m, "consumer_status", _reachable_consumers)
    _write_manifest(tmp_path, "alpha", {"approval": {"status": "approved"}})
    _write_manifest(tmp_path, "broken", "{not json")
    snapshot = m.collect(host_at(tmp_path), store_at(tmp_path))
    assert snapshot["fagan_plans"] == [
        ({"status": "approved"}, 1),
        ({"status": "pending_approval"}, 0),
        ({"status": "rejected"}, 0),
        ({"status": "none"}, 0),
    ]


def test_collect_counts_an_unknown_status_as_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(m, "health_extras", _healthy_scheduler)
    monkeypatch.setattr(m, "outbox_summary", _quiet_outbox)
    monkeypatch.setattr(m, "consumer_status", _reachable_consumers)
    _write_manifest(tmp_path, "alpha", {"approval": {"status": "sk-secret-status"}})
    snapshot = m.collect(host_at(tmp_path), store_at(tmp_path))
    assert snapshot["fagan_plans"] == [
        ({"status": "approved"}, 0),
        ({"status": "pending_approval"}, 0),
        ({"status": "rejected"}, 0),
        ({"status": "none"}, 1),
    ]


def test_no_sentinel_manifest_text_reaches_the_exposition(monkeypatch, tmp_path) -> None:
    sentinel_plan = "sk-SENTINEL-PLAN-NAME"
    sentinel_email = "sentinel-operator@example.invalid"
    monkeypatch.setattr(m, "health_extras", _healthy_scheduler)
    monkeypatch.setattr(m, "outbox_summary", _quiet_outbox)
    monkeypatch.setattr(m, "consumer_status", _reachable_consumers)
    _write_manifest(
        tmp_path,
        sentinel_plan,
        {"approval": {"status": "approved", "decided_by": sentinel_email}},
    )
    rendered = m.render(m.collect(host_at(tmp_path), store_at(tmp_path)))
    assert sentinel_plan not in rendered
    assert sentinel_email not in rendered


def test_no_plan_path_reaches_the_exposition(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(m, "health_extras", _healthy_scheduler)
    monkeypatch.setattr(m, "outbox_summary", _quiet_outbox)
    monkeypatch.setattr(m, "consumer_status", _reachable_consumers)
    _write_manifest(tmp_path, "alpha", {"approval": {"status": "approved"}})
    rendered = m.render(m.collect(host_at(tmp_path), store_at(tmp_path)))
    assert str(tmp_path) not in rendered
