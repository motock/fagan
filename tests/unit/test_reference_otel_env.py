"""TDD test for the REFERENCE.md telemetry-environment documentation.

``pipeline/telemetry`` honours the standard OpenTelemetry exporter variables
alongside the deprecated ``PIPELINE_OTEL_ENDPOINT`` alias, so the operator
reference must name them: an operator who only reads REFERENCE.md has to learn
that ``OTEL_EXPORTER_OTLP_HEADERS`` can authenticate the collector, and that the
alias still works. Membership only -- never a full-content, count or hash
assertion, so an unrelated documentation edit cannot fail this test.

``pipeline.server`` is imported before any other ``pipeline.*`` module: the
package has a pre-existing import cycle this suite inherits (same idiom as
``tests/unit/test_triage_cap_silent_skip.py``).
"""

from __future__ import annotations

from pathlib import Path

import pipeline.server  # noqa: F401  -- import-cycle anchor; must come first

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_reference_documents_standard_otel_headers_variable():
    text = (REPO_ROOT / "REFERENCE.md").read_text(encoding="utf-8")

    assert "OTEL_EXPORTER_OTLP_HEADERS" in text


def test_reference_keeps_the_alias_documented_as_deprecated():
    text = (REPO_ROOT / "REFERENCE.md").read_text(encoding="utf-8")

    assert "deprecated alias" in text
