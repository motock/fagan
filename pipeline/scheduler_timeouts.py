"""Env-driven timeout and threshold resolvers for the scheduler daemon."""
import importlib
import logging
import math
import os

logger = logging.getLogger(__name__)

# Scan-phase watchdog (story sh-02): ``scan_fn`` runs in a worker daemon-thread
# per ``run_once`` and the parent waits with a bounded join. On 2026-09-02 one
# wedged LLM call inside a scan tick froze the ENTIRE loop for ~40 minutes
# because ``run_once`` called ``scan_fn`` synchronously on the main thread.
# The watchdog bounds the blast radius: a hung tick degrades to one skipped
# tick instead of a full freeze. Transport-level timeouts are story sh-01.
_SCAN_JOIN_TIMEOUT_ENV = "PIPELINE_SCAN_JOIN_TIMEOUT_SECONDS"
_DEFAULT_SCAN_JOIN_TIMEOUT_S = 900.0


# Ceiling on the notification outbox drain phase. See the drain block in
# ``run_once`` for why this is not routed through ``_run_with_watchdog``.
_DRAIN_JOIN_TIMEOUT_SECONDS = 120.0
# Operator override for the drain ceiling, read per call like the scan and
# reconcile join deadlines (``_drain_join_timeout_seconds`` below); when the
# variable is unset the module constant above is the default — and it stays
# the monkeypatch seam tests use to shrink the ceiling.
_DRAIN_JOIN_TIMEOUT_ENV = "PIPELINE_DRAIN_JOIN_TIMEOUT_SECONDS"


def _drain_join_timeout_seconds() -> float:
    """Read the drain join deadline from the environment, per call.

    Mirrors :func:`_scan_join_timeout_seconds` and
    :func:`_reconcile_join_timeout_seconds`: malformed, non-finite, and
    non-positive overrides degrade to the default instead of crashing the
    loop (a non-finite deadline would silently disable the bound, and
    ``Thread.join`` on one is unspecified). When the variable is unset the
    module constant :data:`_DRAIN_JOIN_TIMEOUT_SECONDS` is the default —
    which is also the seam tests monkeypatch to shrink the ceiling.
    """
    raw = os.environ.get(_DRAIN_JOIN_TIMEOUT_ENV)
    if raw is None:
        return _DRAIN_JOIN_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s must be a number, got %r; using default %gs",
            _DRAIN_JOIN_TIMEOUT_ENV,
            raw,
            _DRAIN_JOIN_TIMEOUT_SECONDS,
        )
        return _DRAIN_JOIN_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        logger.warning(
            "%s must be a finite positive number, got %r; using default %gs",
            _DRAIN_JOIN_TIMEOUT_ENV,
            raw,
            _DRAIN_JOIN_TIMEOUT_SECONDS,
        )
        return _DRAIN_JOIN_TIMEOUT_SECONDS
    return value


def _scan_join_timeout_seconds() -> float:
    """Read the scan join deadline from the environment, per call.

    Malformed values degrade to the default (900s) instead of crashing the
    loop: a bad operator override must never take the daemon down. This
    includes non-finite values: ``inf`` would disable the watchdog entirely
    (a bounded join against infinity never fires) and ``nan`` gives
    ``Thread.join`` unspecified behaviour.
    """
    raw = os.environ.get(_SCAN_JOIN_TIMEOUT_ENV)
    if raw is None:
        return _DEFAULT_SCAN_JOIN_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s must be a number, got %r; using default %gs",
            _SCAN_JOIN_TIMEOUT_ENV,
            raw,
            _DEFAULT_SCAN_JOIN_TIMEOUT_S,
        )
        return _DEFAULT_SCAN_JOIN_TIMEOUT_S
    if not math.isfinite(value):
        logger.warning(
            "%s must be finite, got %r; using default %gs",
            _SCAN_JOIN_TIMEOUT_ENV,
            raw,
            _DEFAULT_SCAN_JOIN_TIMEOUT_S,
        )
        return _DEFAULT_SCAN_JOIN_TIMEOUT_S
    if value <= 0:
        logger.warning(
            "%s must be positive, got %r; using default %gs",
            _SCAN_JOIN_TIMEOUT_ENV,
            raw,
            _DEFAULT_SCAN_JOIN_TIMEOUT_S,
        )
        return _DEFAULT_SCAN_JOIN_TIMEOUT_S
    return value


# Reconcile-phase watchdog (story LOCKSTARVE-C1): ``reconcile_fn`` runs through
# the same bounded-join worker as ``scan_fn``. On 2026-09-11 a wedged
# reconcile iteration ran 66 minutes (14:31:50Z -> 15:37:59Z) during which
# ``run_once`` never completed and ``last_scan_ts`` never advanced; the
# watchdog bounds that blast radius to one skipped reconcile tick.
_RECONCILE_JOIN_TIMEOUT_ENV = "PIPELINE_RECONCILE_JOIN_TIMEOUT_SECONDS"
_DEFAULT_RECONCILE_JOIN_TIMEOUT_S = 900.0


def _apply_scheduler_role_call_clamp() -> float:
    """Bound the scheduler process's per-call model budget (story SRR-1).

    ``resolve_role_call_timeout()`` is read per call (never cached at import),
    and the scheduler process is exactly the process whose per-tick stacking
    must be bounded: one ``advance_all_plans`` tick stacks several in-process
    model calls (review-loop turns, security review, overlord
    adjudications), while interactive MCP-server processes keep the 600s
    default. Sizing arithmetic: with the 180s clamp, 3-4 stacked calls worst
    case (~540-720s) plus bounded suite runs stay inside the 900s reconcile
    join deadline, so the watchdog returns to being a backstop rather than
    the primary bound.

    When the operator's PIPELINE_ROLE_CALL_TIMEOUT_SECONDS already resolves
    to a smaller-or-equal value it is left untouched; otherwise
    ``os.environ`` is rewritten to the clamped value so every later
    ``resolve_role_call_timeout()`` call in this process sees it. Fail-closed
    semantics are unchanged: a clamped-out call raises exactly the
    RuntimeError ``complete()`` raises today, and callers already route that
    to park/defer.
    """
    # Call-time module fetch instead of an import statement: this module's
    # static import graph must stay stdlib+pipeline
    # (test_scheduler_daemon_imports_remain_stdlib_or_pipeline), and both
    # resolvers are read per call, so resolving app.inference_providers here
    # keeps both properties. sys.modules returns the same module object, so
    # monkeypatched resolver attributes on it stay visible call to call.
    providers = importlib.import_module("app.inference_providers")
    operator = providers.resolve_role_call_timeout()
    clamped = min(operator, providers.resolve_scheduler_role_call_timeout())
    if clamped < operator:
        # os.environ values must be strings; the resolver re-reads this on
        # every subsequent model call in this process.
        os.environ["PIPELINE_ROLE_CALL_TIMEOUT_SECONDS"] = str(clamped)
    return clamped


def _reconcile_join_timeout_seconds() -> float:
    """Read the reconcile join deadline from the environment, per call.

    Mirrors :func:`_scan_join_timeout_seconds` exactly: malformed,
    non-positive, and non-finite values degrade to the default (900s)
    instead of crashing the loop — a bad operator override must never take
    the daemon down, and a non-finite deadline must never silently disable
    the watchdog.
    """
    raw = os.environ.get(_RECONCILE_JOIN_TIMEOUT_ENV)
    if raw is None:
        return _DEFAULT_RECONCILE_JOIN_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s must be a number, got %r; using default %gs",
            _RECONCILE_JOIN_TIMEOUT_ENV,
            raw,
            _DEFAULT_RECONCILE_JOIN_TIMEOUT_S,
        )
        return _DEFAULT_RECONCILE_JOIN_TIMEOUT_S
    if not math.isfinite(value):
        logger.warning(
            "%s must be finite, got %r; using default %gs",
            _RECONCILE_JOIN_TIMEOUT_ENV,
            raw,
            _DEFAULT_RECONCILE_JOIN_TIMEOUT_S,
        )
        return _DEFAULT_RECONCILE_JOIN_TIMEOUT_S
    if value <= 0:
        logger.warning(
            "%s must be positive, got %r; using default %gs",
            _RECONCILE_JOIN_TIMEOUT_ENV,
            raw,
            _DEFAULT_RECONCILE_JOIN_TIMEOUT_S,
        )
        return _DEFAULT_RECONCILE_JOIN_TIMEOUT_S
    return value


# Abandon-restart escape hatch (story LOCKSTARVE-C2): process death is the
# only thing that releases a flock, and the launchd job
# com.fagan.pipeline.advance-scheduler has KeepAlive=true, so exiting IS the
# recovery. After this many CONSECUTIVE watchdog-worker abandonments (scan or
# reconcile) the daemon writes its health file, logs an ERROR naming a leaked
# plan lock as the suspected cause, and raises SystemExit(1) so launchd
# restarts the process and the leaked ``_plan_lock`` flock is released.
_ABANDON_RESTART_THRESHOLD_ENV = "PIPELINE_SCAN_ABANDON_RESTART_THRESHOLD"
_DEFAULT_ABANDON_RESTART_THRESHOLD = 3


def _abandon_restart_threshold() -> int:
    """Read the abandon-restart threshold from the environment, per call.

    Mirrors :func:`_scan_join_timeout_seconds`' idiom: the value is read from
    the environment on every call (never cached on the daemon, so a live env
    change is honoured) and a malformed value degrades to the default (3)
    with a warning instead of crashing the loop.

    Deliberate difference from the join-timeout helpers: a value <= 0 is
    returned UNCHANGED and DISABLES the escape hatch entirely (the daemon
    never restarts itself), rather than being substituted with the default.
    An operator must be able to turn the hatch off; clamping <= 0 to the
    default would silently re-arm a hatch the operator explicitly disabled.
    """
    raw = os.environ.get(_ABANDON_RESTART_THRESHOLD_ENV)
    if raw is None:
        return _DEFAULT_ABANDON_RESTART_THRESHOLD
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s must be an integer, got %r; using default %d",
            _ABANDON_RESTART_THRESHOLD_ENV,
            raw,
            _DEFAULT_ABANDON_RESTART_THRESHOLD,
        )
        return _DEFAULT_ABANDON_RESTART_THRESHOLD
    return value


# Abandoned-worker grace window (story SRR-2): an abandoned watchdog worker
# that is STILL ALIVE this long after its abandonment is a blocked call that
# has not returned, so the plan ``_plan_lock`` it holds is leaked on a horizon
# the daemon cannot influence — process death is the only release. With the
# SRR-1 clamp no model call should outlive ~180s, so a worker still alive 300s
# past its abandonment is genuinely stuck, not merely slow.
_ABANDON_WORKER_GRACE_ENV = "PIPELINE_ABANDON_WORKER_GRACE_SECONDS"
_DEFAULT_ABANDON_WORKER_GRACE_S = 300.0


def _abandon_worker_grace_seconds() -> float:
    """Read the abandoned-worker grace window from the environment, per call.

    Mirrors :func:`_reconcile_join_timeout_seconds` exactly: malformed,
    non-positive, and non-finite values degrade to the default (300s) with a
    warning instead of crashing the loop. The specific failure modes this
    guards against: accepting ``0``/negative would make ``elapsed > grace``
    true on the very first alive worker (an instant restart); accepting
    ``nan`` or ``inf`` would make every comparison False and silently disable
    the leak detector forever.
    """
    raw = os.environ.get(_ABANDON_WORKER_GRACE_ENV)
    if raw is None:
        return _DEFAULT_ABANDON_WORKER_GRACE_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s must be a number, got %r; using default %gs",
            _ABANDON_WORKER_GRACE_ENV,
            raw,
            _DEFAULT_ABANDON_WORKER_GRACE_S,
        )
        return _DEFAULT_ABANDON_WORKER_GRACE_S
    if not math.isfinite(value):
        logger.warning(
            "%s must be finite, got %r; using default %gs",
            _ABANDON_WORKER_GRACE_ENV,
            raw,
            _DEFAULT_ABANDON_WORKER_GRACE_S,
        )
        return _DEFAULT_ABANDON_WORKER_GRACE_S
    if value <= 0:
        logger.warning(
            "%s must be positive, got %r; using default %gs",
            _ABANDON_WORKER_GRACE_ENV,
            raw,
            _DEFAULT_ABANDON_WORKER_GRACE_S,
        )
        return _DEFAULT_ABANDON_WORKER_GRACE_S
    return value
