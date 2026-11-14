"""Executor registry for the park-class ruling actions.

Every action in ``NEW_ACTIONS`` has a real executor. A new action must name its
executor in ``_IMPLEMENTED``: a missing entry fails at import, so no action can
silently fall back to a stub.
"""

from collections.abc import Callable

from .triage_park_classes import NEW_ACTIONS
from .triage_rebase import execute_resolve_rebase
from .triage_rebrief import execute_rebrief, execute_retry_transient
from .triage_test_edit import execute_approve_test_edit

EXECUTORS: dict[str, Callable] = {}


def register(action: str) -> Callable[[Callable], Callable]:
    """Decorator: register ``fn`` as the executor for ``action``."""

    def decorator(fn: Callable) -> Callable:
        EXECUTORS[action] = fn
        return fn

    return decorator


_IMPLEMENTED: dict[str, Callable] = {
    "resolve_rebase": execute_resolve_rebase,
    "retry_transient": execute_retry_transient,
    "rebrief": execute_rebrief,
    "approve_test_edit": execute_approve_test_edit,
}

for _action in sorted(NEW_ACTIONS):
    register(_action)(_IMPLEMENTED[_action])
