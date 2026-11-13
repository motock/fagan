"""Executor registry for the park-class ruling actions.

Each action in ``NEW_ACTIONS`` starts with a shadow-only stub that returns
``"deferred"``. ``pipeline.triage.execute_ruling`` treats ``deferred`` as "no
executor yet" and parks the story for a human. A later story replaces a stub by
registering its own function for the same action.
"""

from collections.abc import Callable

from .triage_park_classes import NEW_ACTIONS
from .triage_rebase import execute_resolve_rebase
from .triage_rebrief import execute_rebrief, execute_retry_transient

EXECUTORS: dict[str, Callable] = {}


def register(action: str) -> Callable[[Callable], Callable]:
    """Decorator: register ``fn`` as the executor for ``action``."""

    def decorator(fn: Callable) -> Callable:
        EXECUTORS[action] = fn
        return fn

    return decorator


def _shadow_only(plan_name, story_key, story, ruling, manifest) -> str:
    """Stub: the action has no executor yet, so it only records a shadow ruling."""
    return "deferred"


#: Actions with a real executor (ov-2). Every other NEW_ACTIONS entry keeps the stub.
_IMPLEMENTED: dict[str, Callable] = {
    "resolve_rebase": execute_resolve_rebase,
    "retry_transient": execute_retry_transient,
    "rebrief": execute_rebrief,
}

for _action in sorted(NEW_ACTIONS):
    register(_action)(_IMPLEMENTED.get(_action, _shadow_only))
