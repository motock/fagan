"""Plan-name validation shared by the write paths (save, ingest).

The audit anchor log keys the *host chain* as ``host``
(``pipeline/audit_anchor.py`` ``HOST_CHAIN = "host"``), so a plan named
``host`` would share that anchor key with the host chain and make the anchor
log ambiguous.  :data:`pipeline.store_records.RESERVED_PLAN_NAMES` holds the
names no plan may take; :func:`validate_new_plan_name` is the guard the
authoring paths call before they write or authorize anything.
"""

from __future__ import annotations

from .store_records import RESERVED_PLAN_NAMES


class ReservedPlanNameError(ValueError):
    """Raised when a plan name is reserved (``host``)."""


def validate_new_plan_name(name: str) -> None:
    """Validate *name* as a new plan name, else raise.

    First the path-safety check every plan key goes through
    (``pipeline.server._validate_key``), then the reservation.  The server
    binding is read at call time, exactly as ``pipeline/service.py``'s
    ``_ServerRef`` does, so a monkeypatched ``pipeline.server._validate_key``
    still lands.
    """
    from . import server as _server

    _server._validate_key(name)
    if name in RESERVED_PLAN_NAMES:
        raise ReservedPlanNameError(
            'plan name "host" is reserved: the audit log uses it for the host chain'
        )
