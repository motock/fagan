"""Cross-process wake markers for the scheduler's plan scan.

Operator actions (approve, resume, ingest, set status, patch, mark done) run in
the MCP server or the dashboard, not in the scheduler, so an in-process bus
event cannot reach the scheduler's handlers on the default bus. One marker file
per plan is the transport that works on every bus: it coalesces naturally, so a
burst of operator actions costs a single sweep.

The marker body is a diagnostic breadcrumb only -- nothing here or in the
scheduler trusts or decides on it. The validated file *name* is the whole
signal: a plan with a marker is a plan to reconcile.

Delivery uses the same claim-by-rename idiom as
:func:`pipeline.watchers.scan_done_markers`: a pass that wins the rename owns
the marker, so concurrent passes never deliver one wake twice.
"""

import json
import logging
import os
import tempfile
from datetime import UTC, datetime

from . import paths
from .parsers import _validate_key

logger = logging.getLogger(__name__)

#: Directory under ``PLAN_DIR`` holding one marker file per plan awaiting a
#: reconcile. Private to the pipeline's user (mode 0o700).
WAKE_DIR_NAME = ".wake"


def _wake_dir():
    return paths.PLAN_DIR / WAKE_DIR_NAME


def request_wake(plan_name: str, reason: str) -> None:
    """Record that *plan_name* should be reconciled on the next scan.

    Never raises. A wake that cannot be written is logged at WARNING with the
    plan and the exception class, because a lost wake costs at most one
    stale-plan sweep: the plan's next organic event re-reads the manifest, and
    the operator can simply repeat the action.

    The marker is written to a ``.``-prefixed temp file and renamed into place,
    so a scan never observes a half-written marker, and the temp name (skipped
    by :func:`claim_wakes`) cannot be mistaken for a wake.
    """
    try:
        _validate_key(plan_name)
    except ValueError:
        logger.warning(
            "wake request rejected: invalid plan name",
            extra={"plan": plan_name, "reason": reason},
        )
        return
    try:
        wake_dir = _wake_dir()
        wake_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps({"reason": reason, "ts": datetime.now(UTC).isoformat()})
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{plan_name}.", suffix=".tmp", dir=str(wake_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, wake_dir / plan_name)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as exc:  # noqa: BLE001 - a lost wake must never break a caller
        logger.warning(
            "wake request failed: %s",
            type(exc).__name__,
            extra={"plan": plan_name, "reason": reason},
        )


def claim_wakes() -> list[str]:
    """Claim every pending wake and return the plan names, each at most once.

    A marker is claimed by renaming it to a ``.``-prefixed ``.claimed`` name and
    then unlinking it. ``FileNotFoundError`` from the rename means another pass
    claimed it first; that pass delivers it, so this one skips quietly.

    Names that fail :func:`pipeline.parsers._validate_key` -- or that start with
    ``.`` (temp and already-claimed files) -- are skipped and left on disk for
    inspection. A missing wake directory simply means nothing is pending.
    """
    wake_dir = _wake_dir()
    try:
        entries = sorted(wake_dir.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:  # an unreadable dir means nothing is pending
        logger.warning("wake scan failed: %s", type(exc).__name__)
        return []
    claimed: list[str] = []
    for entry in entries:
        plan_name = entry.name
        if plan_name.startswith("."):
            continue
        try:
            _validate_key(plan_name)
        except ValueError:
            logger.warning(
                "skipping invalid wake marker", extra={"plan": plan_name}
            )
            continue
        claimed_path = wake_dir / f".{plan_name}.claimed"
        try:
            os.replace(entry, claimed_path)
        except FileNotFoundError:
            # Another pass won the rename; it delivers the wake.
            continue
        except OSError as exc:  # leave it on disk for a later pass
            logger.warning(
                "wake claim failed: %s", type(exc).__name__, extra={"plan": plan_name}
            )
            continue
        try:
            claimed_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # the rename already claimed it
            logger.warning(
                "claimed wake cleanup failed: %s",
                type(exc).__name__,
                extra={"plan": plan_name},
            )
        claimed.append(plan_name)
    return claimed


def after(plan_name: str, reason: str, result: object) -> object:
    """Wake *plan_name* when an operator method's *result* is a success.

    Lets a mutating MCP tool wake the scheduler in one line::

        return wake.after(plan_name, "approve", result)

    A result dict is a success unless it says ``ok: False`` or carries a truthy
    ``skipped``; anything that is not a dict is returned untouched. The result
    is always returned unchanged, so callers keep their existing contract.
    """
    if isinstance(result, dict) and result.get("ok") is not False and not result.get("skipped"):
        request_wake(plan_name, reason)
    return result
