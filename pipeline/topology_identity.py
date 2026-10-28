"""Resolved topology identity for the running pipeline process.

Answers "which plan dir, which repo, which branch/worktree namespace is this
process actually operating on?" — the question an operator asks when two
processes (dashboard, scheduler, agent) disagree about where they are.

The values are **observational**: they are read from the live module
bindings and the environment at call time and reported raw. Nothing here
validates or normalises a namespace — a value the branch-name validator
would reject is still reported verbatim, because the point is to show what
the process sees, not to gate it.

Reads are deliberately late-bound:

* ``pipeline.paths.PLAN_DIR`` and ``pipeline.server.REPO_ROOT`` are looked up
  as module attributes on every call (tests and long-lived processes
  re-patch them; a module-load snapshot would go stale). The ``paths``
  module itself is resolved through the ``pipeline`` package attribute at
  call time for the same reason: a caller that re-imports a submodule (the
  smoke tests do, to re-read env at import time) rebinds the package
  attribute, and a module-level ``from pipeline import paths`` would keep
  reading the superseded module object.
* ``pipeline.server`` is imported *inside* the function: ``pipeline.server``
  imports broadly and importing it at module scope here would create an
  import cycle for callers such as ``app.dashboard``.

See ``docs/specs/TOPOLOGY_HEALTH.md`` for the ``/api/health`` surface.
"""
from __future__ import annotations

import os

from pipeline import branch_names

WORKTREE_NAMESPACE_ENV = "PIPELINE_WORKTREE_NAMESPACE"


def _namespace(env_var: str) -> str | None:
    """Return the raw env value, or ``None`` when unset or empty.

    Empty is treated as unset because the pipeline's own namespace helpers
    default to ``""`` for "no namespace configured"; reporting ``""`` would
    make "unset" indistinguishable from "set to nothing" for a reader.
    """
    return os.environ.get(env_var) or None


def resolved_topology() -> dict[str, str | None]:
    """Return this process's resolved topology as a plain dict.

    Keys (exactly these four):

    * ``plan_dir`` — ``str(pipeline.paths.PLAN_DIR)``
    * ``repo_root`` — ``str(pipeline.server.REPO_ROOT)`` (the active target
      repo root)
    * ``branch_namespace`` — ``PIPELINE_BRANCH_NAMESPACE`` or ``None``
    * ``worktree_namespace`` — ``PIPELINE_WORKTREE_NAMESPACE`` or ``None``
    """
    # Lazy imports: pipeline.server pulls in a large dependency graph and
    # importing it at module scope would cycle back through this package.
    # Both modules are resolved through the ``pipeline`` package attribute at
    # call time (not a module-level ``from pipeline import paths``) so a
    # caller that re-imports a submodule — rebinding the package attribute —
    # is still observed.
    from pipeline import paths, server

    return {
        "plan_dir": str(paths.PLAN_DIR),
        "repo_root": str(server.REPO_ROOT),
        "branch_namespace": _namespace(branch_names.NAMESPACE_ENV),
        "worktree_namespace": _namespace(WORKTREE_NAMESPACE_ENV),
    }


def resolved_runtime() -> dict[str, str]:
    """Return the runtime wiring this process actually selected.

    Keys so far (the block is cumulative — later work adds its own):

    * ``event_bus`` — the bus ``PIPELINE_EVENT_BUS`` selects: ``"inprocess"``
      (default), ``"jsonl"``, or the marker ``"invalid"`` when the variable
      holds an unsupported value. Reported by
      ``pipeline.event_wiring.reported_event_bus_name()``.
    * ``store`` — the store kind ``PIPELINE_STORE`` selects: ``"file"``
      (default). Reported by ``pipeline.store.store_kind()``; the kind is read
      without constructing a store, so a health probe never touches the plan
      directory.

    Read live on every call and free of side effects: the environment is not
    written to, and nothing is cached. An unset ``PIPELINE_EVENT_BUS`` therefore
    keeps meaning "inprocess" rather than freezing that choice into the process.

    Raises
    ------
    ``ValueError`` when ``PIPELINE_STORE`` holds a value the store selector
    cannot honour (anything other than unset or ``file``). The store selector
    is fail-closed: an operator who asked for another store must never be
    handed the JSON-file one silently, and the message never echoes the
    rejected value (it may be a path or a secret). An unsupported
    ``PIPELINE_EVENT_BUS``, by contrast, is reported as ``"invalid"`` rather
    than raised: :func:`pipeline.event_wiring.build_bus` stays fail-closed and
    still refuses to run with a selection it cannot honour, while this report
    degrades instead.

    ``resolved_topology()`` deliberately does not gain these keys — it stays
    pinned to its four documented topology fields.

    ``/api/health`` returns this dict as produced under its ``runtime`` key,
    never rebuilt as a literal, so a selection added here reaches the health
    payload without editing ``app/dashboard.py``.
    """
    # Imported inside the function: this module's reads are late-bound so a
    # caller that re-imports or re-patches ``pipeline.event_wiring`` is still
    # observed, and the import graph stays free of a cycle at module load.
    from pipeline.event_wiring import reported_event_bus_name
    from pipeline.store import store_kind

    return {"event_bus": reported_event_bus_name(), "store": store_kind()}
