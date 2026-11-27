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
import re
import tempfile
from datetime import UTC, datetime
from typing import TypeVar

from app.credentials import is_default_namespace
from pipeline.actor import current_namespace
from pipeline.namespace_names import is_segmented_namespace

from .host_state import build_host_state
from .parsers import _validate_key

logger = logging.getLogger(__name__)

#: Directory under the host root holding one marker file per plan awaiting a
#: reconcile. Private to the pipeline's user (mode 0o700).
WAKE_DIR_NAME = ".wake"

#: Separates the org, team and plan parts of a namespaced marker name,
#: ``<org>@<team>@<plan>``. Neither a namespace segment
#: (``pipeline.namespace_names.SEGMENT``) nor a plan name
#: (``pipeline.parsers._KEY_RE``) can contain ``@``, so the split is unambiguous.
_SEPARATOR = "@"

#: The character set a marker file name may use: a namespace segment, the
#: ``@`` separator, or a plan name. No ``/``, ``\\``, NUL or other character is
#: allowed, so the name is a bare file name. A plain bounded character class with
#: no lookahead or nested repeats, so the match is linear. ``..``, a leading ``.``
#: and a ``@.`` (a plan part starting with ``.``) are refused by plain string
#: checks in :func:`request_wake`, because the temp and claimed-file namespace is
#: the ``.``-prefixed one.
_MARKER_NAME = re.compile(r"[A-Za-z0-9_@-][A-Za-z0-9._@-]{0,254}")


def _wake_dir():
    # Host-local derived state (ADR 0012 decision 9): one marker per plan under
    # the host root, not a replicated record.
    return build_host_state().root() / WAKE_DIR_NAME


def _scope_prefix(namespace: str) -> str | None:
    """Return ``<org>@<team>@`` for a two-segment *namespace*, else None.

    A single-segment namespace has no org/team pair to name a marker with, so
    it has no prefix: its wakes are refused and it claims none.
    """
    if not is_segmented_namespace(namespace):
        return None
    org, team = namespace.split("/")
    return f"{org}{_SEPARATOR}{team}{_SEPARATOR}"


def _split_marker(marker: str) -> tuple[str, str] | None:
    """Split a namespaced marker name into ``(<org>@<team>@, <plan>)``.

    Returns None when the name does not have exactly three parts, or when its
    namespace or plan part fails validation. The result never echoes the name.
    """
    parts = marker.split(_SEPARATOR)
    if len(parts) != 3:
        return None
    org, team, plan_name = parts
    if not is_segmented_namespace(f"{org}/{team}"):
        return None
    try:
        _validate_key(plan_name)
    except ValueError:
        return None
    return f"{org}{_SEPARATOR}{team}{_SEPARATOR}", plan_name


def request_wake(plan_name: str, reason: str) -> None:
    """Record that *plan_name* should be reconciled on the next scan.

    The marker is scoped to the bound namespace: the default namespace writes
    ``<plan_name>``, any other writes ``<org>@<team>@<plan_name>``.

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
    namespace = current_namespace()
    if is_default_namespace(namespace):
        marker = plan_name
    else:
        prefix = _scope_prefix(namespace)
        if prefix is None:
            logger.warning(
                "wake request rejected: namespace cannot scope a marker",
                extra={"plan": plan_name, "reason": reason},
            )
            return
        marker = f"{prefix}{plan_name}"
    try:
        wake_dir = _wake_dir()
        base = os.path.normpath(str(wake_dir))
        destination = os.path.normpath(os.path.join(base, marker))
        # The marker is built from the bound namespace and the plan name, so it
        # is checked here, in the function that writes it: a bare name, no
        # ``..``, no leading ``.`` (also after ``@``), and a destination that
        # stays inside the wake directory.
        if (
            _MARKER_NAME.fullmatch(marker) is None
            or ".." in marker
            or marker.startswith(".")
            or "@." in marker
            or os.path.basename(marker) != marker
            or not destination.startswith(base + os.sep)
        ):
            logger.warning(
                "wake request rejected: unsafe marker name",
                extra={"reason": reason},
            )
            return
        wake_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps({"reason": reason, "ts": datetime.now(UTC).isoformat()})
        # The temp name carries no marker text: claim_wakes skips every
        # ``.``-prefixed name, so a crash leaves a file no scan mistakes for a wake.
        fd, tmp_name = tempfile.mkstemp(prefix=".wake.", suffix=".tmp", dir=base)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, destination)
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
    """Claim every pending wake for the bound namespace; return the plan names.

    Each plan is returned at most once. Under the default binding the claimed
    markers are the bare names (no ``@``); under any other binding they are the
    ``<org>@<team>@`` markers of that namespace, and the plan part is returned.
    A well-formed marker that belongs to another namespace is left on disk,
    silently, for that namespace's pass.

    A marker is claimed by renaming it to a ``.``-prefixed ``.claimed`` name and
    then unlinking it. ``FileNotFoundError`` from the rename means another pass
    claimed it first; that pass delivers it, so this one skips quietly.

    Names that start with ``.`` (temp and already-claimed files) are skipped. A
    name that fails validation is skipped and left on disk for inspection; one
    containing ``@`` is logged without its name, since it may be a path-like
    fragment. A missing wake directory simply means nothing is pending.
    """
    namespace = current_namespace()
    if is_default_namespace(namespace):
        prefix = None
    else:
        prefix = _scope_prefix(namespace)
        if prefix is None:
            return []
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
        marker = entry.name
        if marker.startswith("."):
            continue
        if _SEPARATOR in marker:
            split = _split_marker(marker)
            if split is None:
                logger.warning("skipping invalid wake marker")
                continue
            owner_prefix, plan_name = split
            if owner_prefix != prefix:
                continue
        else:
            if prefix is not None:
                continue
            try:
                _validate_key(marker)
            except ValueError:
                logger.warning(
                    "skipping invalid wake marker", extra={"plan": marker}
                )
                continue
            plan_name = marker
        claimed_path = wake_dir / f".{marker}.claimed"
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


_T = TypeVar("_T")


def after(plan_name: str, reason: str, result: _T) -> _T:
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
