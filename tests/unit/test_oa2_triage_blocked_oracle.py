"""Regression tests: ``blocked_oracle`` stories are triage candidates (OA2-01).

A story whose acceptance oracle is born-broken, already-passing, or empty is
set to status ``blocked_oracle`` by ``pipeline/dispatch.py``. Before this
change the triage sweep only considered ``parked``/``failed`` stories (plus a
step-cap streak), so those stories were permanently invisible to the sweep -
even to the ``patch_acceptance`` executor that exists to fix them.

``triage_candidates`` is a CUMULATIVE artifact: later stories add more
candidate statuses. These tests therefore assert MEMBERSHIP only - never the
exact set, the total count, or the ordering of candidates.
"""

# Import pipeline.server FIRST: pipeline.triage -> build_detect -> server ->
# triage is a circular import, so importing pipeline.triage standalone raises
# ImportError. Importing the server module first breaks the cycle. Same idiom as
# tests/unit/test_triage_cap_silent_skip.py.
import pipeline.server  # noqa: F401  (breaks the triage <-> server cycle)
from pipeline import triage as triage_mod


def _manifest() -> dict:
    """Five stories covering the new status plus the boundaries."""
    return {
        "s_blocked": {"status": "blocked_oracle"},
        "s_done": {"status": "done"},
        "s_todo": {"status": "todo"},
        "s_parked": {"status": "parked"},
        "s_failed": {"status": "failed"},
    }


def test_blocked_oracle_is_a_candidate():
    ids = set(triage_mod.triage_candidates(_manifest()))
    assert "s_blocked" in ids


def test_parked_and_failed_still_candidates():
    ids = set(triage_mod.triage_candidates(_manifest()))
    assert "s_parked" in ids
    assert "s_failed" in ids


def test_done_and_todo_are_never_candidates():
    ids = set(triage_mod.triage_candidates(_manifest()))
    assert "s_done" not in ids
    assert "s_todo" not in ids


def test_blocked_oracle_candidate_is_stable_across_repeated_calls():
    # triage_candidates must not mutate the manifest while selecting: a second
    # call on the SAME object must still see the blocked_oracle story.
    manifest = _manifest()
    first = set(triage_mod.triage_candidates(manifest))
    second = set(triage_mod.triage_candidates(manifest))
    assert "s_blocked" in first
    assert "s_blocked" in second
