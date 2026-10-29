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
from typing import Any

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


def resolved_runtime() -> dict[str, Any]:
    """Return the runtime wiring this process actually selected.

    Keys so far (the block is cumulative — later work adds its own):

    * ``event_bus`` — the bus ``PIPELINE_EVENT_BUS`` selects: ``"inprocess"``
      (default), ``"jsonl"``, or the marker ``"invalid"`` when the variable
      holds an unsupported value. Reported by
      ``pipeline.event_wiring.reported_event_bus_name()``.
    * ``store`` — the store kind ``PIPELINE_STORE`` selects: ``"file"``
      (default), ``"sqlite"``, or the marker ``"invalid"`` when the variable
      holds an unsupported value. Reported by
      ``pipeline.store.reported_store_kind()``; the kind is read without
      constructing a store, so a health probe never touches the plan directory.
    * ``broker`` — present **only** when ``event_bus`` is ``"nats"``, because
      only then is there a broker to report on. It is
      :func:`pipeline.events_nats_consumer.consumer_status()` verbatim: per
      durable consumer the ``pending`` backlog, the ``redelivered`` count and
      the ``dead_lettered`` count, or ``{"reachable": False}`` when the broker
      cannot be queried.

    Read live on every call: the environment is not written to, and no
    selection is cached. An unset ``PIPELINE_EVENT_BUS`` therefore keeps
    meaning "inprocess" rather than freezing that choice into the process. The
    one outbound read is ``broker``, and it is a read-only JetStream query
    whose result ``consumer_status()`` caches for 30 s.

    A selection the wiring cannot honour is always reported as ``"invalid"``,
    never raised: both :func:`pipeline.event_wiring.build_bus` and
    :func:`pipeline.store.build_store` stay fail-closed and refuse to serve an
    unsupported selection, so this report gains no safety from raising — it
    would only turn ``/api/health`` into a 500 and hide the diagnostic. Neither
    the rejected value nor the selector's exception text is echoed here (an
    environment value may be a path or a secret).

    ``resolved_topology()`` deliberately does not gain these keys — it stays
    pinned to its four documented topology fields.

    ``/api/health`` returns this dict as produced under its ``runtime`` key,
    never rebuilt as a literal, so a selection added here reaches the health
    payload without editing ``app/dashboard.py``.
    """
    # Imported inside the function: this module's reads are late-bound so a
    # caller that re-imports or re-patches ``pipeline.event_wiring`` is still
    # observed, and the import graph stays free of a cycle at module load.
    from pipeline.event_wiring import EVENT_BUS_NATS, reported_event_bus_name
    from pipeline.store import reported_store_kind

    runtime = {
        "event_bus": reported_event_bus_name(),
        "store": reported_store_kind(),
    }
    if runtime["event_bus"] == EVENT_BUS_NATS:
        # Imported here for the same reason, and because only a NATS-selected
        # process has a broker to ask: a process on the in-process bus never
        # imports the consumer module at all. ``consumer_status()`` never
        # raises and caches its answer, so a broker that is down or slow
        # degrades this key instead of the health probe.
        from pipeline.events_nats_consumer import consumer_status

        runtime["broker"] = consumer_status()
    return runtime
