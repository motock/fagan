"""Typed, call-time access to the server names that ``pipeline.ci`` reads.

``pipeline.ci`` runs some functions whose bodies read names owned by
``pipeline.server``. Tests patch those names on ``pipeline.server``, so the
functions must look them up there at call time. This module imports nothing
from ``pipeline.server``: :func:`server` reads ``sys.modules``, so ``pipeline.ci``
still imports cold.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from .store import Store
    from .ticketing import LogicalState, TicketProvider


class ServerBindings(Protocol):
    """The names ``pipeline.ci`` and ``pipeline.story_status`` read from ``pipeline.server``.

    Callables that take keyword arguments or defaults are typed ``Callable[..., R]``.
    """

    RETRO_PENDING_PATH: Path
    PIPELINE_SELF_REPO_ROOT: Path
    LogicalState: type[LogicalState]
    _store: Store
    _validate_key: Callable[[str], None]
    _notify_user: Callable[..., None]
    _record_retro_pending: Callable[[str, int], None]
    get_ticket_provider: Callable[[], TicketProvider]

    # pipeline.story_status / pipeline.detached_grade (check_story_status path)
    DETACHED_GRADE_WATCHDOG_SECONDS: int
    DISPATCH_MAX_ATTEMPTS: int
    DISPATCH_STALE_ACTIVITY_SECONDS: int
    DISPATCH_STARTUP_GRACE_SECONDS: int
    DISPATCH_WATCHDOG_SECONDS: int
    REWORK_MAX_ATTEMPTS_NO_COMMIT: int
    _LOCAL_BACKEND_NAMES: frozenset[str]
    _acceptance_tampered: Callable[[dict[str, Any], str], list[str]]
    _added_pytest_test_paths: Callable[..., list[str]]
    _auto_escalation_enabled: Callable[[], bool]
    _default_branch: Callable[[], str]
    _detached_grade_lifecycle: Callable[
        ...,
        tuple[dict[str, Any] | None, subprocess.CompletedProcess[Any] | None],
    ]
    _escalate_review_to_claude: Callable[..., None]
    _escalate_to_claude: Callable[..., None]
    _escalation_label: Callable[..., str]
    _escalation_target: Callable[..., tuple[str, str | None]]
    _find_dead_new_functions: Callable[..., list[str]]
    _heavy_lock: Callable[[], AbstractContextManager[Any]]
    _is_heavy: Callable[[list[str]], bool]
    _is_hidden_by_pytest_ignores: Callable[[str, list[str]], bool]
    _last_done_summary: Callable[[Path], str]
    _last_nonempty_line: Callable[[Path], str]
    _pytest_ignored_paths: Callable[[list[str]], list[str]]
    _rebrief_step_cap_struggle: Callable[..., None]
    _run_lint_gate: Callable[..., dict[str, Any] | None]
    _scope_test_cmd_to_acceptance: Callable[..., list[str] | None]
    _terminate_and_checkpoint: Callable[..., str]
    _worktree_has_new_commits: Callable[..., bool]
    collect_story_wedge_signals: Callable[[str, str, dict[str, Any]], dict[str, Any]]
    detect_test_command: Callable[[Path], tuple[Path, list[str]]]


def server() -> ServerBindings:
    """Return the loaded ``pipeline.server`` module, typed as its ci bindings."""
    module = sys.modules.get("pipeline.server")
    if module is None:
        raise RuntimeError("pipeline.server is not loaded")
    return cast(ServerBindings, module)
