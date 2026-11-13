"""Guard tests for the stage C status text in ADR 0012.

Stage C recorded what was delivered and listed the follow-ups. These tests pin
the follow-up list (it must name stage D and leases) and the Proposed status,
which moving the record to Accepted is the maintainer's call to change. They
read the ADR as text, as tests/unit/test_adr_0012_store_contract.py does.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADR = ROOT / "docs/adr/0012-store-contract-v2.md"
FOLLOW_UPS_HEADING = "## Migration status and follow-ups"


def adr_text() -> str:
    """Return the ADR's text, failing with an assertion when it is missing."""
    assert ADR.exists(), f"missing {ADR}"
    return ADR.read_text(encoding="utf-8")


def follow_ups_section() -> str:
    """Return the text from the follow-ups heading to the next level-2 heading."""
    text = adr_text()
    assert FOLLOW_UPS_HEADING in text, f"missing heading {FOLLOW_UPS_HEADING!r}"
    tail = text.split(FOLLOW_UPS_HEADING, 1)[1]
    return tail.split("\n## ", 1)[0]


def test_follow_ups_name_stage_d():
    """The follow-up list names stage D (the PLAN_DIR/default/ layout migration)."""
    assert "stage D" in follow_ups_section()


def test_follow_ups_name_leases():
    """The follow-up list names leases and the advance queue (ADR 0011 S2)."""
    assert "leases" in follow_ups_section()


def test_status_line_is_still_proposed():
    """Recording stage C's status does not change the Proposed status line."""
    assert "**Status:** Proposed" in adr_text()
