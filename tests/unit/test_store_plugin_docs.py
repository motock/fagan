"""The store plugin is documented in REFERENCE.md and docs/guides/STORE_PLUGINS.md.

Only membership is asserted: wording and row order are free to change, but the
trust-boundary sentence and the CI conftest snippet must stay in the docs.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "REFERENCE.md"
GUIDE = REPO_ROOT / "docs" / "guides" / "STORE_PLUGINS.md"

TRUST_SENTENCE = "whoever can set the variable runs code in the orchestrator"

CONFTEST_SNIPPET = """\
import pytest

from pipeline import persistence
from pipeline.store_memory import InMemoryRecordStore


@pytest.fixture
def record_store_factory(tmp_path, monkeypatch):
    # The store reads plan drafts from the plans directory; keep it empty.
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return InMemoryRecordStore()


@pytest.fixture
def record_store_serves_other():
    return frozenset(
        {"manifest", "decisions", "journal", "usage", "notification", "context_snapshot"}
    )


@pytest.fixture
def record_store_known_gaps():
    return {
        "malformed_get": "get_context_snapshot returns None for a malformed digest",
    }
"""

TEST_SNIPPET = "from pipeline.testing.store_conformance import *  # noqa: F401,F403"


def _row(text: str, first_cell: str) -> str:
    rows = [line for line in text.splitlines() if line.startswith(f"| `{first_cell}`")]
    assert rows, f"REFERENCE.md has no {first_cell} row"
    return rows[0]


def test_reference_has_a_row_for_the_plugin_selection():
    text = REFERENCE.read_text(encoding="utf-8")

    row = _row(text, "PIPELINE_STORE=plugin")
    assert "PIPELINE_STORE_PLUGIN" in row
    assert TRUST_SENTENCE in row


def test_reference_has_a_row_for_the_plugin_spec():
    text = REFERENCE.read_text(encoding="utf-8")

    row = _row(text, "PIPELINE_STORE_PLUGIN")
    assert "module:attr" in row
    assert TRUST_SENTENCE in row


def test_reference_links_the_store_plugin_guide():
    assert "docs/guides/STORE_PLUGINS.md" in REFERENCE.read_text(encoding="utf-8")


def test_the_guide_carries_the_exact_conftest_snippet():
    guide = GUIDE.read_text(encoding="utf-8")

    assert CONFTEST_SNIPPET in guide


def test_the_guide_carries_the_test_module_that_collects_the_suite():
    guide = GUIDE.read_text(encoding="utf-8")

    assert TEST_SNIPPET in guide
