"""Typed, call-time access to the server names that ``pipeline.ci`` reads.

``pipeline.ci`` runs some functions whose bodies read names owned by
``pipeline.server``. Tests patch those names on ``pipeline.server``, so the
functions must look them up there at call time. This module imports nothing
from ``pipeline.server``: :func:`server` reads ``sys.modules``, so ``pipeline.ci``
still imports cold.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from .store import Store
    from .ticketing import LogicalState, TicketProvider


class ServerBindings(Protocol):
    """The names ``pipeline.ci`` reads from ``pipeline.server``."""

    RETRO_PENDING_PATH: Path
    PIPELINE_SELF_REPO_ROOT: Path
    LogicalState: type[LogicalState]
    _store: Store
    _validate_key: Callable[[str], None]
    _notify_user: Callable[..., None]
    _record_retro_pending: Callable[[str, int], None]
    get_ticket_provider: Callable[[], TicketProvider]


def server() -> ServerBindings:
    """Return the loaded ``pipeline.server`` module, typed as its ci bindings."""
    module = sys.modules.get("pipeline.server")
    if module is None:
        raise RuntimeError("pipeline.server is not loaded")
    return cast(ServerBindings, module)
