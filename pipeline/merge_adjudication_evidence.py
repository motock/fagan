"""When a high-risk merge may be ruled on, and how complete its CI evidence is.

The CI states split into three groups (operator ruling, 2026-11-25):

* ``pass`` and ``fail`` conclude: the overlord rules and the record carries
  ``evidence_complete: true``.
* ``none`` (no CI configured) and ``cancelled`` will not change by waiting: the
  overlord rules, with ``evidence_complete: false`` and an evidence note that
  says so.
* Anything else - ``pending``, ``unreadable``, a snapshot with no state, or a
  failed gather that left no state - defers. The hold stands, no record is
  written, and ``merge_readjudication`` re-rules once the state changes.

``merge._adjudicate_high_risk_merge`` is the only caller of the deferral rule.
"""

from __future__ import annotations

from typing import Any

from .forge import CANCELLED, FAIL, NONE, PASS

CONCLUDED_STATES = frozenset({PASS, FAIL})
RULING_STATES = CONCLUDED_STATES | frozenset({NONE, CANCELLED})

CI_GATE_DISABLED_ERROR = "CI gate disabled"

HOST_TESTS_SKIPPED_WORDING = (
    "host tests are skipped by deployment policy; PR CI is the test evidence"
)

_NOTES = {
    NONE: "no CI configured for this repository",
    CANCELLED: "CI was cancelled",
}


def _snapshot_state(checks: Any) -> Any:
    return checks.get("state") if isinstance(checks, dict) else None


def ci_defers_ruling(checks: Any) -> bool:
    """True when the CI snapshot is not in a state the overlord may rule on."""
    return _snapshot_state(checks) not in RULING_STATES


def ci_evidence_complete(checks: Any) -> bool:
    """True when the CI snapshot has concluded (success or failure)."""
    return _snapshot_state(checks) in CONCLUDED_STATES


def ci_evidence_note(checks: Any) -> str:
    """The sentence the overlord reads beside a ruling made on this snapshot.

    Empty for a concluded run with a plain result. A ``pass`` produced by a
    disabled gate says so, so it is not read as a green run.
    """
    state = _snapshot_state(checks)
    if state == PASS and checks.get("error") == CI_GATE_DISABLED_ERROR:
        return "CI gate disabled for this repository; no CI result was checked"
    return _NOTES.get(state, "")


def evidence_complete(story: dict[str, Any]) -> bool:
    """True when the story's gathered CI snapshot has concluded."""
    return ci_evidence_complete(story.get("pr_checks"))
