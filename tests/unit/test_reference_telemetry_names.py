"""REFERENCE.md documents the telemetry names under the ``fagan`` namespace.

The operator-facing reference is what dashboards and alerts are built from, so
it must list the metric names this code exports and must not keep advertising
the retired ``pipeline.*`` / ``fagan-pipeline`` ones.

Only membership and absence are asserted: the surrounding prose, row order and
the rest of the document are free to change.
"""

from __future__ import annotations

from pathlib import Path

REFERENCE = Path(__file__).resolve().parents[2] / "REFERENCE.md"

#: Metric names exported by ``pipeline.telemetry``.
FAGAN_METRIC_NAMES = (
    "fagan.dispatch.outcomes",
    "fagan.authz.decisions",
    "fagan.merge.results",
    "fagan.stories",
    "fagan.capacity.in_use",
)

#: Names the rename retired; the reference must not advertise them.
RETIRED_NAMES = (
    "pipeline.dispatch.outcomes",
    "fagan-pipeline",
)


def test_reference_lists_every_fagan_metric_name():
    text = REFERENCE.read_text(encoding="utf-8")

    for name in FAGAN_METRIC_NAMES:
        assert name in text


def test_reference_does_not_advertise_retired_names():
    text = REFERENCE.read_text(encoding="utf-8")

    for name in RETIRED_NAMES:
        assert name not in text
