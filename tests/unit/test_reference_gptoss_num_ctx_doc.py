"""Tests for the REFERENCE.md doc edit documenting the gpt-oss num_ctx change.

Docs-only story touching three rows of ``REFERENCE.md``:

1. The ``gpt-oss:20b`` row in the "Per-model tuning table" currently claims
   ``num_ctx=32768``, but the live code (``app/ollama_prompt_utils.py``) and
   its test (``test_gptoss_20b_tuned_to_low_temperature_from_ab_experiment``)
   both confirm that entry is ``{"temperature": 0.3}`` ONLY. The stale
   ``32768`` must be cleared from the ``num_ctx`` cell and the Why cell must
   note the value was never itself A/B-tested (mirroring the code comment).
2. A ``gpt-oss-20b-high:latest`` row with ``num_ctx=131072`` must exist
   EXACTLY ONCE, correctly aligned in the 4-column table
   (``| Model tag | temperature | num_ctx | Why |``), immediately after the
   ``gpt-oss:20b`` row, with provenance in the Why cell.
3. The ``PIPELINE_LOCAL_NUM_CTX`` row must note it is no longer pinned by the
   advance-scheduler launchd plist, so locally-dispatched models fall through
   to the per-model tuning table.

These tests assert the *content* of that prose edit. They are RED until the
doc edit lands (the implementation dispatch), which is the intended state.

Every row is located by its unique leading text / first cell, never by line
number, and every value is asserted by CELL INDEX so that a malformed row
(e.g. ``131072`` sitting in the temperature column) fails loudly instead of
passing on a naive substring match.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REFERENCE_PATH = REPO_ROOT / "REFERENCE.md"

# The tuning table's header is ``| Model tag | `temperature` | `num_ctx` | Why |``.
_TUNING_TABLE_HEADER_FIRST_CELL = "Model tag"

# A cell that documents "no value" in this table.
_EMPTY_CELLS = ("", "--", "—", "-")

_HIGH_TAG = "gpt-oss-20b-high:latest"
_BASE_TAG = "gpt-oss:20b"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reference_text() -> str:
    assert REFERENCE_PATH.exists(), f"REFERENCE.md missing: {REFERENCE_PATH}"
    return REFERENCE_PATH.read_text(encoding="utf-8")


def _cells(row: str) -> list[str]:
    """Split one markdown table row into its cells.

    Leading/trailing pipes are dropped and each cell is stripped, the same way
    ``tests/unit/test_reference_transport_max_steps_doc.py`` handles its row.
    """
    stripped = row.strip().removeprefix("|").removesuffix("|")
    return [cell.strip() for cell in stripped.split("|")]


def _normalise_tag(cell: str) -> str:
    """A first-cell tag, with optional backticks removed."""
    return cell.strip().strip("`").strip()


def _rows_with_tag(text: str, tag: str) -> list[str]:
    """Every table row whose first cell is ``tag`` (backticks optional)."""
    matches: list[str] = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _cells(line)
        if cells and _normalise_tag(cells[0]) == tag:
            matches.append(line)
    return matches


def _single_row_with_tag(text: str, tag: str) -> str:
    """The single table row whose first cell is ``tag``; asserts uniqueness."""
    matches = _rows_with_tag(text, tag)
    assert len(matches) == 1, (
        f"expected exactly one {tag!r} table row, found {len(matches)}: "
        f"{matches!r}"
    )
    return matches[0]


def _high_row_is_well_formed(cells: list[str]) -> bool:
    """True only for a correctly aligned 4-cell ``gpt-oss-20b-high:latest`` row.

    Cell order is the table header's: ``| Model tag | temperature | num_ctx |
    Why |``. A 3-cell row that dumps ``131072`` into the temperature column
    must NOT satisfy this.
    """
    return (
        len(cells) == 4
        and _normalise_tag(cells[0]) == _HIGH_TAG
        and cells[1] in _EMPTY_CELLS
        and cells[2] == "131072"
    )


def _tuning_table_header(text: str) -> str:
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _cells(line)
        if cells and cells[0] == _TUNING_TABLE_HEADER_FIRST_CELL:
            return line
    raise AssertionError(
        "could not find the per-model tuning table header "
        f"(first cell {_TUNING_TABLE_HEADER_FIRST_CELL!r}) in REFERENCE.md"
    )


# ---------------------------------------------------------------------------
# File / table shape
# ---------------------------------------------------------------------------

class TestReferenceFileAndTableShape:
    def test_reference_file_exists(self):
        _reference_text()

    def test_tuning_table_has_the_expected_four_columns(self):
        """Anchors the cell-index semantics every other assertion relies on."""
        cells = _cells(_tuning_table_header(_reference_text()))
        assert len(cells) == 4, (
            f"per-model tuning table header must have 4 columns, got {cells!r}"
        )
        assert cells[0] == "Model tag"
        assert _normalise_tag(cells[1]) == "temperature"
        assert _normalise_tag(cells[2]) == "num_ctx"
        assert cells[3] == "Why"


# ---------------------------------------------------------------------------
# Step 1: the stale gpt-oss:20b num_ctx=32768 claim is corrected
# ---------------------------------------------------------------------------

class TestGptOss20bRow:
    def test_exactly_one_gpt_oss_20b_row(self):
        _single_row_with_tag(_reference_text(), _BASE_TAG)

    def test_row_has_four_cells(self):
        cells = _cells(_single_row_with_tag(_reference_text(), _BASE_TAG))
        assert len(cells) == 4, (
            f"gpt-oss:20b row must keep its 4 columns, got {cells!r}"
        )

    def test_num_ctx_cell_is_empty(self):
        cells = _cells(_single_row_with_tag(_reference_text(), _BASE_TAG))
        assert cells[2] in _EMPTY_CELLS, (
            "gpt-oss:20b num_ctx cell (cell index 2) must be cleared: the "
            f"32768 value was never A/B-tested, got {cells[2]!r}"
        )

    def test_row_no_longer_contains_32768(self):
        row = _single_row_with_tag(_reference_text(), _BASE_TAG)
        assert "32768" not in row, (
            "gpt-oss:20b row still claims num_ctx=32768; the live code has "
            '{"temperature": 0.3} only'
        )

    def test_temperature_cell_preserved(self):
        cells = _cells(_single_row_with_tag(_reference_text(), _BASE_TAG))
        assert "0.3" in cells[1], (
            f"gpt-oss:20b temperature cell must stay 0.3, got {cells[1]!r}"
        )

    def test_why_cell_notes_num_ctx_was_never_ab_tested(self):
        cells = _cells(_single_row_with_tag(_reference_text(), _BASE_TAG))
        why = cells[3]
        assert "num_ctx" in why.lower(), (
            "gpt-oss:20b Why cell must mention num_ctx"
        )
        assert "never" in why.lower(), (
            "gpt-oss:20b Why cell must state the num_ctx value was never "
            "itself A/B-tested"
        )
        assert "a/b" in why.lower(), (
            "gpt-oss:20b Why cell must reference the A/B experiment"
        )

    def test_why_cell_notes_num_ctx_was_removed(self):
        cells = _cells(_single_row_with_tag(_reference_text(), _BASE_TAG))
        assert "remov" in cells[3].lower(), (
            "gpt-oss:20b Why cell must state the num_ctx value was removed "
            "from the table"
        )


# ---------------------------------------------------------------------------
# Step 2: the gpt-oss-20b-high:latest row (exactly one, correctly aligned)
# ---------------------------------------------------------------------------

class TestGptOss20bHighRow:
    def test_exactly_one_gpt_oss_20b_high_row(self):
        matches = _rows_with_tag(_reference_text(), _HIGH_TAG)
        assert len(matches) == 1, (
            f"expected exactly one {_HIGH_TAG!r} row, found {len(matches)}: "
            f"{matches!r}"
        )

    def test_row_is_well_formed_by_cell_index(self):
        row = _single_row_with_tag(_reference_text(), _HIGH_TAG)
        cells = _cells(row)
        assert _high_row_is_well_formed(cells), (
            "gpt-oss-20b-high:latest row must be a correctly aligned 4-cell "
            "row: | `gpt-oss-20b-high:latest` | <empty> | `131072` | <why> |; "
            f"got {cells!r}"
        )

    def test_num_ctx_cell_is_exactly_131072(self):
        cells = _cells(_single_row_with_tag(_reference_text(), _HIGH_TAG))
        assert cells[2] == "131072", (
            "gpt-oss-20b-high:latest num_ctx cell (cell index 2) must be "
            f"exactly '131072', got {cells[2]!r}"
        )

    def test_temperature_cell_is_empty(self):
        cells = _cells(_single_row_with_tag(_reference_text(), _HIGH_TAG))
        assert cells[1] in _EMPTY_CELLS, (
            "gpt-oss-20b-high:latest temperature cell (cell index 1) must be "
            f"empty/--, got {cells[1]!r}"
        )

    def test_row_immediately_follows_the_gpt_oss_20b_row(self):
        text = _reference_text()
        lines = text.splitlines()
        base = _single_row_with_tag(text, _BASE_TAG)
        high = _single_row_with_tag(text, _HIGH_TAG)
        assert lines.index(high) == lines.index(base) + 1, (
            "gpt-oss-20b-high:latest row must sit immediately after the "
            "gpt-oss:20b row"
        )

    def test_why_cell_cites_the_sweep_date(self):
        why = _cells(_single_row_with_tag(_reference_text(), _HIGH_TAG))[3]
        assert "2026-09-18" in why, (
            "gpt-oss-20b-high:latest Why cell must cite the 2026-09-18 sweep"
        )

    def test_why_cell_cites_the_host(self):
        why = _cells(_single_row_with_tag(_reference_text(), _HIGH_TAG))[3]
        assert "24GB" in why or "M4" in why, (
            "gpt-oss-20b-high:latest Why cell must cite the Apple M4 / 24GB "
            "host"
        )

    def test_why_cell_cites_the_sweep_range_and_step_count(self):
        why = _cells(_single_row_with_tag(_reference_text(), _HIGH_TAG))[3]
        assert "32768" in why and "131072" in why, (
            "gpt-oss-20b-high:latest Why cell must cite the 32768 -> 131072 "
            "sweep range"
        )
        assert re.search(r"\b(7|seven)[- ]step", why, re.IGNORECASE), (
            "gpt-oss-20b-high:latest Why cell must cite the 7-step sweep"
        )

    def test_why_cell_cites_full_gpu_throughout(self):
        why = _cells(_single_row_with_tag(_reference_text(), _HIGH_TAG))[3]
        assert "100%" in why and "GPU" in why, (
            "gpt-oss-20b-high:latest Why cell must state 100% GPU throughout"
        )

    def test_why_cell_cites_resident_growth(self):
        why = _cells(_single_row_with_tag(_reference_text(), _HIGH_TAG))[3]
        assert "12GB" in why and "13GB" in why, (
            "gpt-oss-20b-high:latest Why cell must cite the 12GB -> 13GB "
            "resident growth"
        )
        assert "resident" in why.lower(), (
            "gpt-oss-20b-high:latest Why cell must describe the resident size"
        )

    def test_why_cell_cites_no_swap_growth(self):
        why = _cells(_single_row_with_tag(_reference_text(), _HIGH_TAG))[3]
        assert re.search(
            r"no\s+(meaningful\s+|further\s+|significant\s+|measurable\s+)?swap",
            why,
            re.IGNORECASE,
        ), (
            "gpt-oss-20b-high:latest Why cell must state there was no swap "
            "growth"
        )

    def test_why_cell_cites_the_trained_ceiling(self):
        why = _cells(_single_row_with_tag(_reference_text(), _HIGH_TAG))[3]
        assert "ceiling" in why.lower(), (
            "gpt-oss-20b-high:latest Why cell must call 131072 a ceiling"
        )
        assert "trained" in why.lower() or "ollama" in why.lower(), (
            "gpt-oss-20b-high:latest Why cell must state 131072 is gpt-oss's "
            "own trained / Ollama-enforced ceiling"
        )

    def test_why_cell_cites_the_1048576_probe(self):
        why = _cells(_single_row_with_tag(_reference_text(), _HIGH_TAG))[3]
        assert "1048576" in why, (
            "gpt-oss-20b-high:latest Why cell must cite the values-up-to-"
            "1048576 probe"
        )

    def test_why_cell_cites_no_further_effect_above_the_ceiling(self):
        why = _cells(_single_row_with_tag(_reference_text(), _HIGH_TAG))[3]
        assert re.search(
            r"no\s+(further|additional|measurable|meaningful)\s+effect",
            why,
            re.IGNORECASE,
        ), (
            "gpt-oss-20b-high:latest Why cell must state values above 131072 "
            "had no further effect"
        )


# ---------------------------------------------------------------------------
# Step 3: the PIPELINE_LOCAL_NUM_CTX row
# ---------------------------------------------------------------------------

class TestPipelineLocalNumCtxRow:
    def test_exactly_one_pipeline_local_num_ctx_row(self):
        _single_row_with_tag(_reference_text(), "PIPELINE_LOCAL_NUM_CTX")

    def test_default_is_still_16384(self):
        row = _single_row_with_tag(_reference_text(), "PIPELINE_LOCAL_NUM_CTX")
        assert "16384" in row, (
            "PIPELINE_LOCAL_NUM_CTX row must keep its 16384 default"
        )

    def test_notes_it_is_no_longer_pinned_by_the_advance_scheduler_plist(self):
        row = _single_row_with_tag(_reference_text(), "PIPELINE_LOCAL_NUM_CTX")
        assert "advance-scheduler" in row, (
            "PIPELINE_LOCAL_NUM_CTX row must name the advance-scheduler "
            "launchd plist"
        )
        assert re.search(
            r"no longer|not (?:set|pinned)|removed", row, re.IGNORECASE
        ), (
            "PIPELINE_LOCAL_NUM_CTX row must state it is no longer set/pinned "
            "by the advance-scheduler launchd plist"
        )

    def test_notes_the_per_model_tuning_table_override(self):
        row = _single_row_with_tag(_reference_text(), "PIPELINE_LOCAL_NUM_CTX")
        mentions_table = (
            "per-model" in row.lower() and "tuning table" in row.lower()
        )
        assert mentions_table or _HIGH_TAG in row, (
            "PIPELINE_LOCAL_NUM_CTX row must note that locally-dispatched "
            "models fall through to the per-model tuning table (e.g. the "
            f"{_HIGH_TAG} override)"
        )

    def test_notes_locally_dispatched_models_fall_through(self):
        row = _single_row_with_tag(_reference_text(), "PIPELINE_LOCAL_NUM_CTX")
        assert "fall" in row.lower(), (
            "PIPELINE_LOCAL_NUM_CTX row must state locally-dispatched models "
            "fall through to this global default"
        )


# ---------------------------------------------------------------------------
# Negative / boundary cases for the row-parsing helpers themselves
# ---------------------------------------------------------------------------

# The malformed row an earlier sibling story produced: THREE cells, with
# 131072 in the temperature column and the provenance prose in the num_ctx
# column. It contains the substring "131072", so a naive substring assertion
# would wrongly pass on it.
_MALFORMED_HIGH_ROW = (
    "| `gpt-oss-20b-high:latest` | `131072` | 2026-09-18 manual sweep "
    "(Apple M4, 24GB unified memory) of gpt-oss-20b-high:latest: swept "
    "num_ctx 32768 -> 131072 in 7 steps, 100% GPU throughout, 12GB -> 13GB "
    "resident, no swap growth; 131072 is gpt-oss's own trained ceiling "
    "(values up to 1048576 had no further effect). |"
)


class TestRowParsingHelpers:
    def test_malformed_three_cell_row_is_rejected(self):
        cells = _cells(_MALFORMED_HIGH_ROW)
        assert len(cells) == 3, f"fixture should have 3 cells, got {cells!r}"
        assert "131072" in _MALFORMED_HIGH_ROW  # naive check would pass
        assert not _high_row_is_well_formed(cells), (
            "a 3-cell row with 131072 in the temperature column must NOT be "
            "accepted as well formed"
        )

    def test_131072_in_the_temperature_cell_is_rejected(self):
        cells = _cells(
            "| `gpt-oss-20b-high:latest` | `131072` | `131072` | why |"
        )
        assert not _high_row_is_well_formed(cells)

    def test_131072_in_the_why_cell_is_rejected(self):
        cells = _cells(
            "| `gpt-oss-20b-high:latest` | -- | -- | 131072 |"
        )
        assert not _high_row_is_well_formed(cells)

    def test_wrong_tag_is_rejected(self):
        cells = _cells("| `other-model` | -- | `131072` | why |")
        assert not _high_row_is_well_formed(cells)

    def test_empty_temperature_cell_variants_are_accepted(self):
        for empty in ("", "--", "—", "-"):
            cells = ["`gpt-oss-20b-high:latest`", empty, "131072", "why"]
            assert _high_row_is_well_formed(cells), (
                f"empty temperature cell {empty!r} should be accepted"
            )

    def test_rows_with_tag_returns_empty_list_when_absent(self):
        text = "| `some-other-model` | -- | `131072` | why |\n"
        assert _rows_with_tag(text, _HIGH_TAG) == []

    def test_single_row_with_tag_raises_on_zero_matches(self):
        text = "| `some-other-model` | -- | `131072` | why |\n"
        with pytest.raises(AssertionError, match="expected exactly one"):
            _single_row_with_tag(text, _HIGH_TAG)

    def test_single_row_with_tag_raises_on_duplicate_matches(self):
        text = (
            "| `gpt-oss-20b-high:latest` | -- | `131072` | why |\n"
            "| `gpt-oss-20b-high:latest` | -- | `131072` | why |\n"
        )
        with pytest.raises(AssertionError, match="expected exactly one"):
            _single_row_with_tag(text, _HIGH_TAG)

    def test_rows_with_tag_ignores_prose_mentions(self):
        text = (
            "The shipped plist no longer pins `PIPELINE_LOCAL_NUM_CTX`, so "
            "`gpt-oss-20b-high:latest` effectively runs at 131072.\n"
        )
        assert _rows_with_tag(text, _HIGH_TAG) == []


# ---------------------------------------------------------------------------
# Regression: a table row must never contain a literal two-character "\n"
# ---------------------------------------------------------------------------
#
# A previous edit to REFERENCE.md merged two table rows into ONE physical line
# by writing the two-character sequence backslash + "n" instead of a real
# newline (REFERENCE.md:984). Rendered, the second row
# (``PIPELINE_ROLE_CALL_TIMEOUT_SECONDS``) was swallowed into the first as two
# extra columns and vanished from the table. The tests below pin the shape so
# the same mistake cannot land again.

# The literal two-character sequence backslash + "n" (NOT a newline).
_LITERAL_BACKSLASH_N = "\\n"

# An env-var name in the first cell of the (3-column) environment-variable
# table that the per-model tuning table is glued to.
_ENV_VAR_FIRST_CELL = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _table_rows(text: str) -> list[tuple[int, str]]:
    """Every markdown table row in ``text`` as ``(1-based line number, line)``."""
    return [
        (lineno, line)
        for lineno, line in enumerate(text.splitlines(), 1)
        if line.lstrip().startswith("|")
    ]


def _cells_unescaped(row: str) -> list[str]:
    """Split a markdown row on UNESCAPED pipes only.

    Some rows legitimately contain an escaped pipe (``\\|``) inside a cell, so
    a naive ``str.split("|")`` over-counts their columns. This counts the way a
    markdown renderer does.
    """
    stripped = row.strip().removeprefix("|")
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped)]


def _tuning_table_rows(text: str) -> list[tuple[int, str]]:
    """Rows of the per-model tuning table block, header through the last row.

    The tuning table is glued to the following environment-variable rows (no
    blank line between them), so the block is every contiguous pipe row from
    the ``Model tag`` header until the first non-pipe line. The merged
    ``PIPELINE_LOCAL_TIMEOUT_SECONDS``/``PIPELINE_ROLE_CALL_TIMEOUT_SECONDS``
    line lives inside this block, so a literal ``\\n`` there is caught here.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        cells = _cells(line)
        if cells and cells[0] == _TUNING_TABLE_HEADER_FIRST_CELL:
            start = i
            break
    assert start is not None, (
        "could not find the per-model tuning table header "
        f"(first cell {_TUNING_TABLE_HEADER_FIRST_CELL!r}) in REFERENCE.md"
    )
    rows: list[tuple[int, str]] = []
    for i in range(start, len(lines)):
        line = lines[i]
        if not line.lstrip().startswith("|"):
            break
        rows.append((i + 1, line))
    return rows


def _tuning_model_rows(text: str) -> list[tuple[int, str]]:
    """The per-model rows of the tuning table (first cell is a model tag).

    The legacy environment-variable rows glued onto the end of the block have
    a SCREAMING_SNAKE_CASE first cell and are excluded; only rows whose first
    cell is a model tag are expected to carry the table's 4 columns.
    """
    return [
        (lineno, line)
        for lineno, line in _tuning_table_rows(text)
        if not _ENV_VAR_FIRST_CELL.match(_normalise_tag(_cells(line)[0]))
    ]


class TestNoLiteralBackslashNInTableRows:
    """No table row may embed a literal ``\\n`` (backslash + n)."""

    def test_no_table_row_contains_a_literal_backslash_n(self):
        offenders = [
            (lineno, line)
            for lineno, line in _table_rows(_reference_text())
            if _LITERAL_BACKSLASH_N in line
        ]
        assert not offenders, (
            "table row(s) contain the literal two-character sequence '\\n' "
            "(backslash + n) instead of a real newline, merging two rows into "
            f"one physical line: {offenders!r}"
        )

    def test_helper_detects_a_merged_row(self):
        """Self-test: the fixture reproduces the exact broken shape."""
        merged = (
            "| `PIPELINE_LOCAL_TIMEOUT_SECONDS` | `600` | Legacy. |"
            "\\n| `PIPELINE_ROLE_CALL_TIMEOUT_SECONDS` | `600` | Budget. |"
        )
        assert _LITERAL_BACKSLASH_N in merged
        # 8 pipes -> 7 cells, instead of two 3-cell rows.
        assert len(_cells(merged)) == 7, _cells(merged)
        assert _table_rows(merged) == [(1, merged)]


class TestTimeoutRowsAreSeparatePhysicalLines:
    """The two timeout rows must each be their own physical table line."""

    def test_local_timeout_row_is_a_well_formed_three_cell_row(self):
        row = _single_row_with_tag(
            _reference_text(), "PIPELINE_LOCAL_TIMEOUT_SECONDS"
        )
        cells = _cells(row)
        assert len(cells) == 3, (
            "the `PIPELINE_LOCAL_TIMEOUT_SECONDS` row must be its own 3-cell "
            f"row, got {len(cells)} cells: {row!r}"
        )
        assert _normalise_tag(cells[0]) == "PIPELINE_LOCAL_TIMEOUT_SECONDS"
        assert "PIPELINE_ROLE_CALL_TIMEOUT_SECONDS" not in row, (
            "the `PIPELINE_ROLE_CALL_TIMEOUT_SECONDS` row was merged into the "
            f"`PIPELINE_LOCAL_TIMEOUT_SECONDS` row: {row!r}"
        )
        assert _LITERAL_BACKSLASH_N not in row

    def test_role_call_timeout_row_is_its_own_three_cell_row(self):
        row = _single_row_with_tag(
            _reference_text(), "PIPELINE_ROLE_CALL_TIMEOUT_SECONDS"
        )
        cells = _cells(row)
        assert len(cells) == 3, (
            "the `PIPELINE_ROLE_CALL_TIMEOUT_SECONDS` row must be its own "
            f"3-cell row, got {len(cells)} cells: {row!r}"
        )
        assert cells[0] == "PIPELINE_ROLE_CALL_TIMEOUT_SECONDS"
        assert _LITERAL_BACKSLASH_N not in row


class TestPerModelTuningTableShape:
    """Every per-model tuning table row must have exactly 4 cells (5 pipes)."""

    def test_every_tuning_table_row_has_exactly_four_cells(self):
        rows = _tuning_model_rows(_reference_text())
        assert rows, "no per-model tuning table rows found in REFERENCE.md"
        bad = [
            (lineno, len(_cells(line)), line)
            for lineno, line in rows
            if len(_cells(line)) != 4
        ]
        assert not bad, (
            "every per-model tuning table row must have exactly 4 cells "
            f"(5 pipes); offenders (line, cells, row): {bad!r}"
        )

    def test_tuning_table_rows_do_not_contain_a_literal_backslash_n(self):
        offenders = [
            (lineno, line)
            for lineno, line in _tuning_table_rows(_reference_text())
            if _LITERAL_BACKSLASH_N in line
        ]
        assert not offenders, (
            "per-model tuning table row(s) contain the literal two-character "
            f"sequence '\\n' (backslash + n): {offenders!r}"
        )

    def test_no_tuning_block_row_has_more_than_four_cells(self):
        """A merged row shows up as extra columns (the bug had 7 cells)."""
        rows = _tuning_table_rows(_reference_text())
        assert rows, "no per-model tuning table rows found in REFERENCE.md"
        bad = [
            (lineno, len(_cells_unescaped(line)), line)
            for lineno, line in rows
            if len(_cells_unescaped(line)) > 4
        ]
        assert not bad, (
            "no row in the per-model tuning table block may have more than 4 "
            "cells (a merged row appears as extra columns); offenders "
            f"(line, cells, row): {bad!r}"
        )

    def test_helper_detects_the_merged_row_as_extra_cells(self):
        """Self-test: the exact broken shape yields 7 cells, not 3."""
        merged = (
            "| `PIPELINE_LOCAL_TIMEOUT_SECONDS` | `600` | Legacy. |"
            "\\n| `PIPELINE_ROLE_CALL_TIMEOUT_SECONDS` | `600` | Budget. |"
        )
        assert len(_cells_unescaped(merged)) == 7, _cells_unescaped(merged)
        assert len(_cells_unescaped(merged)) > 4

    def test_helper_ignores_escaped_pipes_when_counting_cells(self):
        """Self-test: an escaped ``\\|`` inside a cell is not a column break."""
        row = "| `PIPELINE_LOCAL_MAX_RISK` | `low` | one \\| two \\| three |"
        assert len(_cells_unescaped(row)) == 3, _cells_unescaped(row)

    def test_helper_cuts_the_block_at_the_first_non_pipe_line(self):
        """Self-test: the block runs to the first non-pipe line."""
        text = (
            "| Model tag | `temperature` | `num_ctx` | Why |\n"
            "|---|---|---|---|\n"
            "| `gpt-oss:20b` | `0.3` |  | why |\n"
            "| `PIPELINE_LOCAL_TIMEOUT_SECONDS` | `600` | Legacy. |\n"
            "\n"
            "prose\n"
        )
        rows = _tuning_table_rows(text)
        assert [lineno for lineno, _ in rows] == [1, 2, 3, 4]

    def test_helper_excludes_env_var_rows_from_the_four_cell_check(self):
        """Self-test: glued env-var rows are not counted as model rows."""
        text = (
            "| Model tag | `temperature` | `num_ctx` | Why |\n"
            "|---|---|---|---|\n"
            "| `gpt-oss:20b` | `0.3` |  | why |\n"
            "| `PIPELINE_LOCAL_TIMEOUT_SECONDS` | `600` | Legacy. |\n"
        )
        assert [lineno for lineno, _ in _tuning_model_rows(text)] == [1, 2, 3]
