"""Pure wedged-story verdict function for dispatched pipeline stories.

``wedge_verdict`` decides whether a dispatched story looks wedged. It is
PURE by contract: nothing in its call path imports os/subprocess/time, reads
the environment, or reads a clock. Callers pass in

- ``pid_alive``: three-valued liveness. True (process alive), False (dead OR
  zombie -- pipeline/story_status.py establishes that os.kill(pid, 0)
  succeeds for defunct processes and only a ``ps -o stat=`` value starting
  with Z distinguishes them, so the caller collapses both into False), or
  None (story has no pid / liveness unknown). None and True are never wedge
  reasons.
- ``activity_age_seconds``: measured seconds since the newest worktree
  activity signal (journal mtime / agent.log mtime), or None when no signal
  exists. None means "cannot prove staleness" and is fail-open (same
  convention as pipeline/usage.py's staleness helper), never a reason on its
  own. Negative ages (future mtime / clock skew) are never reasons either.
- ``stale_seconds``: threshold resolved by the caller from pipeline.config
  (WEDGE_STALE_ACTIVITY_SECONDS).

The verdict carries the measured reading next to the threshold so a
mis-thresholded detector is diagnosable from its own output (see
.claude/rules/testing-config-gates.md). Wiring this into a caller is a
separate story; this module must stay free of I/O.
"""

from __future__ import annotations


def wedge_verdict(
    pid_alive: bool | None,
    activity_age_seconds: float | None,
    stale_seconds: int,
) -> dict:
    """Return the wedged-story verdict for the given measurements.

    Pure: thresholds come from the caller (pipeline.config), measurements
    come from the caller's gatherers. No clock, environ, or OS access here.
    """
    reasons = []

    # pid_alive is three-valued: only an explicit False (dead or zombie, as
    # collapsed by the caller's liveness check) is a wedge reason. `is False`
    # matters -- `if not pid_alive:` would wrongly fire for None (a story
    # that merely has no pid field), because `not None` is True.
    if pid_alive is False:
        reasons.append("dead_pid")

    # Strictly greater: age exactly equal to the threshold is NOT wedged.
    # None means "cannot prove staleness" (fail-open) and negative age means
    # future mtime / clock skew -- neither is a reason, so no special branch
    # and no normalization (abs()/clamping) is wanted here.
    if activity_age_seconds is not None and activity_age_seconds > stale_seconds:
        reasons.append("stale_activity")

    # Deterministic output regardless of the order the checks fired in.
    reasons.sort()

    return {
        "wedged": len(reasons) > 0,
        "reasons": reasons,
        "measured": {
            "pid_alive": pid_alive,
            "activity_age_seconds": activity_age_seconds,
        },
        "thresholds": {"stale_seconds": stale_seconds},
    }