"""Guard tests for the security-audit row in ADR 0012's record table.

Decision 3 of ADR 0012 lists the namespaced control-plane record kinds. The
per-repository security-audit state (pipeline/security_audit.py) is one of
them, so its row must sit inside that table and must not carry an absolute
path. The Amended line must record the addition. These tests read the ADR as
text, as tests/unit/test_adr_0012_store_contract.py does.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADR = ROOT / "docs/adr/0012-store-contract-v2.md"

ROW_PREFIX = "   | Security-audit state (per repository) | `security-audit.<digest>.json` |"
PLAN_ROW_MARKER = "| Plan completion marker and report |"
LEASES_ROW_MARKER = "| Leases and the advance queue |"
AMENDED_PREFIX = "- **Amended:**"
AMENDED_PHRASE = "per-repository security-audit state added to the record kinds in decision 3"


def adr_lines() -> list[str]:
    """Return the ADR as a list of lines, failing when the file is missing."""
    assert ADR.exists(), f"missing {ADR}"
    return ADR.read_text(encoding="utf-8").splitlines()


def row_lines() -> list[str]:
    """Return every line that starts with the security-audit row prefix."""
    return [line for line in adr_lines() if line.startswith(ROW_PREFIX)]


def index_of(marker: str) -> int:
    """Return the index of the single line containing the marker."""
    matches = [i for i, line in enumerate(adr_lines()) if marker in line]
    assert len(matches) == 1, f"expected one line containing {marker!r}, found {len(matches)}"
    return matches[0]


def test_security_audit_row_is_in_the_record_table():
    """Decision 3 has a row for the per-repository security-audit state."""
    assert len(row_lines()) == 1


def test_security_audit_row_sits_between_plan_completion_and_leases():
    """The row is inside decision 3's table, not appended elsewhere in the ADR."""
    row = index_of(ROW_PREFIX)
    assert index_of(PLAN_ROW_MARKER) < row < index_of(LEASES_ROW_MARKER)


def test_amended_line_records_the_security_audit_addition():
    """The Amended line says the security-audit state was added to decision 3."""
    amended = [line for line in adr_lines() if line.startswith(AMENDED_PREFIX)]
    assert len(amended) == 1
    assert AMENDED_PHRASE in amended[0]


def test_security_audit_row_contains_no_absolute_path():
    """The row names a relative record file, never a host-specific absolute path."""
    rows = row_lines()
    assert len(rows) == 1, "security-audit row is missing"
    row = rows[0]
    for forbidden in ("/run/", "/Users/", "PLAN_DIR/"):
        assert forbidden not in row, f"security-audit row contains {forbidden!r}"
