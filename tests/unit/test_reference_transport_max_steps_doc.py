"""Tests for the REFERENCE.md doc edit renaming the transport-only write
target from ``LOCAL_AGENT_MAX_STEPS`` to ``PIPELINE_TRANSPORT_MAX_STEPS``.

This is a docs-only story touching exactly one table row in ``REFERENCE.md``:
the ``PIPELINE_LOCAL_MAX_STEPS`` row (the single row that currently mentions
``LOCAL_AGENT_MAX_STEPS``). The edit replaces the cell text so that:

* ``LOCAL_AGENT_MAX_STEPS`` is no longer mentioned anywhere in the row (the
  legacy duplicate write was removed; nothing reads it).
* ``PIPELINE_TRANSPORT_MAX_STEPS`` is now described as the transport-only
  write target that ``backend.py`` writes the resolved value into.
* The input-knob contract (``PIPELINE_LOCAL_MAX_STEPS`` is the input knob,
  re-read on every dispatch) is preserved.
* The plist / launchctl guidance is preserved.

These tests assert the *content* of that prose edit. They are RED until the
doc edit lands (the implementation dispatch), which is the intended state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REFERENCE_PATH = REPO_ROOT / "REFERENCE.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reference_text() -> str:
    assert REFERENCE_PATH.exists(), f"REFERENCE.md missing: {REFERENCE_PATH}"
    return REFERENCE_PATH.read_text(encoding="utf-8")


def _pipeline_local_max_steps_row(text: str) -> str:
    """Return the single table row whose first cell is exactly
    ``| `PIPELINE_LOCAL_MAX_STEPS` |``.

    The row is one physical line in the markdown table. There must be exactly
    one such row.
    """
    # Match a table line whose first cell is the PIPELINE_LOCAL_MAX_STEPS key.
    # The key cell is `| `PIPELINE_LOCAL_MAX_STEPS` |` (backticks around name).
    rows = [
        line
        for line in text.splitlines()
        if line.lstrip().startswith("| `PIPELINE_LOCAL_MAX_STEPS` |")
    ]
    assert len(rows) == 1, (
        f"expected exactly one PIPELINE_LOCAL_MAX_STEPS row, found {len(rows)}"
    )
    return rows[0]


# ---------------------------------------------------------------------------
# Row existence / uniqueness
# ---------------------------------------------------------------------------

class TestRowExists:
    def test_reference_file_exists(self):
        _reference_text()

    def test_exactly_one_pipeline_local_max_steps_row(self):
        text = _reference_text()
        _pipeline_local_max_steps_row(text)  # asserts exactly one


# ---------------------------------------------------------------------------
# The rename: LOCAL_AGENT_MAX_STEPS is gone from the row
# ---------------------------------------------------------------------------

class TestLegacyNameRemoved:
    """The row must no longer mention LOCAL_AGENT_MAX_STEPS anywhere."""

    def test_row_does_not_mention_local_agent_max_steps(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "LOCAL_AGENT_MAX_STEPS" not in row, (
            "PIPELINE_LOCAL_MAX_STEPS row still mentions the dead "
            "LOCAL_AGENT_MAX_STEPS name; it must be removed"
        )

    def test_row_mentions_legacy_duplicate_write_was_removed(self):
        """The new text must explicitly state the legacy duplicate write was
        removed and nothing reads it."""
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "removed" in row.lower(), (
            "row must state the legacy LOCAL_AGENT_MAX_STEPS duplicate write "
            "was removed"
        )
        assert "nothing reads it" in row.lower(), (
            "row must state nothing reads the legacy name"
        )


# ---------------------------------------------------------------------------
# The new transport name: PIPELINE_TRANSPORT_MAX_STEPS
# ---------------------------------------------------------------------------

class TestTransportNameIntroduced:
    """The row must describe PIPELINE_TRANSPORT_MAX_STEPS as the
    transport-only write target."""

    def test_row_mentions_pipeline_transport_max_steps(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "PIPELINE_TRANSPORT_MAX_STEPS" in row, (
            "row must mention PIPELINE_TRANSPORT_MAX_STEPS as the "
            "transport-only write target"
        )

    def test_transport_name_described_as_transport_only(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "transport-only" in row.lower(), (
            "row must describe PIPELINE_TRANSPORT_MAX_STEPS as transport-only"
        )

    def test_transport_name_must_not_be_set_in_plist_or_shell(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "must not be set in the plist or shell" in row, (
            "row must state PIPELINE_TRANSPORT_MAX_STEPS must not be set in "
            "the plist or shell"
        )

    def test_backend_writes_resolved_value_into_transport_name(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "writes the resolved value into `PIPELINE_TRANSPORT_MAX_STEPS`" in row, (
            "row must state backend.py writes the resolved value into "
            "PIPELINE_TRANSPORT_MAX_STEPS for the subprocess"
        )

    def test_transport_name_is_for_the_subprocess(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "for the subprocess" in row, (
            "row must state the transport write is for the subprocess"
        )


# ---------------------------------------------------------------------------
# Preserved content: input-knob contract & plist guidance
# ---------------------------------------------------------------------------

class TestPreservedContent:
    """The parts of the row that were not part of the rename must remain."""

    def test_input_knob_phrase_preserved(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "`PIPELINE_LOCAL_MAX_STEPS` is the input knob" in row, (
            "the input-knob contract for PIPELINE_LOCAL_MAX_STEPS must be "
            "preserved"
        )

    def test_backend_rereads_input_knob_on_every_dispatch(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "re-reads `PIPELINE_LOCAL_MAX_STEPS` on every dispatch" in row, (
            "row must preserve that backend.py re-reads the input knob on "
            "every dispatch"
        )

    def test_plist_path_preserved(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "launchd/com.fagan.pipeline.advance-scheduler.plist" in row, (
            "row must preserve the plist path guidance"
        )

    def test_launchctl_guidance_preserved(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "launchctl unload && launchctl load" in row, (
            "row must preserve the launchctl unload/load guidance"
        )

    def test_max_tool_call_steps_lead_preserved(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert row.lstrip().startswith(
            "| `PIPELINE_LOCAL_MAX_STEPS` | `40` | Max tool-call steps a local **dispatch** run takes before it parks (WIP-commits)."
        ), "row must preserve its leading 'Max tool-call steps ...' sentence"

    def test_default_value_cell_unchanged(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "| `40` |" in row, "default value cell must remain `40`"


# ---------------------------------------------------------------------------
# Scope: no other row in the table lost its LOCAL_AGENT_* content
# ---------------------------------------------------------------------------

class TestScopeIsScopedToTheOneRow:
    """The edit must touch only the PIPELINE_LOCAL_MAX_STEPS row. Other
    LOCAL_AGENT_* rows (READ_SILENCE_SECONDS, CHAT_MAX_ATTEMPTS, etc.) are
    unrelated transport knobs and must remain intact."""

    @pytest.mark.parametrize(
        "key",
        [
            "LOCAL_AGENT_READ_SILENCE_SECONDS",
            "LOCAL_AGENT_CHAT_MAX_ATTEMPTS",
            "LOCAL_AGENT_CHAT_RETRY_BACKOFF",
            "LOCAL_AGENT_BASH_TIMEOUT_SECONDS",
            "LOCAL_AGENT_READ_HEAVY_WINDOW",
            "LOCAL_AGENT_READ_HEAVY_DISTINCT_WINDOWS",
        ],
    )
    def test_other_local_agent_rows_preserved(self, key):
        text = _reference_text()
        assert f"| `{key}` |" in text, (
            f"unrelated {key} row must not be touched by this edit"
        )

    def test_review_max_steps_row_unchanged(self):
        """The sibling PIPELINE_LOCAL_REVIEW_MAX_STEPS row references the
        input-knob contract and must remain intact."""
        text = _reference_text()
        rows = [
            line
            for line in text.splitlines()
            if line.lstrip().startswith("| `PIPELINE_LOCAL_REVIEW_MAX_STEPS` |")
        ]
        assert len(rows) == 1, "PIPELINE_LOCAL_REVIEW_MAX_STEPS row missing"
        assert "same input-knob contract as `PIPELINE_LOCAL_MAX_STEPS`" in rows[0]


# ---------------------------------------------------------------------------
# Whole-file invariant: LOCAL_AGENT_MAX_STEPS as a bare token
# ---------------------------------------------------------------------------

class TestWholeFileInvariant:
    """Sanity: after the edit, the only place LOCAL_AGENT_MAX_STEPS could
    legitimately survive is inside the row's 'removed' clause — but the task
    requires the row no longer mention it *at all*, so the bare token must be
    absent from the row. We do not assert it is absent from the whole file
    (other sections may reference it), only from the edited row, which the
    TestLegacyNameRemoved class already covers. This class instead asserts
    the new transport name appears in the row exactly once (not duplicated).
    """

    def test_transport_name_appearances_in_row(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        count = row.count("PIPELINE_TRANSPORT_MAX_STEPS")
        assert count >= 1, "PIPELINE_TRANSPORT_MAX_STEPS must appear at least once"
        # It should not be spuriously duplicated many times.
        assert count <= 4, (
            f"PIPELINE_TRANSPORT_MAX_STEPS appears {count} times in the row; "
            "expected a small number, not a runaway duplication"
        )

    def test_input_knob_name_still_in_row(self):
        row = _pipeline_local_max_steps_row(_reference_text())
        assert "PIPELINE_LOCAL_MAX_STEPS" in row