"""coa-00: the parked merge-hold re-adjudication lives in its own module.

Mechanical extraction: ``_readjudicate_parked_merge_hold`` and
``_MERGE_HOLD_REASON`` moved out of ``pipeline/advance_merge.py`` (which sat at
the 1000-line limit) into ``pipeline/merge_readjudication.py``. These tests pin
the observable wiring:

* ``advance_merge`` keeps the name bound to the SAME function object, so the
  existing monkeypatch target ``advance_merge._readjudicate_parked_merge_hold``
  still lands on the call site in ``_adjudicate_merges``.
* ``pipeline.advance`` still re-exports the hold reason with its exact value.
* ``advance_merge.py`` is back under the 950-line headroom it was cut to.
"""

from pathlib import Path

# A new test file that imports a pipeline.* module must import pipeline.server
# first (pre-existing import cycle, see tests/unit/test_triage_cap_silent_skip.py).
import pipeline.server  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[2]
ADVANCE_MERGE_PATH = REPO_ROOT / "pipeline" / "advance_merge.py"


def test_advance_merge_binds_the_moved_readjudication_function():
    from pipeline import advance_merge, merge_readjudication

    assert (
        advance_merge._readjudicate_parked_merge_hold
        is merge_readjudication._readjudicate_parked_merge_hold
    )


def test_advance_reexports_the_merge_hold_reason_with_its_exact_value():
    from pipeline import advance

    assert advance._MERGE_HOLD_REASON == "high risk held for human review"


def test_advance_merge_stays_under_the_950_line_headroom():
    line_count = len(ADVANCE_MERGE_PATH.read_text().splitlines())

    assert line_count <= 950, (
        f"pipeline/advance_merge.py is {line_count} lines; it must stay at or "
        "under 950 so the coa-05 change has headroom"
    )
