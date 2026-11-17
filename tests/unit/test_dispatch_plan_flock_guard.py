"""A dispatch is guarded by the plan flock, not by a store transaction.

``pipeline/dispatch.py``'s ``_dispatch_story_impl`` used to run its whole body
- the baseline snapshot, the worktree/venv provisioning and the agent spawn -
inside ``with _store.transaction(plan_name) as acquired:``. That held a
PostgreSQL connection with an open transaction (and the plan flock) for
minutes, so the scheduler's scan stalled behind it and ``last_scan_ts`` froze
for its 900 s join window - a wedged scheduler.

Cross-process exclusion is already provided by the PLAN FLOCK:
``advance_pipeline``'s tick holds ``_plan_lock`` and the MCP ``dispatch_story``
acquires it too, and ``_plan_lock`` is reentrant per thread. The store
transaction added only single-statement atomicity, which the dispatch's
autocommit writes already had.

These tests pin the guard dispatch actually takes. The behavioural half (the
``skipped: locked`` refusal under contention, the launch when the lock is
free, and the absence of any store transaction) lives in
``tests/unit/test_dispatch_story_writes_through_the_store.py``, which already
drives the real dispatch path; this file covers the module-level contract.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from pipeline import dispatch as pdispatch

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_checker():
    """Load scripts/check_line_limit.py as a module.

    The line cap and the allowlists are read out of the real checker rather
    than hardcoded here, so they cannot drift from the repo's own gate.
    """
    spec = importlib.util.spec_from_file_location(
        "check_line_limit", REPO_ROOT / "scripts" / "check_line_limit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. the dispatch module binds the shared plan flock
# ---------------------------------------------------------------------------


def test_dispatch_module_exposes_the_plan_flock():
    """The dispatch module binds the shared plan-flock helper.

    The guard dispatch takes must be the one the rest of the pipeline uses
    (``pipeline.concurrency._plan_lock``), so a dispatch and an
    ``advance_pipeline`` tick exclude each other.
    """
    from pipeline import concurrency

    assert hasattr(pdispatch, "_plan_lock"), (
        "pipeline.dispatch must bind the plan flock it guards a dispatch with"
    )
    assert pdispatch._plan_lock is concurrency._plan_lock


def test_dispatch_module_does_not_use_the_store_plan_transaction():
    """``pipeline/dispatch.py`` must not open the store's plan transaction.

    ``grep -c "_store.transaction" pipeline/dispatch.py`` is 0: the old guard
    held a database connection with an open transaction across the whole
    provisioning and the agent spawn.
    """
    source = (REPO_ROOT / "pipeline" / "dispatch.py").read_text(encoding="utf-8")

    assert "_store.transaction" not in source, (
        "pipeline/dispatch.py still opens a store transaction"
    )
    assert "_plan_lock(" in source, (
        "pipeline/dispatch.py must guard a dispatch with the plan flock"
    )


# ---------------------------------------------------------------------------
# 2. the file stays inside the repo's blocking line cap
# ---------------------------------------------------------------------------


def test_dispatch_module_stays_inside_the_blocking_line_limit():
    """``pipeline/dispatch.py`` must stay under the repo's blocking line cap.

    The cap is enforced by ``scripts/check_line_limit.py`` (production Python
    is blocking, exit 1) and the blocking allowlist is empty, so this story's
    edit may not push the file over the limit. The limit is read from the
    checker itself rather than hardcoded, so the cap stays the repo's.
    """
    checker = _load_checker()
    path = REPO_ROOT / "pipeline" / "dispatch.py"

    count = checker.count_lines(path)

    assert count < checker.DEFAULT_LIMIT, (
        f"pipeline/dispatch.py is {count} lines, at or over the "
        f"{checker.DEFAULT_LIMIT}-line cap"
    )


def test_dispatch_module_is_not_in_the_blocking_allowlist():
    """The dispatch module must not be a grandfathered over-limit file.

    The blocking allowlist is a registry that split stories retire entry by
    entry, so only membership of this one path is asserted - never the
    allowlist's size or contents.
    """
    checker = _load_checker()

    assert "pipeline/dispatch.py" not in checker._BLOCKING_ALLOWLIST
