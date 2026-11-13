"""Tests for the REFERENCE.md "File size policy" section and env-var row.

Docs-only story (fsp-08). Two anchored edits to ``REFERENCE.md``:

1. A new ``## File size policy (`file_size_policy`)`` section, inserted
   between "Test scope" and "Plan-level ``context`` block".
2. A ``PIPELINE_FILE_SIZE_POLICY`` row in the environment-variable table,
   directly after the ``PIPELINE_TEST_SCOPE`` row.

Assertions target the documented behavior (layer order, default, unknown
and error resolution, enforced/advisory effects) and the exact env row cells.
Rows are located by their leading first cell, never by line number, and
values are asserted by cell index.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REFERENCE_PATH = REPO_ROOT / "REFERENCE.md"

SECTION_HEADING = "## File size policy (`file_size_policy`)"
NEXT_SECTION_HEADING = "## Plan-level `context` block"
PRECEDING_SECTION_HEADING = "## Test scope (`test_scope`)"
ENV_ROW_PREFIX = "| `PIPELINE_FILE_SIZE_POLICY` |"
PRECEDING_ENV_ROW_PREFIX = "| `PIPELINE_TEST_SCOPE` |"


def _reference_text() -> str:
    assert REFERENCE_PATH.exists(), f"REFERENCE.md missing: {REFERENCE_PATH}"
    return REFERENCE_PATH.read_text(encoding="utf-8")


def _section_body() -> str:
    # Whitespace is collapsed so prose assertions survive hard-wrapped lines.
    text = _reference_text()
    start = text.index(SECTION_HEADING)
    end = text.index("\n## ", start + len(SECTION_HEADING))
    return " ".join(text[start:end].split())


def _env_row_lines() -> list[str]:
    return [
        line
        for line in _reference_text().splitlines()
        if line.startswith(ENV_ROW_PREFIX)
    ]


def _cells(row: str) -> list[str]:
    # Split on unescaped pipes only: the description cell uses ``\|`` for the
    # literal "advisory | enforced" separator.
    inner = row.strip()
    assert inner.startswith("|") and inner.endswith("|"), f"not a table row: {row!r}"
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", inner[1:-1])]


# ---------------------------------------------------------------------------
# Section placement
# ---------------------------------------------------------------------------


def test_file_size_policy_section_heading_appears_exactly_once():
    assert _reference_text().count(SECTION_HEADING + "\n") == 1


def test_file_size_policy_section_sits_between_test_scope_and_context_block():
    text = _reference_text()
    test_scope = text.index(PRECEDING_SECTION_HEADING)
    section = text.index(SECTION_HEADING)
    context_block = text.index(NEXT_SECTION_HEADING)
    assert test_scope < section < context_block


# ---------------------------------------------------------------------------
# Section content: resolution
# ---------------------------------------------------------------------------


def test_section_names_both_modes_and_the_advisory_default():
    body = _section_body()
    assert "`advisory`" in body
    assert "`enforced`" in body
    assert "default" in body


def test_section_lists_layers_highest_first():
    body = _section_body()
    team = body.index("team")
    org = body.index("org", team)
    env = body.index("`PIPELINE_FILE_SIZE_POLICY`", org)
    fallback = body.index("default", env)
    assert team < org < env < fallback


def test_section_states_v1_has_no_plan_or_story_field():
    assert "no plan or story field" in _section_body()


def test_section_documents_allow_team_loosen_and_refusal():
    body = _section_body()
    assert "allow_team_loosen" in body
    assert "refused" in body


def test_section_documents_settings_error_resolves_to_enforced():
    body = _section_body()
    assert re.search(r"settings provider error[^.]*`enforced`", body)


def test_section_documents_unknown_value_resolves_to_advisory_and_invalid_layers():
    body = _section_body()
    assert re.search(r"unknown value[^.]*`advisory`", body)
    assert "`invalid_layers`" in body


def test_section_documents_dispatch_stamp():
    assert "`file_size_policy_resolution`" in _section_body()


def test_section_does_not_claim_a_plan_level_override():
    # Negative: v1 has no plan/story field, so the prose must not promise one.
    assert "plan-level `file_size_policy`" not in _section_body()


# ---------------------------------------------------------------------------
# Section content: effects of each mode
# ---------------------------------------------------------------------------


def test_section_documents_advisory_effects_on_implementer_and_reviewer():
    body = _section_body()
    assert "park" in body
    assert "Suggestion" in body
    assert "Nit" in body


def test_section_documents_advisory_ci_warn_mode():
    assert "`scripts/check_line_limit.py --mode warn`" in _section_body()


def test_section_documents_line_count_pins_fail_only_when_enforced():
    body = _section_body()
    assert "PIPELINE_FILE_SIZE_POLICY=enforced" in body
    assert "warn instead of failing" in body


def test_section_documents_test_runs_read_environment_only():
    assert "environment only" in _section_body()


def test_section_documents_notices_stay_notifications_in_both_modes():
    body = _section_body()
    assert "scope_headroom" in body
    assert "notification" in body


# ---------------------------------------------------------------------------
# Environment-variable row
# ---------------------------------------------------------------------------


def test_env_row_appears_exactly_once():
    assert len(_env_row_lines()) == 1


def test_env_row_text_is_exact():
    expected = (
        "| `PIPELINE_FILE_SIZE_POLICY` | `advisory` | `advisory` \\| `enforced` — "
        'the file-size policy request; see "File size policy". '
        "Unknown values resolve to `advisory`. |"
    )
    assert _env_row_lines() == [expected]


def test_env_row_default_cell_is_advisory():
    cells = _cells(_env_row_lines()[0])
    assert cells[0] == "`PIPELINE_FILE_SIZE_POLICY`"
    assert cells[1] == "`advisory`"


def test_env_row_does_not_default_to_enforced():
    # Negative: the secure-by-default requirement is advisory, never enforced.
    cells = _cells(_env_row_lines()[0])
    assert cells[1] != "`enforced`"


def test_env_row_follows_the_test_scope_row_directly():
    lines = _reference_text().splitlines()
    preceding = next(
        i for i, line in enumerate(lines) if line.startswith(PRECEDING_ENV_ROW_PREFIX)
    )
    assert lines[preceding + 1].startswith(ENV_ROW_PREFIX)
